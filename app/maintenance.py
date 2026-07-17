"""Operational maintenance: backups, log rotation, DB hygiene, retention.

All functions are periodic-safe (idempotent, never raise out) and cheap enough
to run on schedule. Wired in app/jobs/periodics.py:

  * backup_db          nightly  — VACUUM INTO data/backups/, keep the last 7
  * rotate_logs        hourly   — copy-truncate any data/*.log over 20 MB
  * db_hygiene         weekly   — FTS orphan sweep + ANALYZE + WAL checkpoint
  * retention_sweep    daily    — old clips / staged orphans / .replaced backups
  * capture_daily_stats daily   — known-words + comprehension history rows
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from pathlib import Path

from .config import settings
from .db import connect, cursor

log = logging.getLogger("mimi_lab.maintenance")

BACKUP_KEEP = 7
LOG_ROTATE_BYTES = 20 * 1024 * 1024   # rotate when a log exceeds 20 MB
LOG_KEEP_TAIL = 5 * 1024 * 1024       # how much recent history survives into .1
CLIP_RETENTION_DAYS = 30
STAGED_RETENTION_DAYS = 30
REPLACED_RETENTION_DAYS = 7


# --------------------------------------------------------------------------- #
# backups
# --------------------------------------------------------------------------- #
def backup_db() -> dict:
    """Nightly consistent snapshot of the SQLite DB (VACUUM INTO), pruned to
    the last BACKUP_KEEP. The DB holds watch history, manual matches and the
    whole comprehension state — it had zero backups before this.
    """
    out: dict = {"ok": False}
    try:
        backups = Path(settings.mimi_lab_db).parent / "backups"
        backups.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d")
        dst = backups / f"mimi_lab-{stamp}.db"
        if dst.exists():
            out.update(ok=True, skipped="already backed up today", path=str(dst))
            return out
        t0 = time.time()
        with connect() as cx:
            cx.execute("VACUUM INTO ?", (str(dst),))
        out.update(ok=True, path=str(dst), seconds=round(time.time() - t0, 1),
                   size_bytes=dst.stat().st_size)
        # prune old backups (keep newest BACKUP_KEEP by name — names sort by date)
        snaps = sorted(backups.glob("mimi_lab-*.db"))
        for old in snaps[:-BACKUP_KEEP]:
            old.unlink(missing_ok=True)
            out.setdefault("pruned", []).append(old.name)
        log.info("backup_db: %s", out)
    except Exception as e:
        out["error"] = str(e)
        log.warning("backup_db failed: %s", e)
    return out


# --------------------------------------------------------------------------- #
# log rotation (copy-truncate — launchd holds the fd in append mode, so
# truncation is safe: O_APPEND writes continue at the new EOF)
# --------------------------------------------------------------------------- #
def rotate_logs() -> dict:
    rotated: list[str] = []
    try:
        data_dir = Path(settings.mimi_lab_db).parent
        for logfile in data_dir.glob("*.log"):
            try:
                size = logfile.stat().st_size
                if size < LOG_ROTATE_BYTES:
                    continue
                keep_from = max(0, size - LOG_KEEP_TAIL)
                with open(logfile, "rb") as f:
                    f.seek(keep_from)
                    tail = f.read()
                prev = logfile.with_suffix(logfile.suffix + ".1")
                prev.write_bytes(tail)
                with open(logfile, "r+b") as f:
                    f.truncate(0)
                rotated.append(f"{logfile.name} ({size >> 20}MB)")
            except Exception as e:
                log.warning("rotate %s failed: %s", logfile.name, e)
        if rotated:
            log.info("rotate_logs: %s", ", ".join(rotated))
    except Exception as e:
        log.warning("rotate_logs failed: %s", e)
    return {"rotated": rotated}


# --------------------------------------------------------------------------- #
# DB hygiene
# --------------------------------------------------------------------------- #
def db_hygiene() -> dict:
    """Weekly: purge FTS rows orphaned by non-pipeline deletions (4 existed in
    production — subtitle_fts has no triggers), refresh planner stats, and
    checkpoint the WAL."""
    out: dict = {}
    try:
        with cursor() as cx:
            cur = cx.execute(
                "DELETE FROM subtitle_fts WHERE line_id NOT IN (SELECT id FROM subtitle_lines)"
            )
            out["fts_orphans_removed"] = cur.rowcount or 0
        with connect() as cx:
            cx.execute("ANALYZE")
            cx.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            cx.execute("PRAGMA optimize")
        out["ok"] = True
        log.info("db_hygiene: %s", out)
    except Exception as e:
        out["error"] = str(e)
        log.warning("db_hygiene failed: %s", e)
    return out


# --------------------------------------------------------------------------- #
# file retention
# --------------------------------------------------------------------------- #
def _prune_older_than(pattern_dir: Path, glob: str, days: int, out: dict, key: str,
                      keep_if=None) -> None:
    if not pattern_dir.exists():
        return
    cutoff = time.time() - days * 86400
    for f in pattern_dir.glob(glob):
        try:
            if not f.is_file() or f.stat().st_mtime >= cutoff:
                continue
            if keep_if and keep_if(f):
                continue
            f.unlink()
            out[key] = out.get(key, 0) + 1
        except Exception:
            pass


def retention_sweep() -> dict:
    """Daily: clips older than 30d (regenerable on demand), staged processed
    files whose match-queue item is gone (previously orphaned forever), and
    week-old .replaced.mp4 organize() backups."""
    out: dict = {}
    try:
        _prune_older_than(Path(settings.clips_dir), "line_*.*", CLIP_RETENTION_DAYS, out, "clips")

        staged = Path(settings.inbox_dir) / "processed"
        referenced: set[str] = set()
        try:
            with connect() as cx:
                rows = cx.execute(
                    "SELECT processed_path FROM match_queue "
                    "WHERE processed_path IS NOT NULL AND state='pending'"
                ).fetchall()
            referenced = {r["processed_path"] for r in rows if r["processed_path"]}
        except Exception:
            pass
        _prune_older_than(staged, "*.mp4", STAGED_RETENTION_DAYS, out, "staged_orphans",
                          keep_if=lambda f: str(f) in referenced)

        _prune_older_than(Path(settings.library_dir), "**/*.replaced.mp4",
                          REPLACED_RETENTION_DAYS, out, "replaced_backups")
        if out:
            log.info("retention_sweep: %s", out)
        out["ok"] = True
    except Exception as e:
        out["error"] = str(e)
        log.warning("retention_sweep failed: %s", e)
    return out


# --------------------------------------------------------------------------- #
# daily learning stats (the Stats dashboard's time series)
# --------------------------------------------------------------------------- #
def capture_comprehension_history() -> dict:
    """Insert a comprehension_history row from the episodes table. Called daily
    and after each library-wide recompute; episodes.comprehension_pct is
    overwritten in place, so this table is the only trend record."""
    out: dict = {"ok": False}
    try:
        with cursor() as cx:
            row = cx.execute(
                "SELECT COUNT(*) scored, AVG(comprehension_pct) avg_pct, "
                "SUM(comprehension_pct >= 80 AND comprehension_pct < 95) sweet, "
                "SUM(comprehension_pct >= 65 AND comprehension_pct < 80) almost, "
                "SUM(comprehension_pct >= 95) easy, "
                "SUM(comprehension_pct < 65) hard "
                "FROM episodes WHERE comprehension_pct IS NOT NULL"
            ).fetchone()
            if not row or not row["scored"]:
                out["skipped"] = "nothing scored"
                return out
            cx.execute(
                "INSERT INTO comprehension_history"
                "(scored_episodes, avg_pct, sweet_count, almost_count, easy_count, hard_count) "
                "VALUES(?,?,?,?,?,?)",
                (row["scored"], round(row["avg_pct"] or 0, 2), row["sweet"] or 0,
                 row["almost"] or 0, row["easy"] or 0, row["hard"] or 0),
            )
        out["ok"] = True
    except Exception as e:
        out["error"] = str(e)
        log.warning("capture_comprehension_history failed: %s", e)
    return out


def capture_daily_stats() -> dict:
    """Daily periodic: known-words snapshot (deduped inside) + comprehension
    history row. This keeps Stats charts current even when Connector uploads
    are infrequent."""
    out = {"comprehension": capture_comprehension_history()}
    try:
        from .known.service import _capture_snapshot
        out["known_changed"] = _capture_snapshot()
    except Exception as e:
        out["known_error"] = str(e)
    return out
