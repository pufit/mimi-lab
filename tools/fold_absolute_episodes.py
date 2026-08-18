#!/usr/bin/env python
"""Repair episode rows created under ABSOLUTE (cross-season) numbering.

A sequel's releases and subtitles are often numbered straight on from the
previous season (S2 episode 1 shipped as `13`), and a single jimaku entry
routinely carries BOTH conventions for the same episodes. Before the ingest
path learned to fold them (match.infer_episode_offset), every distinct number
became its own episode row — so a season could hold the same episode twice
(`1` and `13`) and strand later ones at absolute numbers with no twin.

This refreshes the affected titles' AniList metadata (the aired-episode count
is what makes folding possible), then merges the duplicates and renumbers the
stranded rows.

Dry run:  .venv/bin/python -m tools.fold_absolute_episodes
Apply:    .venv/bin/python -m tools.fold_absolute_episodes --apply
One title: ... --anilist-id <ANILIST_ID>

Rows are backed up to data/backups/ before anything is written.
"""
from __future__ import annotations

import argparse
import csv
import sys
from datetime import datetime, timezone
from pathlib import Path

from app.catalog import service as catalog
from app.config import settings
from app.db import connect, init_db

BACKUP_TABLES = ("episodes", "subtitles")


def _backup(anilist_ids: list[int]) -> list[Path]:
    """Dump every row this repair can touch, one TSV per table."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out_dir = settings.mimi_lab_db.parent / "backups"
    out_dir.mkdir(parents=True, exist_ok=True)
    qmarks = ",".join("?" * len(anilist_ids))
    written = []
    with connect() as cx:
        for table in BACKUP_TABLES:
            if table == "episodes":
                sql = f"SELECT * FROM episodes WHERE anilist_id IN ({qmarks})"
            else:
                sql = (
                    f"SELECT s.* FROM subtitles s JOIN episodes e ON e.id = s.episode_id "
                    f"WHERE e.anilist_id IN ({qmarks})"
                )
            rows = cx.execute(sql, anilist_ids).fetchall()
            path = out_dir / f"absolute-fold-{stamp}.{table}.tsv"
            with path.open("w", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh, delimiter="\t", lineterminator="\n")
                if rows:
                    w.writerow(rows[0].keys())
                    w.writerows([tuple(r) for r in rows])
            written.append(path)
            print(f"  backed up {len(rows):5d} {table} row(s) -> {path}")
    return written


def _describe(plan: list[dict]) -> None:
    if not plan:
        print("\nNothing to fold — no absolute-numbered episode rows found.")
        return
    print(f"\n{'title':>8}  {'action':<9} {'ep':>4} -> {'ep':<4} {'keep':>6} {'drop':>6}")
    print("-" * 52)
    for p in sorted(plan, key=lambda x: (x["anilist_id"], x["to"])):
        print(f"{p['anilist_id']:>8}  {p['action']:<9} {p['from']:>4} -> {p['to']:<4} "
              f"{p['keep_id']:>6} {str(p['drop_id'] or '-'):>6}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true",
                    help="write the changes (default: dry run)")
    ap.add_argument("--anilist-id", type=int, default=None,
                    help="limit the repair to one title")
    ap.add_argument("--skip-refresh", action="store_true",
                    help="do not re-fetch AniList metadata first")
    args = ap.parse_args()

    init_db()

    if not args.skip_refresh:
        print("Refreshing AniList metadata (aired-episode counts)...")
        if args.anilist_id:
            ok = catalog.refresh_title_meta(args.anilist_id)
            print(f"  {args.anilist_id}: {'refreshed' if ok else 'FAILED'}")
        else:
            res = catalog.refresh_stale_titles()
            print(f"  refreshed {res['refreshed']}/{res['checked']} title(s)")

    preview = catalog.fold_absolute_episodes(args.anilist_id, apply=False)
    _describe(preview["plan"])
    if not preview["plan"]:
        return 0

    print(f"\n{preview['merged']} merge(s), {preview['renumbered']} renumber(s) "
          f"across {len(preview['titles'])} title(s)")
    if not args.apply:
        print("\nDry run — re-run with --apply to write.")
        return 0

    print("\nBacking up affected rows...")
    _backup(preview["titles"])
    res = catalog.fold_absolute_episodes(args.anilist_id, apply=True)
    print(f"\nApplied: {res['merged']} merged, {res['renumbered']} renumbered.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
