#!/usr/bin/env python3
"""Decide, per curated card, which CONTEXT lines the meaning depends on.

The user (2026-09-03): "If you're explaining context with the messages that are NOT in
the sentence, we should extend the period in this case." So for every card we ask
Sonnet: given the target line, its neighbouring lines (with ids) and the judge's
`why_clear`, which neighbouring lines must the learner hear for the meaning to be
unmistakable? Writes `evidence_line_ids` (ordered, may be empty) and
`evidence_reason` into data/srs-curation/cards.json (backup kept).

Usage:
  .venv/bin/python tools/srs_evidence_lines.py --dry-run      # show what would be asked
  .venv/bin/python tools/srs_evidence_lines.py [--workers 6]  # annotate cards.json in place
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CARDS = ROOT / "data" / "srs-curation" / "cards.json"
BIDI_RE = re.compile("[‎‏‪-‮⁦-⁩]")
EVIDENCE_MODEL = "claude-opus-5"

SYSTEM = (
    "You curate video sentence cards for a Japanese learner. A card shows ONE subtitle line "
    "(the target) with one highlighted word, plus a short video clip. A reviewer wrote WHY the "
    "word's meaning is clear from the scene. Your job: decide which NEIGHBOURING lines (if any) "
    "the learner must actually hear/see for that explanation to hold — i.e. lines the explanation "
    "relies on, or lines without which the target sentence alone would be ambiguous. "
    "Return ONLY neighbouring line ids that are genuinely needed (fewer is better); return an "
    "empty list when the target line by itself (plus the visible situation) carries the meaning. "
    "Prefer lines immediately before the target; include a following line only when the "
    "explanation depends on the reply/continuation. Never include the target line itself. "
    "Return JSON {\"evidence_line_ids\": [<ids>], \"reason\": \"<one short sentence>\"}."
)
SCHEMA = {
    "type": "object",
    "properties": {
        "evidence_line_ids": {"type": "array", "items": {"type": "integer"}},
        "reason": {"type": "string"},
    },
    "required": ["evidence_line_ids", "reason"],
    "additionalProperties": False,
}


def build_user(card: dict, ctx: list[dict]) -> str:
    lines = []
    for c in ctx:
        mark = ">> TARGET" if c.get("is_target") else "  "
        t = BIDI_RE.sub("", c["text"])
        tr = c.get("translation") or ""
        lines.append(f"[id {c['line_id']}] {mark} {t}" + (f"   ({tr})" if tr else ""))
    return (
        f"Target word: {card['lemma']} ({card['reading']}) — {card['meaning_short']}\n\n"
        f"Lines in order (ids in brackets; the target is marked):\n" + "\n".join(lines) +
        f"\n\nReviewer's explanation of why the meaning is clear:\n{card['why_clear']}\n\n"
        "Which neighbouring line ids must the learner hear for this explanation to hold?"
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    doc = json.loads(CARDS.read_text())
    cards = doc["cards"]
    ctx_by_line: dict[int, list[dict]] = {}
    for cp in sorted(CARDS.parent.glob("candidates*.json")):
        for w in json.loads(cp.read_text())["words"]:
            for m in w["moments"]:
                ctx_by_line.setdefault(m["line_id"], m.get("context", []))

    todo = [c for c in cards if "evidence_line_ids" not in c]
    if args.limit:
        todo = todo[: args.limit]
    print(f"cards={len(cards)} to_annotate={len(todo)}")
    if args.dry_run:
        c = todo[0]
        print(build_user(c, ctx_by_line.get(c["line_id"], [])))
        return 0

    from app import llm
    from app.config import settings

    def one(card: dict):
        ctx = ctx_by_line.get(card["line_id"]) or []
        valid = {c["line_id"] for c in ctx if not c.get("is_target")}
        if not valid:
            return [], "no context lines available"
        # Opus 5 per the user (2026-09-03); adaptive thinking is on by default and its
        # tokens count against max_tokens, so leave generous headroom.
        res = llm.claude_json(SYSTEM, build_user(card, ctx), model=EVIDENCE_MODEL,
                              schema=SCHEMA, max_tokens=2500, timeout=120.0)
        if not isinstance(res, dict):
            return None, None
        ids = [int(i) for i in (res.get("evidence_line_ids") or []) if int(i) in valid]
        # keep chronological order as in ctx
        order = [c["line_id"] for c in ctx]
        ids = sorted(set(ids), key=order.index)
        return ids, str(res.get("reason") or "").strip()

    backup = CARDS.with_suffix(f".pre-evidence-{time.strftime('%Y%m%d-%H%M%S')}.json")
    shutil.copy(CARDS, backup)
    t0 = time.time()
    done = failed = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(one, c): c for c in todo}
        for fut in as_completed(futs):
            c = futs[fut]
            try:
                ids, reason = fut.result()
            except Exception as e:
                ids, reason = None, str(e)
            if ids is None:
                failed += 1
                continue
            c["evidence_line_ids"] = ids
            c["evidence_reason"] = reason
            done += 1
            if (done + failed) % 100 == 0:
                print(f"  {done + failed}/{len(todo)} ({time.time() - t0:.0f}s)")
    doc["stats"]["evidence_annotated"] = sum(1 for c in cards if "evidence_line_ids" in c)
    doc["stats"]["evidence_failed"] = failed
    CARDS.write_text(json.dumps(doc, ensure_ascii=False, indent=1))
    with_ev = [c for c in cards if c.get("evidence_line_ids")]
    print(f"annotated={done} failed={failed} in {time.time() - t0:.0f}s; cards needing context: "
          f"{len(with_ev)}/{len(cards)} → {CARDS} (backup {backup.name})")
    from collections import Counter
    print("evidence line count histogram:", Counter(len(c.get("evidence_line_ids") or []) for c in cards))
    for c in with_ev[:6]:
        print(f"- {c['lemma']} 「{c['text']}」 needs {c['evidence_line_ids']}: {c['evidence_reason']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
