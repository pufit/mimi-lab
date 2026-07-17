"""One-off maintenance: re-ingest every cached Japanese subtitle whose stored
text still carries broadcast-caption artifacts (（speaker）/《narration》/→/♪/
furigana/full-width spacing), now that `clean_ja_caption` strips them.

Idempotent + resumable: it only targets rows that still have dirty lines, so a
re-run continues where a crash left off (already-clean rows are skipped). Per-row
best-effort — one unreadable file never aborts the run. Recomputes comprehension
per healed episode so library-wide scores reflect the cleaned corpus.

Run:  .venv/bin/python -m tools.reclean_ja_corpus
Progress is appended to data/reclean_ja_corpus.log and printed to stdout.
"""
from __future__ import annotations

import time
from pathlib import Path

from app.db import connect
from app.subs import service as S
from app.learn import service as L

DIRTY = "*[《》〈〉→⟶⇒⇨➡➔⤴⤵♩♪♫♬〽【】（）　]*"  # GLOB of artifact chars
LOG = Path(__file__).resolve().parent.parent / "data" / "reclean_ja_corpus.log"


def _log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def main() -> int:
    with connect() as cx:
        rows = cx.execute(
            "SELECT s.id, s.episode_id, s.source, s.jimaku_entry_id, s.jimaku_file_id, "
            "       s.jimaku_filename, s.aligned, s.align_tool, s.path "
            "FROM subtitles s "
            "WHERE COALESCE(s.lang,'ja')<>'en' AND EXISTS ("
            "  SELECT 1 FROM subtitle_lines sl "
            f"  WHERE sl.subtitle_id=s.id AND sl.text GLOB '{DIRTY}') "
            "ORDER BY s.id"
        ).fetchall()

    total = len(rows)
    _log(f"=== reclean start: {total} dirty JA subtitle rows ===")
    done = missing = failed = comp_ok = 0
    seen_eps: set[int] = set()
    t0 = time.monotonic()

    for i, r in enumerate(rows, 1):
        path = r["path"]
        if not path or not Path(path).exists():
            missing += 1
            continue
        try:
            S.ingest_subtitle(
                r["episode_id"], path,
                source=r["source"] or "jimaku",
                jimaku_entry_id=r["jimaku_entry_id"],
                jimaku_file_id=r["jimaku_file_id"],
                jimaku_filename=r["jimaku_filename"],
                aligned=r["aligned"] or 0,
                align_tool=r["align_tool"],
            )
            done += 1
            # recompute comprehension once per episode (best-effort)
            if r["episode_id"] not in seen_eps:
                seen_eps.add(r["episode_id"])
                try:
                    L.comprehension_aligned(r["episode_id"])
                    comp_ok += 1
                except Exception as e:
                    _log(f"  comp failed ep {r['episode_id']}: {e}")
        except Exception as e:
            failed += 1
            _log(f"  ingest failed row {r['id']} ({path}): {e}")

        if i % 50 == 0 or i == total:
            el = time.monotonic() - t0
            rate = i / el if el else 0
            eta = (total - i) / rate if rate else 0
            _log(f"  {i}/{total}  done={done} missing={missing} failed={failed} "
                 f"comp={comp_ok}  {rate:.1f}/s  ETA {eta/60:.1f}m")

    el = time.monotonic() - t0
    _log(f"=== RECLEAN DONE: processed={total} healed={done} missing={missing} "
         f"failed={failed} comp_recomputed={comp_ok} in {el/60:.1f}m ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
