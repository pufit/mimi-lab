#!/usr/bin/env python3
"""Re-judge the deck with the translation-blind clarity gate and build sibling cards.

The user (2026-09-03) on 攻撃 (stack #2): "the context is unclear to me". The first
judge admitted the English translation as a clarity cue; half the deck's
`why_clear` texts lean on it. This tool re-runs every live card through
`app.srs.blind_judge` (mask the word, infer from the Japanese alone, grade the
guess) and applies the result, plus the multi-card rule (≤ MAX_CARDS_PER_LEMMA
cards per word, each from a different anime).

  judge   for every live card: its moment + up to --alternates alternate moments
          (candidate pool; different anime first) in parallel (--workers). Verdicts
          are appended to data/srs-curation/rejudge/verdicts.jsonl — resumable, a
          judged (lemma, line_id) is skipped. Read-only on the live DB.
  apply   per word: primary passes → keep (clarity / why_clear / evidence lines
          refreshed, clip re-cut when the window changes); primary fails but an
          alternate passes → swap the card onto it; nothing passes → state='rejected'
          (review history kept). Then siblings: further passing moments from OTHER
          anime become new cards at the stack bottom (≤ 3 per word). Dry-run by
          default; --apply writes (JSON backup of the srs_* tables first). Needs the
          server on the multi-card code (srs_cards.lemma no longer UNIQUE).
  report  summarise verdicts.jsonl.

Usage:
  .venv/bin/python tools/srs_rejudge_blind.py judge [--workers 24] [--alternates 3] [--limit N] [--lemma 語]
  .venv/bin/python tools/srs_rejudge_blind.py apply [--apply] [--limit N] [--lemma 語]
  .venv/bin/python tools/srs_rejudge_blind.py report
"""
from __future__ import annotations

import argparse
import dataclasses
import gzip
import json
import sys
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CUR = ROOT / "data" / "srs-curation"
OUT = CUR / "rejudge"
VERDICTS = OUT / "verdicts.jsonl"
PLAN = OUT / "plan.json"
PRICES = {"claude-opus-5": (5.0, 25.0), "claude-sonnet-4-6": (3.0, 15.0),
          "claude-haiku-4-5": (1.0, 5.0)}
STAMP = "blind re-judge 2026-09-03"
BLIND_MODEL = "blind:claude-opus-5"


def _vnote(v: dict) -> str:
    """The srs_moments note for a verdict — the same text the generator writes."""
    from app.srs.blind_judge import fail_note, passes
    n = fail_note(v)
    return n.replace("blind:", "blind ✓:", 1) if passes(v) else n

CARD_COLS = (
    "id, lemma, reading, pos, gloss, meaning_short, meaning_full, usage_note, tags_json, "
    "freq_rank, source, score, usefulness, priority, line_id, episode_id, anilist_id, "
    "show_title, ep_number, start_ms, end_ms, text, norm_text, translation, "
    "translation_source, target_surface, context_json, extend_json, clip_status, "
    "clip_version, state, notes"
)


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def load_cards(cx, lemma: Optional[str] = None, include_rejected: bool = False) -> list[dict]:
    q = f"SELECT {CARD_COLS} FROM srs_cards WHERE source<>'confirm' AND line_id IS NOT NULL"
    if not include_rejected:
        q += " AND state<>'rejected'"
    args: tuple = ()
    if lemma:
        q += " AND lemma=?"
        args = (lemma,)
    return [dict(r) for r in cx.execute(q + " ORDER BY id", args)]


def _reload_card(card_id: int) -> dict:
    from app.db import connect
    cx = connect()
    try:
        return dict(cx.execute(f"SELECT {CARD_COLS} FROM srs_cards WHERE id=?", (card_id,)).fetchone())
    finally:
        cx.close()


def load_candidates() -> dict[str, list[dict]]:
    """lemma → candidate moments (mechanical rank order) from candidates*.json."""
    out: dict[str, list[dict]] = defaultdict(list)
    for p in sorted(CUR.glob("candidates*.json")):
        for w in json.loads(p.read_text())["words"]:
            for rank, m in enumerate(w.get("moments") or []):
                out[w["lemma"]].append({
                    "line_id": int(m["line_id"]), "anilist_id": m.get("anilist_id"),
                    "show": m.get("show"), "ep_number": m.get("ep_number"), "text": m.get("text"),
                    "translation": m.get("translation"), "target_surface": m.get("target_surface"),
                    "rank": rank,
                })
    return out


def reference_of(card: dict) -> str:
    parts = [card.get("meaning_short") or "", card.get("meaning_full") or ""]
    ref = " — ".join(p for p in parts if p)
    if card.get("gloss") and (card["gloss"] not in ref):
        ref = f"{ref} (JMdict: {card['gloss']})" if ref else card["gloss"]
    return ref


def pick_alternates(card: dict, cands: list[dict], k: int) -> list[dict]:
    """≤k alternate moments: one per OTHER anime first (best rank each), then
    the rest by rank."""
    from app.srs.franchise import franchise_key

    prim_fr = franchise_key(card.get("show_title"))
    others, seen = [], {int(card["line_id"])}
    for m in cands:
        if m["line_id"] in seen:
            continue
        seen.add(m["line_id"])
        others.append(m)
    by_fr: dict[str, list[dict]] = defaultdict(list)
    for m in others:
        by_fr[franchise_key(m.get("show"))].append(m)
    picks = [ms[0] for fr, ms in by_fr.items() if fr and fr != prim_fr]
    picks.sort(key=lambda m: m["rank"])
    picks = picks[:k]
    if len(picks) < k:
        rest = [m for m in others if m not in picks]
        picks += rest[: k - len(picks)]
    return picks


def load_verdict_records() -> list[dict]:
    """Verdict records, LAST record per (lemma, line_id) wins (re-tries append),
    with `verdict.passed` recomputed by the current pass rule."""
    if not VERDICTS.exists():
        return []
    from app.srs.blind_judge import passes

    by_key: dict[tuple[str, int], dict] = {}
    for line in VERDICTS.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        v = r.get("verdict") or {}
        v["passed"] = passes(v)
        r["verdict"] = v
        by_key[(r["lemma"], int(r["line_id"]))] = r
    return list(by_key.values())


def _cost(usage: dict[str, dict]) -> float:
    total = 0.0
    for model, u in usage.items():
        pin, pout = PRICES.get(model, (5.0, 25.0))
        total += (u.get("in", 0) * pin + u.get("out", 0) * pout) / 1_000_000.0
    return total


# ---------------------------------------------------------------------------
# judge
# ---------------------------------------------------------------------------

def cmd_judge(args) -> int:
    from app import llm
    from app.db import connect
    from app.srs import blind_judge

    OUT.mkdir(parents=True, exist_ok=True)
    cx = connect()
    try:
        # rejected cards are included: a word parked by an earlier pass gets its
        # second look when the judge/context changes
        cards = load_cards(cx, args.lemma or None, include_rejected=True)
    finally:
        cx.close()
    cands = load_candidates()
    latest = {(r["lemma"], int(r["line_id"])): r for r in load_verdict_records()}

    def needs(key) -> bool:
        """Resumable: skip a pair with a good verdict; re-try LLM errors; with
        --failed-only also re-judge every pair that did not pass (a gone line
        never changes and is skipped)."""
        r = latest.get(key)
        if r is None:
            return True
        v = r.get("verdict") or {}
        if v.get("error") == "line_gone":
            return False
        if v.get("error"):
            return True
        return bool(args.failed_only) and not v.get("passed")

    items: list[tuple[dict, dict]] = []
    for c in cards:
        moms = [{"role": "primary", "line_id": int(c["line_id"]), "anilist_id": c["anilist_id"],
                 "show": c["show_title"], "ep_number": c["ep_number"],
                 "target_surface": c["target_surface"], "translation": c["translation"]}]
        for m in pick_alternates(c, cands.get(c["lemma"], []), args.alternates):
            moms.append({"role": "alt", **{k: m.get(k) for k in
                         ("line_id", "anilist_id", "show", "ep_number", "target_surface", "translation")}})
        for m in moms:
            if not needs((c["lemma"], int(m["line_id"]))):
                continue
            items.append((c, m))
    done = {k for k, r in latest.items() if not (r.get("verdict") or {}).get("error")}
    if args.limit:
        items = items[: args.limit]
    n_prim = sum(1 for _c, m in items if m["role"] == "primary")
    print(f"cards={len(cards)} already_judged={len(done)} to_judge={len(items)} "
          f"(primary {n_prim}, alternates {len(items) - n_prim}) workers={args.workers}")
    if not items:
        return 0

    # exact token accounting for THIS process (kv counters are shared with the server)
    usage: dict[str, dict] = defaultdict(lambda: {"in": 0, "out": 0, "calls": 0})
    orig_record = llm._record_usage

    def _rec(model: str, u: dict) -> None:
        usage[model]["in"] += int(u.get("input_tokens") or 0)
        usage[model]["out"] += int(u.get("output_tokens") or 0)
        usage[model]["calls"] += 1
        orig_record(model, u)

    llm._record_usage = _rec                     # type: ignore[assignment]

    lock = threading.Lock()
    t0 = time.time()
    counts = Counter()

    def one(c: dict, m: dict) -> dict:
        v = blind_judge.judge_moment_by_id(
            lemma=c["lemma"], line_id=int(m["line_id"]), target_surface=m.get("target_surface"),
            pos=c.get("pos"), reference=reference_of(c),
            translation=m.get("translation") or c.get("translation"),
            show=m.get("show"), ep_number=m.get("ep_number"),
        )
        rec = {"card_id": c["id"], "lemma": c["lemma"], "role": m["role"], "line_id": int(m["line_id"]),
               "anilist_id": m.get("anilist_id"), "show": m.get("show"), "ep_number": m.get("ep_number"),
               "target_surface": m.get("target_surface"), "translation": m.get("translation"),
               "verdict": v, "at": _now_iso()}
        with lock:
            with VERDICTS.open("a") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return rec

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(one, c, m): (c, m) for c, m in items}
        for i, fut in enumerate(as_completed(futs), start=1):
            c, m = futs[fut]
            try:
                rec = fut.result()
                v = rec["verdict"]
                key = ("pass" if v.get("passed") else ("error" if v.get("error") else "fail"))
                counts[f"{m['role']}:{key}"] += 1
            except Exception as e:
                counts[f"{m['role']}:exception"] += 1
                print(f"  ! {c['lemma']} line {m['line_id']}: {type(e).__name__}: {e}")
            if i % 50 == 0 or i == len(items):
                el = time.time() - t0
                rate = i / el if el else 0.0
                eta = (len(items) - i) / rate if rate else 0.0
                print(f"  {i}/{len(items)}  {el:.0f}s  {rate * 60:.0f}/min  ETA {eta / 60:.0f} min  "
                      f"{dict(counts)}  ${_cost(usage):.2f}")
    print(f"done in {time.time() - t0:.0f}s: {dict(counts)}")
    print("usage:", {m: dict(u) for m, u in usage.items()}, f"≈ ${_cost(usage):.2f}")
    return 0


# ---------------------------------------------------------------------------
# apply
# ---------------------------------------------------------------------------

def _norm_extend(entries) -> list[dict]:
    out = []
    for x in entries or []:
        if isinstance(x, dict):
            out.append({"line_id": x.get("line_id"), "start_ms": int(x.get("start_ms") or 0),
                        "end_ms": int(x.get("end_ms") or 0), "text": x.get("text") or "",
                        "role": x.get("role") or "continuation"})
    return out


def _json_list(raw) -> list:
    try:
        v = json.loads(raw) if raw else []
    except Exception:
        return []
    return v if isinstance(v, list) else []


def build_plan(cards: list[dict]) -> list[dict]:
    """Word-centric plan (2026-09-03 rev 2). For every card of a word the
    verdict of its CURRENT line decides (a card swapped by an earlier pass is
    judged on the line it shows now, not on the run's original role); passing
    lines not shown by any live card of the word are the alternates; the anime
    rule is enforced across ALL live cards of the word, and siblings are
    proposed once per word (attached to its first live card)."""
    from app.srs.constants import MAX_CARDS_PER_LEMMA
    from app.srs.franchise import franchise_key

    recs = load_verdict_records()
    by_lemma: dict[str, dict[int, dict]] = defaultdict(dict)
    for r in recs:
        by_lemma[r["lemma"]][int(r["line_id"])] = r
    cards_by_lemma: dict[str, list[dict]] = defaultdict(list)
    for c in cards:
        cards_by_lemma[c["lemma"]].append(c)

    plan: list[dict] = []
    for lemma, lcards in cards_by_lemma.items():
        recs_l = by_lemma.get(lemma, {})
        live = [c for c in lcards if c.get("state") != "rejected"]
        used_lines = {int(c["line_id"]) for c in live if c.get("line_id")}
        used_fr = {franchise_key(c.get("show_title")) for c in live}

        def _passing_unused() -> list[dict]:
            out = [r for lid, r in recs_l.items()
                   if r["verdict"].get("passed") and not r["verdict"].get("error")
                   and lid not in used_lines]
            out.sort(key=lambda r: -float(r["verdict"].get("confidence") or 0))
            return out

        entries: list[dict] = []
        n_live_after = 0
        for c in lcards:
            rejected = c.get("state") == "rejected"
            prim = recs_l.get(int(c["line_id"])) if c.get("line_id") else None
            if prim is None:
                entries.append({"card": c, "action": "skip", "reason": "no verdict for its line"})
                if not rejected:
                    n_live_after += 1
                continue
            if prim["verdict"].get("error"):
                entries.append({"card": c, "action": "skip", "primary": prim,
                                "reason": f"verdict error: {prim['verdict']['error']}"})
                if not rejected:
                    n_live_after += 1
                continue
            if prim["verdict"].get("passed"):
                entries.append({"card": c, "action": "restore" if rejected else "keep",
                                "primary": prim, "keep": prim, "siblings": []})
                if rejected:
                    used_lines.add(int(c["line_id"]))
                    used_fr.add(franchise_key(c.get("show_title")))
                n_live_after += 1
                continue
            # the card's own line fails → move it onto a passing line of another
            # anime than the word's other live cards (or any, when it is alone)
            alts = _passing_unused()
            if rejected or len(live) > 1:
                others = used_fr - ({franchise_key(c.get("show_title"))} if not rejected else set())
                pick = next((a for a in alts if franchise_key(a.get("show")) not in others), None)
            else:
                pick = alts[0] if alts else None
            if pick is not None:
                used_lines.add(int(pick["line_id"]))
                used_fr.discard(franchise_key(c.get("show_title"))) if not rejected else None
                used_fr.add(franchise_key(pick.get("show")))
                entries.append({"card": c, "action": "restore_swap" if rejected else "swap",
                                "primary": prim, "keep": pick, "siblings": []})
                n_live_after += 1
            else:
                entries.append({"card": c, "action": "leave" if rejected else "reject",
                                "primary": prim, "keep": None, "siblings": []})
        # siblings: further passing lines from anime not yet used, up to the cap
        host = next((e for e in entries if e["action"] in ("keep", "swap", "restore", "restore_swap")), None)
        if host is not None:
            for a in _passing_unused():
                if n_live_after >= MAX_CARDS_PER_LEMMA:
                    break
                fr = franchise_key(a.get("show"))
                if not fr or fr in used_fr:
                    continue
                host["siblings"].append(a)
                used_fr.add(fr)
                used_lines.add(int(a["line_id"]))
                n_live_after += 1
        plan.extend(entries)
    return plan

def _summary(plan: list[dict]) -> str:
    acts = Counter(p["action"] for p in plan)
    sib = sum(len(p.get("siblings") or []) for p in plan)
    return (f"cards={len(plan)} keep={acts['keep']} swap={acts['swap']} reject={acts['reject']} "
            f"restore={acts['restore']} restore_swap={acts['restore_swap']} leave={acts['leave']} "
            f"skip={acts['skip']} siblings_to_create={sib}")


def apply_restore(p: dict, eps: dict) -> str:
    """A rejected card whose moment (or an alternate) now passes: the app's own
    `restore` action brings it back (§3.8 resume table), bulk restores go to the
    stack bottom, then the keep/swap refresh runs on the fresh row."""
    from app.srs import service

    card = p["card"]
    service.card_action(int(card["id"]), "restore")
    try:
        service.card_action(int(card["id"]), "bottom")
    except Exception:
        pass                                   # not a stack card (had memory) — leave it
    with service._txn() as cx:
        cx.execute("UPDATE srs_cards SET notes=COALESCE(notes,'') || ? WHERE id=?",
                   (f"\n[{STAMP}] restored: passes the blind test with the fixed/wider context",
                    card["id"]))
    p["card"] = _reload_card(int(card["id"]))
    msg = apply_keep(p, eps) if p["action"] == "restore" else apply_swap(p)
    return "restored; " + msg


def _backup(cx) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"backup-{time.strftime('%Y%m%d-%H%M%S')}.json.gz"
    dump = {}
    for t in ("srs_cards", "srs_words", "srs_moments", "srs_reviews"):
        dump[t] = [dict(r) for r in cx.execute(f"SELECT * FROM {t}")]
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump(dump, f, ensure_ascii=False)
    return path


def _line_dict(cx, line_id: int) -> Optional[dict]:
    r = cx.execute(
        "SELECT id, episode_id, idx, start_ms, end_ms, text FROM subtitle_lines WHERE id=?", (line_id,)
    ).fetchone()
    if not r:
        return None
    from app.srs.snapshot import strip_bidi
    return {"line_id": int(r["id"]), "episode_id": int(r["episode_id"]), "idx": r["idx"],
            "start_ms": int(r["start_ms"] or 0), "end_ms": int(r["end_ms"] or 0),
            "text": strip_bidi(r["text"] or "")}


def _moment_snap(card: dict) -> dict:
    """Minimal snapshot dict of the card's CURRENT moment for `_upsert_moment`."""
    return {"line_id": card["line_id"], "episode_id": card["episode_id"], "idx": None,
            "start_ms": card["start_ms"], "end_ms": card["end_ms"], "norm_text": card["norm_text"],
            "text": card["text"], "translation": card["translation"],
            "translation_source": card["translation_source"], "target_surface": card["target_surface"]}


def apply_keep(p: dict, eps: dict) -> str:
    from app.srs import clips, service
    from app.srs.blind_judge import why_clear_text
    from app.srs.snapshot import build_extend, dialogue_neighbours

    card, v = p["card"], p["keep"]["verdict"]
    now = service._now()
    with service._txn() as cx:
        cx.execute("UPDATE srs_cards SET clarity=?, why_clear=?, updated_at=datetime('now') WHERE id=?",
                   (float(v["confidence"]), why_clear_text(v), card["id"]))
        service._upsert_moment(cx, card["lemma"], _moment_snap(card), verdict="accept", accepted=1,
                               clarity=float(v["confidence"]), now=now,
                               evidence_line_ids=list(v.get("evidence_line_ids") or []),
                               note=_vnote(v), judge_model=BLIND_MODEL)
        line = _line_dict(cx, int(card["line_id"]))
        if line is None:
            return "kept (line gone, clip untouched)"
        before, after = dialogue_neighbours(cx, line, 2)
        extend, _tr = build_extend(cx, line, list(v.get("evidence_line_ids") or []),
                                   next_line=after[0] if after else None,
                                   allowed_ids={n["line_id"] for n in (*before, *after) if n.get("line_id")})
        prev, nxt = clips._neighbours(cx, card, extend)
        plan = clips.plan_window(line, extend, prev, nxt, eps.get(int(card["episode_id"] or 0)))
        if _norm_extend(plan.extend) == _norm_extend(_json_list(card.get("extend_json"))):
            return "kept"
        n = cx.execute(
            "UPDATE srs_cards SET extend_json=?, clip_start_ms=?, clip_end_ms=?, "
            "clip_version=clip_version+1, clip_status='pending', clip_requested_at=?, "
            "clip_error=NULL, updated_at=datetime('now') WHERE id=? AND clip_version=?",
            (json.dumps(plan.extend, ensure_ascii=False), plan.start_ms, plan.end_ms,
             service._sql(now), card["id"], card["clip_version"])).rowcount
        if n == 1:
            service._enqueue("srs_clip", {"card_id": int(card["id"]), "v": int(card["clip_version"]) + 1},
                             priority=30)
            return "kept + clip re-cut"
        return "kept (clip_version moved — window not changed)"


def apply_swap(p: dict) -> str:
    from app.srs import service
    from app.srs.blind_judge import fail_note, why_clear_text
    from app.srs.snapshot import snapshot_line

    card, keep = p["card"], p["keep"]
    v = keep["verdict"]
    # snapshot BEFORE the write transaction (may call Haiku to trim a merged cue)
    snap = snapshot_line(int(keep["line_id"]), card["lemma"], keep.get("target_surface"),
                         reading=card.get("reading"), clean=True,
                         evidence_line_ids=list(v.get("evidence_line_ids") or []))
    now = service._now()
    with service._txn() as cx:
        service._upsert_moment(cx, card["lemma"], _moment_snap(card), verdict="reject", accepted=0,
                               clarity=float(p["primary"]["verdict"].get("confidence") or 0), now=now,
                               note=_vnote(p["primary"]["verdict"]), judge_model=BLIND_MODEL)
        service._apply_snapshot(cx, int(card["id"]), snap, bump_clip=True, now=now)
        cx.execute(
            "UPDATE srs_cards SET clarity=?, why_clear=?, alt_moment_ids_json='[]', "
            "notes=COALESCE(notes,'') || ?, updated_at=datetime('now') WHERE id=?",
            (float(v["confidence"]), why_clear_text(v),
             f"\n[{STAMP}] moment swapped: previous line failed the blind test "
             f"({fail_note(p['primary']['verdict'])})", card["id"]))
        service._upsert_moment(cx, card["lemma"], snap, verdict="accept", accepted=1,
                               clarity=float(v["confidence"]), now=now,
                               evidence_line_ids=list(v.get("evidence_line_ids") or []),
                               note=_vnote(v), judge_model=BLIND_MODEL)
        version = int(cx.execute("SELECT clip_version FROM srs_cards WHERE id=?",
                                 (card["id"],)).fetchone()["clip_version"])
        service._enqueue("srs_clip", {"card_id": int(card["id"]), "v": version}, priority=30)
    return f"swapped → line {keep['line_id']} ({keep.get('show')})"


def apply_reject(p: dict) -> str:
    from app.srs import service
    from app.srs.blind_judge import fail_note

    card, pv = p["card"], p["primary"]["verdict"]
    guess = pv.get("best_guess") or "—"
    cands = ", ".join(c["meaning"] for c in (pv.get("candidates") or [])[:4]) or "—"
    note = (f"\n[{STAMP}] rejected: the context does not single out the meaning "
            f"({pv.get('inferability')}, {float(pv.get('confidence') or 0):.2f}; guess '{guess}' → "
            f"{pv.get('match')}; candidates: {cands}). Alternates tried: "
            f"{len([a for a in p.get('all_alts', [])])}.")
    now = service._now()
    with service._txn() as cx:
        cx.execute(
            "UPDATE srs_cards SET state='rejected', queue_pos=NULL, study_now=0, "
            "notes=COALESCE(notes,'') || ?, updated_at=datetime('now') WHERE id=?",
            (note, card["id"]))
        cx.execute(
            "UPDATE srs_words SET judge_status='rejected', judge_reason='no_clear_moment_blind', "
            "judge_note=?, judged_at=datetime('now'), updated_at=datetime('now') WHERE lemma=?",
            (fail_note(pv), card["lemma"]))
        service._upsert_moment(cx, card["lemma"], _moment_snap(card), verdict="reject", accepted=0,
                               clarity=float(pv.get("confidence") or 0), now=now,
                               note=_vnote(pv), judge_model=BLIND_MODEL)
        service.renumber_stack(cx)
    return f"rejected ({pv.get('inferability')}, guess '{guess}')"


def apply_sibling(p: dict, a: dict) -> str:
    from app.srs import service
    from app.srs.blind_judge import why_clear_text
    from app.srs.snapshot import CardSpec, snapshot_line

    card, v = p["card"], a["verdict"]
    snap = snapshot_line(int(a["line_id"]), card["lemma"], a.get("target_surface"),
                         reading=card.get("reading"), clean=True,
                         evidence_line_ids=list(v.get("evidence_line_ids") or []))
    fields = {f.name for f in dataclasses.fields(CardSpec)}
    kwargs = {k: val for k, val in snap.items() if k in fields}
    kwargs["lemma"] = card["lemma"]
    spec = CardSpec(**kwargs)
    spec.reading = card.get("reading") or spec.reading
    spec.pos = card.get("pos")
    spec.gloss = card.get("gloss")
    spec.meaning_short = card.get("meaning_short")
    spec.meaning_full = card.get("meaning_full")
    spec.usage_note = card.get("usage_note") or ""
    spec.tags = _json_list(card.get("tags_json"))
    spec.freq_rank = card.get("freq_rank")
    spec.source = "curated-initial"
    spec.score = card.get("score")
    spec.clarity = float(v["confidence"])
    spec.usefulness = card.get("usefulness")
    spec.priority = card.get("priority")
    spec.why_clear = why_clear_text(v)
    try:
        new_id = service.create_card(spec, position="bottom")
    except service.SrsConflict as e:
        return f"sibling skipped ({e})"
    now = service._now()
    with service._txn() as cx:
        service._upsert_moment(cx, card["lemma"], snap, verdict="accept", accepted=1,
                               clarity=float(v["confidence"]), now=now,
                               evidence_line_ids=list(v.get("evidence_line_ids") or []),
                               note=_vnote(v), judge_model=BLIND_MODEL)
        cx.execute("UPDATE srs_cards SET notes=COALESCE(notes,'') || ? WHERE id=?",
                   (f"\n[{STAMP}] sibling of card {card['id']} ({card.get('show_title')})", new_id))
    return f"sibling #{new_id} ← line {a['line_id']} ({a.get('show')})"


def cmd_apply(args) -> int:
    from app.db import connect

    cx = connect()
    try:
        ddl = cx.execute("SELECT sql FROM sqlite_master WHERE name='srs_cards'").fetchone()["sql"]
        if "NOT NULL UNIQUE" in ddl:
            print("ABORT: srs_cards.lemma is still UNIQUE — restart the server on the multi-card "
                  "code first (deploy/restart.sh server runs the migration at boot)")
            return 2
        cards = load_cards(cx, args.lemma or None, include_rejected=bool(args.include_rejected))
        eps = {int(r["id"]): r["duration_ms"] for r in cx.execute("SELECT id, duration_ms FROM episodes")}
    finally:
        cx.close()
    plan = build_plan(cards)
    # remember how many alternates were tried (for the reject note)
    recs = load_verdict_records()
    tried = Counter((r["card_id"]) for r in recs if r["role"] == "alt")
    for p in plan:
        p["all_alts"] = [None] * tried.get(int(p["card"]["id"]), 0)
    if args.limit:
        plan = plan[: args.limit]
    print(_summary(plan))
    OUT.mkdir(parents=True, exist_ok=True)
    PLAN.write_text(json.dumps([{k: v for k, v in p.items() if k != "all_alts"} for p in plan],
                               ensure_ascii=False, indent=1, default=str))
    print(f"plan → {PLAN}")

    rejected = [p for p in plan if p["action"] == "reject"]
    for p in rejected[:12]:
        pv = p["primary"]["verdict"]
        print(f"  - REJECT {p['card']['lemma']:<6} 「{p['card']['text'][:34]}」 "
              f"{pv.get('inferability')} {float(pv.get('confidence') or 0):.2f} guess '{pv.get('best_guess')}' → {pv.get('match')}")
    for p in [x for x in plan if x["action"] == "swap"][:6]:
        print(f"  - SWAP   {p['card']['lemma']:<6} → line {p['keep']['line_id']} ({p['keep'].get('show')})")
    for p in [x for x in plan if x["action"] in ("restore", "restore_swap")][:8]:
        v = p["keep"]["verdict"]
        print(f"  - {p['action'].upper():<12} {p['card']['lemma']:<6} line {p['keep']['line_id']} "
              f"{v.get('inferability')} {float(v.get('confidence') or 0):.2f} passes={v.get('context_passes')}")
    if not args.apply:
        print("dry run — nothing written (pass --apply)")
        return 0

    cx = connect()
    try:
        bpath = _backup(cx)
    finally:
        cx.close()
    print(f"backup → {bpath}")

    counts = Counter()
    t0 = time.time()
    for i, p in enumerate(plan, start=1):
        try:
            if p["action"] == "keep":
                msg = apply_keep(p, eps)
            elif p["action"] == "swap":
                msg = apply_swap(p)
            elif p["action"] == "reject":
                msg = apply_reject(p)
            elif p["action"] in ("restore", "restore_swap"):
                msg = apply_restore(p, eps)
            elif p["action"] == "leave":
                msg = "left rejected"
            else:
                msg = f"skipped ({p.get('reason')})"
            counts[p["action"]] += 1
            for a in p.get("siblings") or []:
                smsg = apply_sibling(p, a)
                counts["sibling_created" if smsg.startswith("sibling #") else "sibling_skipped"] += 1
                msg += f"; {smsg}"
        except Exception as e:
            counts["error"] += 1
            msg = f"ERROR {type(e).__name__}: {e}"
        if p["action"] != "keep" or "re-cut" in msg or i % 100 == 0:
            print(f"  [{i}/{len(plan)}] {p['card']['lemma']:<6} {msg}")
    print(f"applied in {time.time() - t0:.0f}s: {dict(counts)}")
    return 1 if counts["error"] else 0


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------

def cmd_report(_args) -> int:
    recs = load_verdict_records()
    if not recs:
        print("no verdicts yet")
        return 0
    c = Counter()
    inf = Counter()
    for r in recs:
        v = r["verdict"]
        key = "pass" if v.get("passed") else ("error" if v.get("error") else "fail")
        c[f"{r['role']}:{key}"] += 1
        if not v.get("error"):
            inf[f"{r['role']}:{v.get('inferability')}/{v.get('match')}"] += 1
    prim = [r for r in recs if r["role"] == "primary"]
    n_pass = sum(1 for r in prim if r["verdict"].get("passed"))
    print(f"verdicts={len(recs)} primaries={len(prim)} primary_pass={n_pass} "
          f"({n_pass / max(1, len(prim)):.0%})")
    print("counts:", dict(c))
    print("inferability/match:", dict(sorted(inf.items(), key=lambda x: -x[1])))
    words = {r["card_id"] for r in recs}
    rescued = 0
    for cid in words:
        p = next((r for r in recs if r["card_id"] == cid and r["role"] == "primary"), None)
        if p and not p["verdict"].get("passed") and any(
                r["verdict"].get("passed") for r in recs if r["card_id"] == cid and r["role"] == "alt"):
            rescued += 1
    print(f"words whose primary failed but an alternate passes: {rescued}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    j = sub.add_parser("judge")
    j.add_argument("--workers", type=int, default=24)
    j.add_argument("--alternates", type=int, default=3)
    j.add_argument("--limit", type=int, default=0)
    j.add_argument("--lemma", default="")
    j.add_argument("--failed-only", action="store_true",
                   help="re-judge every pair whose latest verdict did not pass (after a judge/context change)")
    a = sub.add_parser("apply")
    a.add_argument("--apply", action="store_true", help="write (default: dry run)")
    a.add_argument("--limit", type=int, default=0)
    a.add_argument("--lemma", default="")
    a.add_argument("--include-rejected", action="store_true",
                   help="also plan rejected cards: restore when their moment (or an alternate) now passes")
    sub.add_parser("report")
    args = ap.parse_args()
    return {"judge": cmd_judge, "apply": cmd_apply, "report": cmd_report}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
