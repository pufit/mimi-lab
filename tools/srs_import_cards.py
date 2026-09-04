#!/usr/bin/env python
"""Import the curated initial deck into the SRS (design §5.11, amendments §B).

    .venv/bin/python tools/srs_import_cards.py [--path data/srs-curation/cards.json] [--dry-run]

Runs in-process against the same DB as the server (`MIMI_LAB_DB`), so it works
before the new server is restarted: `srs_clip` jobs enqueued while the running
server does not yet register that type are *parked*, not failed, and
`unpark_jobs()` requeues them at the next boot.

Idempotent: re-running reports every card as `skipped_existing`.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    ap = argparse.ArgumentParser(description="import the curated SRS deck")
    ap.add_argument("--path", default="data/srs-curation/cards.json",
                    help="curation file (relative paths resolve under the repo root)")
    ap.add_argument("--dry-run", action="store_true", help="parse and count only, write nothing")
    args = ap.parse_args()

    path = Path(args.path)
    if not path.is_absolute():
        path = ROOT / path
    if not path.exists():
        print(f"not found: {path}", file=sys.stderr)
        return 2

    doc = json.loads(path.read_text(encoding="utf-8"))
    cards = doc.get("cards") or []
    print(f"{path.name}: {len(cards)} cards (generated_at={doc.get('generated_at')})")
    if args.dry_run:
        return 0

    from app.db import init_db
    from app.srs.importer import import_cards

    init_db()
    report = import_cards(doc)
    print(
        f"created={report.created} skipped_existing={report.skipped_existing} "
        f"clip_jobs_queued={report.clip_jobs_queued} errors={len(report.errors)}"
    )
    for err in report.errors[:20]:
        print(f"  ! {err.lemma}: {err.reason}")
    if len(report.errors) > 20:
        print(f"  … and {len(report.errors) - 20} more")
    return 0


if __name__ == "__main__":
    sys.exit(main())
