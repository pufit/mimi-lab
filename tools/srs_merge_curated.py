#!/usr/bin/env python3
"""Merge the agent-curated batches (judged/ + refuted/) into the initial deck.

Reads data/srs-curation/{candidates.json, judged/batch-*.json, refuted/batch-*.json}
and writes:
  data/srs-curation/cards.json         the import contract for the SRS (see below)
  data/srs-curation/cards_preview.md   human-readable preview + stats
  data/srs-curation/rejections.json    every rejected/dropped word with the reason

cards.json = {"generated_at", "source", "stats", "cards": [card...]} where card =
  lemma, reading, pos, meaning_short, meaning_full, why_clear, usage_note,
  line_id, episode_id, anilist_id, show, ep_number, start_ms, end_ms, text, translation,
  target_surface, alt_line_ids[], clarity, usefulness, priority (1-5), tags[],
  jpdb_rank, occurrences, episodes, leverage_crossings,
  judge_clarity, refuter_clarity, verdict (keep|swap|rescued), stack_score, stack_rank (1 = top)

Rules: a judge card survives only with a refuter verdict keep/swap; swap moves it
to swap_line_id (must be one of the word's candidate moments); refuter "fixes"
override prose fields; refuter rescues are added as-is (verdict=rescued);
target_surface must be an exact substring of the line text (else we repair from the
candidate moment's own target_surface, else the card is dropped as invalid).
"""
from __future__ import annotations

import json
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASE = ROOT / "data" / "srs-curation"


def load(p: Path):
    try:
        return json.loads(p.read_text())
    except FileNotFoundError:
        return None
    except json.JSONDecodeError as e:
        print(f"!! bad JSON in {p}: {e}")
        return None


def main() -> int:
    words: dict[str, dict] = {}
    moments_by_line = {}
    for cp in sorted(BASE.glob("candidates*.json")):      # main pool + supplementary runs
        cand = load(cp)
        for w in cand["words"]:
            words.setdefault(w["lemma"], w)
            for m in w["moments"]:
                moments_by_line[(w["lemma"], m["line_id"])] = m

    batches = sorted(p.name for p in (BASE / "batches").glob("batch-*.json"))
    cards: dict[str, dict] = {}
    rejections: list[dict] = []
    stats = Counter()
    missing = []

    def finalize(lemma: str, card: dict, verdict: str, judge_cl, ref_cl) -> bool:
        w = words.get(lemma)
        if not w:
            stats["unknown_lemma"] += 1
            rejections.append({"lemma": lemma, "reason_category": "invalid", "reason": "lemma not in candidates"})
            return False
        m = moments_by_line.get((lemma, card.get("line_id")))
        if not m:
            stats["invalid_line"] += 1
            rejections.append({"lemma": lemma, "reason_category": "invalid",
                               "reason": f"line_id {card.get('line_id')} is not a candidate moment"})
            return False
        ts = card.get("target_surface") or ""
        if not ts or ts not in m["text"]:
            if m.get("target_surface") and m["target_surface"] in m["text"]:
                ts = m["target_surface"]
                stats["surface_repaired"] += 1
            else:
                stats["invalid_surface"] += 1
                rejections.append({"lemma": lemma, "reason_category": "invalid",
                                   "reason": f"target_surface {ts!r} not in line text"})
                return False
        clarity = float(card.get("clarity") or 0)
        usefulness = float(card.get("usefulness") or 0)
        priority = int(card.get("priority") or 3)
        priority = max(1, min(5, priority))
        stack_score = priority * 10 + usefulness * 5 + clarity * 3 + min(w["score"], 30) / 10
        out = {
            "lemma": lemma,
            "reading": card.get("reading") or w["reading"],
            "pos": card.get("pos") or "",
            "meaning_short": (card.get("meaning_short") or "").strip(),
            "meaning_full": (card.get("meaning_full") or w.get("gloss_jmdict") or "").strip(),
            "why_clear": (card.get("why_clear") or "").strip(),
            "usage_note": (card.get("usage_note") or "").strip(),
            "line_id": m["line_id"], "episode_id": m["episode_id"], "anilist_id": m["anilist_id"],
            "show": m["show"], "ep_number": m["ep_number"],
            "start_ms": m["start_ms"], "end_ms": m["end_ms"],
            "text": m["text"], "translation": m["translation"],
            "target_surface": ts,
            "alt_line_ids": [x for x in (card.get("alt_line_ids") or [])
                             if (lemma, x) in moments_by_line and x != m["line_id"]],
            "clarity": clarity, "usefulness": usefulness, "priority": priority,
            "tags": list(card.get("tags") or []),
            "jpdb_rank": w["jpdb_rank"], "occurrences": w["occurrences"], "episodes": w["episodes"],
            "leverage_crossings": w["leverage_crossings"],
            "judge_clarity": judge_cl, "refuter_clarity": ref_cl, "verdict": verdict,
            "stack_score": round(stack_score, 3),
        }
        if not out["meaning_short"]:
            stats["missing_meaning_short"] += 1
        prev = cards.get(lemma)
        if prev is None or out["clarity"] > prev["clarity"]:
            cards[lemma] = out
        else:
            stats["dup_lemma"] += 1
        return True

    for name in batches:
        b = name[len("batch-"):-len(".json")]
        judged = load(BASE / "judged" / name)
        refuted = load(BASE / "refuted" / name)
        if judged is None or refuted is None:
            missing.append(b)
            continue
        stats["batches"] += 1
        for r in judged.get("rejected") or []:
            rejections.append({"lemma": r.get("lemma"), "reason_category": r.get("reason_category"),
                               "reason": r.get("reason"), "stage": "judge"})
            stats[f"judge_rejected:{r.get('reason_category')}"] += 1
        verdicts = {(v.get("lemma"), v.get("line_id")): v for v in refuted.get("verdicts") or []}
        by_lemma = {v.get("lemma"): v for v in refuted.get("verdicts") or []}
        for c in judged.get("cards") or []:
            v = verdicts.get((c.get("lemma"), c.get("line_id"))) or by_lemma.get(c.get("lemma"))
            if v is None:
                stats["no_verdict"] += 1
                rejections.append({"lemma": c.get("lemma"), "reason_category": "no_verdict",
                                   "reason": "refuter gave no verdict", "stage": "refute"})
                continue
            verdict = (v.get("verdict") or "").lower()
            fixes = v.get("fixes") or {}
            card = dict(c)
            for k in ("reading", "meaning_short", "meaning_full", "usage_note"):
                if fixes.get(k):
                    card[k] = fixes[k]
            if verdict == "drop":
                stats["dropped"] += 1
                rejections.append({"lemma": c.get("lemma"), "reason_category": "refuted",
                                   "reason": v.get("reason"), "stage": "refute", "line_id": c.get("line_id")})
                continue
            if verdict == "swap" and v.get("swap_line_id"):
                card["line_id"] = v["swap_line_id"]
                card["target_surface"] = v.get("swap_target_surface") or ""
                stats["swapped"] += 1
            elif verdict == "swap":
                verdict = "keep"
            if verdict not in ("keep", "swap"):
                stats["bad_verdict"] += 1
                continue
            if finalize(c.get("lemma"), card, verdict, c.get("clarity"), v.get("clarity_second_opinion")):
                stats["kept"] += 1
        for c in refuted.get("rescued") or []:
            if finalize(c.get("lemma"), c, "rescued", None, c.get("clarity")):
                stats["rescued"] += 1

    ordered = sorted(cards.values(), key=lambda c: (-c["stack_score"], c["jpdb_rank"] or 10**9))
    for i, c in enumerate(ordered, 1):
        c["stack_rank"] = i

    out = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "source": "agent-curated initial deck: per-batch judge + adversarial refuter (Nerve, 2026-09-03)",
        "stats": {**dict(stats), "cards": len(ordered), "missing_batches": missing},
        "cards": ordered,
    }
    (BASE / "cards.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))
    (BASE / "rejections.json").write_text(json.dumps(rejections, ensure_ascii=False, indent=1))

    lines = [f"# Initial deck preview — {len(ordered)} cards ({time.strftime('%Y-%m-%d %H:%M')})", ""]
    lines.append("## Stats")
    for k, v in sorted(out["stats"].items()):
        lines.append(f"- {k}: {v}")
    lines += ["", "## Top 60 cards (stack order)", ""]
    for c in ordered[:60]:
        lines.append(f"{c['stack_rank']}. **{c['lemma']}** [{c['reading']}] — {c['meaning_short']}  "
                     f"(p{c['priority']}, clarity {c['clarity']:.2f}/{c['refuter_clarity']}, rank {c['jpdb_rank']}, {c['show']} E{c['ep_number']})  ")
        lines.append(f"   「{c['text']}」 — {c['translation']}  ")
        lines.append(f"   _why clear:_ {c['why_clear']}")
    lines += ["", "## Shows", ""]
    for show, n in Counter(c["show"] for c in ordered).most_common():
        lines.append(f"- {show}: {n}")
    lines += ["", "## Priority histogram", ""]
    for p, n in sorted(Counter(c["priority"] for c in ordered).items(), reverse=True):
        lines.append(f"- p{p}: {n}")
    (BASE / "cards_preview.md").write_text("\n".join(lines) + "\n")
    print(json.dumps(out["stats"], ensure_ascii=False, indent=1))
    print(f"wrote {len(ordered)} cards → {BASE/'cards.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
