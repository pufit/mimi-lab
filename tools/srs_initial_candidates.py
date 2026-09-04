#!/usr/bin/env python3
"""Build the CANDIDATE POOL for the initial, agent-curated SRS deck.

Read-only against the live DB. Mechanical filters here only WIDEN the pool —
they never decide what becomes a card. Judgment about whether a word's meaning
is unmistakable from its moment is done by the curation agents that read the
batches this script writes (the user, 2026-09-02: "Don't just stick to mechanical
rules (e.g. 1 unknown word)").

Output (data/srs-curation/):
  candidates.json      every candidate word with up to MAX_MOMENTS moments (+ context)
  batches/batch-NNN.json  BATCH_SIZE words each, in priority order (for fan-out)
  manifest.json        list of batch files + counts
  summary.txt          human-readable stats

Usage:
  .venv/bin/python tools/srs_initial_candidates.py [--db PATH] [--out DIR] [--max-words N]
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.learn.service import (  # noqa: E402  (pure helpers, no DB access)
    _NON_VOCAB_POS,
    _feat,
    _is_content,
    _lemma_of,
    _reading_of,
    _tagger,
    _to_hira,
    _token_status,
)

MAX_MOMENTS = 5
BATCH_SIZE = 15          # ~30-40k tokens per batch file — one careful judge agent per batch
DIGIT_RE = re.compile(r"^[0-9０-９]+$")
KANJI_RE = re.compile(r"[一-鿿㐀-䶿]")
LYRIC_RE = re.compile(r"[♪♬♫♩]")


def ro_connect(path: Path) -> sqlite3.Connection:
    cx = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    cx.row_factory = sqlite3.Row
    return cx


def load_glosses(forms: list[str]) -> dict[str, str]:
    p = ROOT / "app" / "learn" / "data" / "glosses.sqlite"
    if not p.exists() or not forms:
        return {}
    gx = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    gx.row_factory = sqlite3.Row
    out: dict[str, str] = {}
    CH = 800
    for i in range(0, len(forms), CH):
        chunk = forms[i:i + CH]
        q = ",".join("?" * len(chunk))
        for r in gx.execute(f"SELECT form, gloss FROM glosses WHERE form IN ({q})", chunk):
            out.setdefault(r["form"], r["gloss"])
    gx.close()
    return out


def rank_benefit(rank):
    if rank is None:
        return -2.0
    if rank <= 3000:
        return 4.0
    if rank <= 6000:
        return 3.0
    if rank <= 12000:
        return 2.0
    if rank <= 20000:
        return 1.0
    return 0.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(ROOT / "data" / "mimi_lab.db"))
    ap.add_argument("--out", default=str(ROOT / "data" / "srs-curation"))
    ap.add_argument("--max-words", type=int, default=1200)
    ap.add_argument("--basic-rank", type=int, default=1000,
                    help="JPDB rank at/below which an unknown word is parked as 'probably known'")
    ap.add_argument("--rank-range", default="",
                    help="LO-HI: keep only words whose JPDB rank is within this range (supplementary runs)")
    ap.add_argument("--batch-offset", type=int, default=0, help="first batch number (supplementary runs)")
    ap.add_argument("--suffix", default="", help="output file suffix, e.g. 'supp' → candidates-supp.json")
    args = ap.parse_args()
    sfx = f"-{args.suffix}" if args.suffix else ""
    rank_lo, rank_hi = (0, 10**9)
    if args.rank_range:
        lo_s, _, hi_s = args.rank_range.partition("-")
        rank_lo, rank_hi = int(lo_s), int(hi_s)

    t0 = time.time()
    out_dir = Path(args.out)
    (out_dir / "batches").mkdir(parents=True, exist_ok=True)
    cx = ro_connect(Path(args.db))

    # ---- known sets (same semantics as learn._known_lookup) -----------------
    known: set[str] = set()
    ignored: set[str] = set()
    learning: set[str] = set()
    known_readings: set[str] = set()   # readings of KNOWN words (catches kana spellings of known kanji words: とる←取る)
    for r in cx.execute("SELECT dict_form, reading, status FROM known_words"):
        st = (r["status"] or "").upper()
        if not r["dict_form"]:
            continue
        if st == "KNOWN":
            known.add(r["dict_form"])
            if r["reading"]:
                known_readings.add(_to_hira(r["reading"]))
            if not KANJI_RE.search(r["dict_form"]):
                known_readings.add(_to_hira(r["dict_form"]))
        elif st == "IGNORED":
            ignored.add(r["dict_form"])
        elif st == "LEARNING":
            learning.add(r["dict_form"])

    # ---- downloaded episodes with a video that exists on disk ---------------
    eps: dict[int, dict] = {}
    for r in cx.execute(
        "SELECT e.id, e.anilist_id, e.ep_number, e.title AS ep_title, e.video_path, e.duration_ms, "
        "t.romaji, t.english FROM episodes e JOIN titles t ON t.anilist_id=e.anilist_id "
        "WHERE e.video_path IS NOT NULL AND e.video_path != ''"
    ):
        if not Path(r["video_path"]).exists():
            continue
        eps[r["id"]] = dict(r)
    ep_ids = list(eps)
    print(f"episodes with video on disk: {len(ep_ids)}")

    # ---- lines + lemmas ------------------------------------------------------
    lines: dict[int, dict] = {}
    by_ep_idx: dict[tuple[int, int], int] = {}
    q = ",".join("?" * len(ep_ids))
    for r in cx.execute(
        f"SELECT id, episode_id, idx, start_ms, end_ms, text, translation FROM subtitle_lines "
        f"WHERE episode_id IN ({q})", ep_ids
    ):
        d = dict(r)
        d["tokens"] = []
        lines[d["id"]] = d
        by_ep_idx[(d["episode_id"], d["idx"])] = d["id"]
    for r in cx.execute(
        f"SELECT line_id, lemma, reading, surface FROM line_lemmas WHERE episode_id IN ({q}) "
        f"ORDER BY rowid", ep_ids
    ):
        ln = lines.get(r["line_id"])
        if ln is not None:
            ln["tokens"].append((r["lemma"], r["reading"], r["surface"]))
    print(f"lines: {len(lines)}  ({time.time()-t0:.1f}s)")

    # ---- POS via fugashi (line_lemmas.pos is empty for migaku tokens) -------
    tagger = _tagger()
    pos_by_form: dict[str, Counter] = defaultdict(Counter)
    pos2_by_form: dict[str, Counter] = defaultdict(Counter)
    for ln in lines.values():
        for w in tagger(ln["text"]):
            p1 = _feat(w, "pos1") or ""
            p2 = _feat(w, "pos2") or ""
            for key in {_lemma_of(w), w.surface}:
                if key:
                    pos_by_form[key][p1] += 1
                    if p2:
                        pos2_by_form[key][p2] += 1
    print(f"fugashi pass done ({time.time()-t0:.1f}s)")

    def pos_of(form: str) -> tuple[str, str]:
        p1 = pos_by_form.get(form)
        p2 = pos2_by_form.get(form)
        return (p1.most_common(1)[0][0] if p1 else "", p2.most_common(1)[0][0] if p2 else "")

    # ---- per-line unknown analysis ------------------------------------------
    words: dict[str, dict] = {}
    for ln in lines.values():
        unk: list[tuple[str, str, str]] = []
        seen = set()
        for lemma, reading, surface in ln["tokens"]:
            form = lemma or surface
            if not form or form in seen:
                continue
            if not _is_content(lemma, surface) or DIGIT_RE.match(form):
                continue
            st = _token_status(lemma, surface, known, ignored, reading=reading)
            if st != "UNKNOWN":
                continue
            p1, _p2 = pos_of(form)
            if p1 in _NON_VOCAB_POS:
                continue
            seen.add(form)
            unk.append((form, reading or "", surface or ""))
        ln["unknown"] = unk
        for form, reading, surface in unk:
            w = words.setdefault(form, {"lemma": form, "readings": Counter(), "surfaces": Counter(),
                                        "lines": [], "eps": set(), "occ": 0})
            w["occ"] += 1
            w["eps"].add(ln["episode_id"])
            w["lines"].append(ln["id"])
            if reading:
                w["readings"][_to_hira(reading)] += 1
            if surface:
                w["surfaces"][surface] += 1
    print(f"distinct unknown content lemmas: {len(words)}  ({time.time()-t0:.1f}s)")

    # ---- decorate: rank, gloss, leverage -------------------------------------
    forms = list(words)
    ranks: dict[str, int] = {}
    for i in range(0, len(forms), 800):
        chunk = forms[i:i + 800]
        qq = ",".join("?" * len(chunk))
        for r in cx.execute(f"SELECT lemma, rank FROM lemma_freq WHERE lemma IN ({qq})", chunk):
            ranks[r["lemma"]] = r["rank"]
    glosses = load_glosses(forms)
    leverage: dict[str, dict] = {}
    row = cx.execute("SELECT value FROM kv WHERE key='leverage.cache'").fetchone()
    if row and row["value"]:
        try:
            for w in json.loads(row["value"]).get("words", []):
                leverage[w["lemma"]] = {"crossings": w.get("crossings", 0),
                                        "cumulative_unlocked": w.get("cumulative_unlocked", 0)}
        except Exception:
            pass

    # ---- word-level WIDE filter + score --------------------------------------
    BASIC_RANK = args.basic_rank   # ≤ this JPDB rank at ~3k known words = almost surely known-but-untracked

    def grammar_combo(form: str) -> bool:
        """True when UniDic splits the form into several tokens that include a
        particle/auxiliary (かも = か+も, んで, とも, だが…): a function-word
        combination Migaku's tokenizer emits as one token — never vocabulary."""
        toks = list(tagger(form))
        if len(toks) <= 1:
            return False
        return any((_feat(t, "pos1") or "") in _NON_VOCAB_POS for t in toks)

    dropped = Counter()
    pool: list[dict] = []
    probably_known: list[dict] = []
    for form, w in words.items():
        p1, p2 = pos_of(form)
        rank = ranks.get(form)
        gloss = glosses.get(form)
        if p2 == "固有名詞":
            dropped["proper_noun"] += 1
            continue
        if not p1 and grammar_combo(form):
            dropped["grammar_combo"] += 1
            continue
        if p1 in _NON_VOCAB_POS or p1 in {"接続詞", "接頭辞", "接尾辞", "連体詞"} and (rank or 10**9) <= 3000:
            dropped["function_word"] += 1
            continue
        if not KANJI_RE.search(form) and _to_hira(form) in known_readings:
            probably_known.append({"lemma": form, "why": "kana spelling of a KNOWN word (reading match)",
                                   "jpdb_rank": rank, "occurrences": w["occ"], "episodes": len(w["eps"])})
            dropped["kana_of_known"] += 1
            continue
        if rank is not None and rank <= BASIC_RANK:
            probably_known.append({"lemma": form, "why": f"JPDB rank {rank} ≤ {BASIC_RANK}",
                                   "jpdb_rank": rank, "occurrences": w["occ"], "episodes": len(w["eps"]),
                                   "gloss": gloss})
            dropped["basic_rank"] += 1
            continue
        if args.rank_range and not (rank is not None and rank_lo <= rank <= rank_hi):
            dropped["outside_rank_range"] += 1
            continue
        if not KANJI_RE.search(form) and not gloss:
            dropped["kana_no_gloss"] += 1          # tokenizer fragments (かあ, いやー…)
            continue
        if len(form) == 1 and not KANJI_RE.search(form):
            dropped["single_kana"] += 1
            continue
        if rank is None and len(w["eps"]) < 2 and not gloss:
            dropped["unranked_rare_nogloss"] += 1
            continue
        lev = leverage.get(form, {})
        score = (3.0 * min(lev.get("crossings", 0), 5)
                 + 2.0 * math.log1p(w["occ"])
                 + 1.5 * min(len(w["eps"]), 8)
                 + rank_benefit(rank))
        pool.append({
            "lemma": form,
            "reading": (w["readings"].most_common(1)[0][0] if w["readings"] else ""),
            "surfaces": [s for s, _ in w["surfaces"].most_common(4)],
            "pos1": p1, "pos2": p2,
            "jpdb_rank": rank,
            "gloss_jmdict": gloss,
            "occurrences": w["occ"],
            "episodes": len(w["eps"]),
            "leverage_crossings": lev.get("crossings", 0),
            "migaku_learning": form in learning,
            "score": round(score, 3),
            "_lines": w["lines"],
        })
    pool.sort(key=lambda d: (-d["score"], d["jpdb_rank"] or 10**9))
    total_pool = len(pool)
    pool = pool[: args.max_words]
    print(f"pool: {total_pool} words after wide filter; keeping top {len(pool)}; dropped={dict(dropped)}")

    # ---- moments per word ----------------------------------------------------
    def ctx_lines(ep_id: int, idx: int, span: int = 2) -> list[dict]:
        out = []
        for j in range(idx - span, idx + span + 1):
            lid = by_ep_idx.get((ep_id, j))
            if lid is None:
                continue
            l = lines[lid]
            out.append({"line_id": lid, "idx": j, "is_target": j == idx,
                        "text": l["text"], "translation": l["translation"]})
        return out

    def moment_score(l: dict, target: str) -> float:
        others = [u for u in l["unknown"] if u[0] != target]
        n_tok = len(l["tokens"])
        dur = l["end_ms"] - l["start_ms"]
        s = 10.0 - 3.0 * len(others)
        s += 3.0 if l["translation"] else -4.0
        if 1500 <= dur <= 6000:
            s += 2.0
        elif dur < 900 or dur > 9000:
            s -= 4.0
        if 5 <= n_tok <= 14:
            s += 2.0
        elif n_tok < 3 or n_tok > 22:
            s -= 4.0
        if LYRIC_RE.search(l["text"]):
            s -= 6.0
        return s

    for w in pool:
        cands = []
        for lid in w["_lines"]:
            l = lines[lid]
            others = [u for u in l["unknown"] if u[0] != w["lemma"]]
            if len(others) > 2:            # widen: allow up to 2 OTHER unknowns
                continue
            cands.append((moment_score(l, w["lemma"]), lid))
        cands.sort(key=lambda t: -t[0])
        moments = []
        seen_text = set()
        for sc, lid in cands:
            l = lines[lid]
            if l["text"] in seen_text:
                continue
            seen_text.add(l["text"])
            e = eps[l["episode_id"]]
            surf = next((u[2] for u in l["unknown"] if u[0] == w["lemma"]), "")
            span = 2 if len(moments) < 3 else 1     # full context for the top-3 moments
            moments.append({
                "line_id": lid,
                "episode_id": l["episode_id"],
                "anilist_id": e["anilist_id"],
                "show": e["romaji"] or e["english"],
                "ep_number": e["ep_number"],
                "start_ms": l["start_ms"], "end_ms": l["end_ms"],
                "text": l["text"],
                "translation": l["translation"],
                "target_surface": surf,
                "other_unknowns": [{"lemma": u[0], "reading": u[1], "surface": u[2]}
                                    for u in l["unknown"] if u[0] != w["lemma"]],
                "n_tokens": len(l["tokens"]),
                "context": ctx_lines(l["episode_id"], l["idx"], span=span),
            })
            if len(moments) >= MAX_MOMENTS:
                break
        w["moments"] = moments
        w["n_candidate_lines"] = len(cands)
        del w["_lines"]

    pool = [w for w in pool if w["moments"]]
    print(f"words with >=1 usable moment: {len(pool)}  ({time.time()-t0:.1f}s)")

    # ---- write ---------------------------------------------------------------
    stats = {
        "episodes": len(ep_ids), "lines": len(lines),
        "known": len(known), "ignored": len(ignored), "learning": len(learning),
        "distinct_unknown_lemmas": len(words), "pool_after_wide_filter": total_pool,
        "kept_words": len(pool), "dropped": dict(dropped),
        "moments_total": sum(len(w["moments"]) for w in pool),
    }
    (out_dir / f"candidates{sfx}.json").write_text(
        json.dumps({"generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "stats": stats, "words": pool},
                   ensure_ascii=False, indent=1))
    probably_known.sort(key=lambda d: (d["jpdb_rank"] or 10**9))
    (out_dir / f"probably_known{sfx}.json").write_text(json.dumps(probably_known, ensure_ascii=False, indent=1))
    stats["probably_known"] = len(probably_known)
    def dump_batch(batch_no: int, chunk: list[dict]) -> str:
        """Readable-but-compact layout: one line per word header, one line per
        moment (each moment carries its own context) — a Read tool shows the
        whole batch in ~100 lines without whitespace bloat."""
        out = [f'{{"batch": {batch_no}, "words": [']
        for wi, w in enumerate(chunk):
            head = {k: v for k, v in w.items() if k != "moments"}
            out.append(json.dumps(head, ensure_ascii=False)[:-1] + ', "moments": [')
            for mi, m in enumerate(w["moments"]):
                out.append("  " + json.dumps(m, ensure_ascii=False) + ("," if mi < len(w["moments"]) - 1 else ""))
            out.append("]}" + ("," if wi < len(chunk) - 1 else ""))
        out.append("]}")
        return "\n".join(out) + "\n"

    manifest = []
    for i in range(0, len(pool), BATCH_SIZE):
        chunk = pool[i:i + BATCH_SIZE]
        bno = args.batch_offset + i // BATCH_SIZE
        name = f"batches/batch-{bno:03d}.json"
        (out_dir / name).write_text(dump_batch(bno, chunk))
        manifest.append({"file": str(out_dir / name), "batch": bno, "words": len(chunk),
                         "first": chunk[0]["lemma"], "last": chunk[-1]["lemma"],
                         "score_range": [chunk[0]["score"], chunk[-1]["score"]]})
    (out_dir / f"manifest{sfx}.json").write_text(json.dumps({"stats": stats, "batches": manifest},
                                                            ensure_ascii=False, indent=1))
    summary = [f"generated {time.strftime('%Y-%m-%d %H:%M:%S')} in {time.time()-t0:.1f}s"]
    summary += [f"{k}: {v}" for k, v in stats.items()]
    summary.append(f"batches: {manifest[0]['batch'] if manifest else '-'}..{manifest[-1]['batch'] if manifest else '-'}")
    summary.append("top 40 words: " + ", ".join(
        f"{w['lemma']}({w['jpdb_rank']},{w['occurrences']}x,{w['episodes']}ep,lev{w['leverage_crossings']})"
        for w in pool[:40]))
    (out_dir / f"summary{sfx}.txt").write_text("\n".join(summary) + "\n")
    print("\n".join(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
