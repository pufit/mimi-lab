"""In-app events / notifications backend.

A thin recorder over the `events` table (see app/db.py). The auto-pipeline
records one event per step (download / match / subtitle / comprehension / ...)
so the UI can show a live activity feed + an unread badge.

    from app.events import service as events
    events.record("download", "Downloaded Foo - 01", "success")
    events.list_events(limit=50, unread_only=True)
    events.unread_count()
    events.mark_read(all=True)

`record` is intentionally never-raising: a pipeline step must not fail just
because event recording hit a DB hiccup.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

from ..db import connect, cursor

log = logging.getLogger("mimi_lab.events")

# kinds recognised by the UI; anything else falls back to 'info'
_KINDS = {"info", "success", "warning", "error"}


def record(
    category: str,
    title: str,
    kind: str = "info",
    detail: Optional[str] = None,
    meta: Optional[dict] = None,
) -> Optional[int]:
    """Insert a row into `events`. Returns the new event id (or None on failure).

    Best-effort: never raises — recording an event must not break a pipeline.
    """
    k = kind if kind in _KINDS else "info"
    meta_json = None
    if meta is not None:
        try:
            meta_json = json.dumps(meta, ensure_ascii=False)
        except Exception:
            meta_json = None
    try:
        with cursor() as cx:
            cur = cx.execute(
                "INSERT INTO events(kind,category,title,detail,meta_json) "
                "VALUES(?,?,?,?,?)",
                (k, category, title, detail, meta_json),
            )
            new_id = cur.lastrowid
        # nudge connected browsers to refresh (bell + any category-affected list)
        try:
            from . import bus
            bus.publish("event", {"category": category, "kind": k})
        except Exception:
            pass
        return new_id
    except Exception as e:  # pragma: no cover
        log.warning("events.record failed (%s/%s): %s", category, title, e)
        return None


def _row_to_dict(row) -> dict:
    return {
        "id": row["id"],
        "kind": row["kind"],
        "category": row["category"],
        "title": row["title"],
        "detail": row["detail"],
        "created_at": row["created_at"],
        "read": bool(row["read"]),
    }


def list_events(limit: int = 50, unread_only: bool = False) -> list[dict]:
    """Return recent events (newest first) as plain dicts.

    Each: {id, kind, category, title, detail, created_at, read}.
    """
    sql = "SELECT id, kind, category, title, detail, created_at, read FROM events"
    if unread_only:
        sql += " WHERE read=0"
    sql += " ORDER BY id DESC LIMIT ?"
    with connect() as cx:
        rows = cx.execute(sql, (int(limit),)).fetchall()
    return [_row_to_dict(r) for r in rows]


def mark_read(ids: Optional[list[int]] = None, all: bool = False) -> int:
    """Mark events read. `all=True` marks every unread event; otherwise the
    given `ids`. Returns the number of rows updated."""
    with cursor() as cx:
        if all:
            cur = cx.execute("UPDATE events SET read=1 WHERE read=0")
            return cur.rowcount
        if ids:
            ids = [int(i) for i in ids]
            qmarks = ",".join("?" * len(ids))
            cur = cx.execute(
                f"UPDATE events SET read=1 WHERE id IN ({qmarks})", ids
            )
            return cur.rowcount
    return 0


def unread_count() -> int:
    """Count unread events."""
    with connect() as cx:
        row = cx.execute("SELECT COUNT(*) AS n FROM events WHERE read=0").fetchone()
    return int(row["n"]) if row else 0
