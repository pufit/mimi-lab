#!/usr/bin/env python3
"""Trim merged English cues in data/srs-curation/cards.json before import.

English subtitle cues are often merged with a neighbouring line ("…sheets... Sh-She's
very demanding." / "…anyway. NOM NOM"). For each curated card whose translation
looks merged (heuristic), ask Haiku for the English that translates ONLY the
Japanese line; keep the original under `translation_raw`.

Usage:
  .venv/bin/python tools/srs_clean_translations.py --dry-run     # count only
  .venv/bin/python tools/srs_clean_translations.py [--workers 8]  # rewrite cards.json in place (backup kept)
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
TERM_RE = re.compile(r"[.!?…](\s|$)")
BIDI_RE = re.compile("[‎‏‪-‮⁦-⁩]")

SYSTEM = (
    "You align anime subtitles. You get ONE Japanese subtitle line, its neighbouring Japanese "
    "lines for context, and the English caption that was shown at the same time — which is often "
    "MERGED with the translation of a neighbouring line or contains stray on-screen text. "
    "Return ONLY the English that translates the target Japanese line: keep the subtitle's own "
    "wording when it is present in the caption; drop the parts that translate other lines or "
    "are not dialogue. If the caption does not translate the target line at all, translate the "
    "target line yourself, naturally and concisely. Return JSON {\"t\": \"<english>\"}."
)
SCHEMA = {"type": "object", "properties": {"t": {"type": "string"}}, "required": ["t"], "additionalProperties": False}


def looks_merged(card: dict) -> bool:
    tr = (card.get("translation") or "").strip()
    ja = BIDI_RE.sub("", card.get("text") or "")
    if not tr:
        return False
    terms = len(TERM_RE.findall(tr))
    if terms >= 2:
        return True
    if len(tr) > 2.2 * max(1, len(ja)) + 12:
        return True
    return False


def clean_one(card: dict, ctx_lines: list[str]) -> str | None:
    from app import llm
    from app.config import settings

    user = (
        "Context (Japanese, in order; the target line is marked >>):\n"
        + "\n".join(ctx_lines)
        + f"\n\nEnglish caption shown at that time (may be merged):\n{card['translation']}"
    )
    res = llm.claude_json(SYSTEM, user, model=settings.anthropic_model, schema=SCHEMA,
                          max_tokens=200, timeout=30.0)
    t = (res or {}).get("t") if isinstance(res, dict) else None
    t = str(t).strip() if t else None
    return t or None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()

    doc = json.loads(CARDS.read_text())
    cards = doc["cards"]
    flagged = [c for c in cards if looks_merged(c) and not c.get("translation_raw")]
    print(f"cards={len(cards)} flagged_merged={len(flagged)}")
    if args.dry_run:
        for c in flagged[:12]:
            print(f"- {c['lemma']}: 「{c['text']}」 → {c['translation'][:140]}")
        return 0

    # context lines come from the candidates files (moments carry ±2 context)
    ctx_by_line: dict[int, list[str]] = {}
    for cp in sorted(CARDS.parent.glob("candidates*.json")):
        for w in json.loads(cp.read_text())["words"]:
            for m in w["moments"]:
                lines = []
                for c in m.get("context", []):
                    mark = ">> " if c.get("is_target") else "   "
                    lines.append(mark + BIDI_RE.sub("", c["text"]))
                ctx_by_line.setdefault(m["line_id"], lines)

    backup = CARDS.with_suffix(f".pre-clean-{time.strftime('%Y%m%d-%H%M%S')}.json")
    shutil.copy(CARDS, backup)
    t0 = time.time()
    done = 0
    failed = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(clean_one, c, ctx_by_line.get(c["line_id"], [">> " + c["text"]])): c for c in flagged}
        for fut in as_completed(futs):
            c = futs[fut]
            try:
                t = fut.result()
            except Exception as e:  # never abort the batch
                t = None
                print(f"!! {c['lemma']}: {e}")
            if t:
                c["translation_raw"] = c["translation"]
                c["translation"] = t
                c["translation_source"] = "cleaned"
                done += 1
            else:
                failed += 1
            if (done + failed) % 50 == 0:
                print(f"  {done + failed}/{len(flagged)} ({time.time() - t0:.0f}s)")
    doc["stats"]["translations_cleaned"] = done
    doc["stats"]["translations_clean_failed"] = failed
    CARDS.write_text(json.dumps(doc, ensure_ascii=False, indent=1))
    print(f"cleaned={done} failed={failed} in {time.time() - t0:.0f}s → {CARDS} (backup {backup.name})")
    for c in [x for x in cards if x.get("translation_raw")][:8]:
        print(f"- {c['lemma']}: {c['translation_raw'][:90]!r} → {c['translation']!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
