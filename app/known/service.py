"""Known-words layer: receive Migaku's WordList from the Connector into our
`known_words` cache and summarize it (notes/REMOTE_PLAYBACK_DESIGN.md §3, P2).

Migaku is the authoritative source: status is its own enum
(KNOWN | LEARNING | UNKNOWN | IGNORED). "Known for comprehension" == KNOWN;
IGNORED is excluded from the comprehension denominator (handled in learn).

Topology note: the WordList lives in Migaku's IndexedDB on the *viewing* machine,
so the Connector reads it and **pushes** it here (POST /api/known/upload). The
"Sync from Migaku" button asks the connected Connector to push a fresh copy
(`request_sync` → WS `sync-known`).
"""
from __future__ import annotations

import logging

import jaconv

from ..db import connect, cursor
from ..models import KnownWordsSummary

log = logging.getLogger("mimi_lab.known")

_VALID = {"KNOWN", "LEARNING", "UNKNOWN", "IGNORED"}


def _norm_status(s: str) -> str:
    s = (s or "").upper().strip()
    return s if s in _VALID else "UNKNOWN"


def upload_known_words(rows: list[dict]) -> dict:
    """Replace the known-set with a pushed WordList, keyed (dict_form, reading).

    `rows` is the Connector's decoded WordList: [{dictForm, reading, knownStatus}]
    (also tolerates dict_form/secondary/status aliases). Returns a sync summary
    {written, removed, counts}.

    FULL-REPLACE semantics: the Connector always pushes the complete WordList,
    so rows we hold that are absent from the push are words the user deleted /
    reset in Migaku — they used to linger forever (upsert-only), silently
    skewing comprehension upward. Guarded: a push that would shrink the set by
    more than half is treated as a truncated/broken read and only upserts.
    """
    parsed: list[tuple[str, str, str]] = []
    counts: dict[str, int] = {}
    for w in rows or []:
        dict_form = (w.get("dictForm") or w.get("dict_form") or "").strip()
        if not dict_form:
            continue
        reading = jaconv.kata2hira((w.get("reading") or w.get("secondary") or "").strip())
        status = _norm_status(w.get("knownStatus") or w.get("status") or "")
        parsed.append((dict_form, reading, status))
        counts[status] = counts.get(status, 0) + 1

    removed = 0
    with cursor() as cx:
        cx.executemany(
            "INSERT INTO known_words(dict_form,reading,status,source,updated_at) "
            "VALUES(?,?,?, 'migaku', datetime('now')) "
            "ON CONFLICT(dict_form,reading) DO UPDATE SET "
            "status=excluded.status, source='migaku', updated_at=datetime('now')",
            parsed,
        )
        existing = cx.execute("SELECT COUNT(*) AS n FROM known_words").fetchone()["n"]
        if parsed and len(parsed) >= existing / 2:
            keys = {(d, r) for d, r, _ in parsed}
            stale = cx.execute("SELECT dict_form, reading FROM known_words").fetchall()
            to_del = [(row["dict_form"], row["reading"]) for row in stale
                      if (row["dict_form"], row["reading"]) not in keys]
            if to_del:
                cx.executemany(
                    "DELETE FROM known_words WHERE dict_form=? AND reading=?", to_del
                )
                removed = len(to_del)
        elif parsed:
            log.warning("known upload: push (%d) is <50%% of existing (%d) — "
                        "skipping tombstone pass (truncated read?)", len(parsed), existing)
    written = len(parsed)
    log.info("known upload: wrote %d words, removed %d stale, %s", written, removed, counts)
    # snapshot the new known-set for growth tracking; if the KNOWN count moved,
    # re-rank the backlog so comprehension reflects what the user now knows.
    changed = _capture_snapshot() or removed > 0
    if changed:
        try:
            from ..jobs.service import enqueue
            enqueue("comprehension_recompute_all", {})  # deduped; one bulk pass
        except Exception as e:  # pragma: no cover
            log.warning("could not enqueue re-rank after known sync: %s", e)
    # SRS reconcile runs UNCONDITIONALLY (SRS_DESIGN §9.3): `changed` only
    # reflects a KNOWN-count move, so an IGNORED-only or net-zero push would
    # otherwise wait for the daily backstop. Dedup makes a repeat free.
    try:
        from ..jobs.service import enqueue
        enqueue("srs_reconcile", {}, priority=30)
    except Exception as e:  # pragma: no cover
        log.warning("could not enqueue srs reconcile after known sync: %s", e)
    return {"written": written, "removed": removed, "counts": counts}


def _capture_snapshot() -> bool:
    """Append a known_snapshots row iff the counts changed from the last one.

    Returns True when the KNOWN count changed (so callers can trigger a re-rank).
    """
    s = summary()
    with cursor() as cx:
        last = cx.execute(
            "SELECT known, learning, unknown, ignored FROM known_snapshots "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        same = last and (
            last["known"] == s.known and last["learning"] == s.learning
            and last["unknown"] == s.unknown and last["ignored"] == s.ignored
        )
        if same:
            return False
        cx.execute(
            "INSERT INTO known_snapshots(known,learning,unknown,ignored,total) "
            "VALUES(?,?,?,?,?)",
            (s.known, s.learning, s.unknown, s.ignored, s.total),
        )
    return not last or last["known"] != s.known


def growth(days: int = 7) -> dict:
    """Vocabulary growth over the last `days`: current KNOWN vs the earliest
    snapshot on/after the window start. {known, known_prev, delta, since, days}."""
    _capture_snapshot()  # ensure a 'today' baseline exists (no-op if unchanged)
    s = summary()
    with cursor() as cx:
        prev = cx.execute(
            "SELECT known, captured_at FROM known_snapshots "
            "WHERE captured_at <= datetime('now', ?) ORDER BY id DESC LIMIT 1",
            (f"-{int(days)} days",),
        ).fetchone()
        if prev is None:
            # no snapshot older than the window — use the earliest we have
            prev = cx.execute(
                "SELECT known, captured_at FROM known_snapshots ORDER BY id ASC LIMIT 1"
            ).fetchone()
    known_prev = prev["known"] if prev else s.known
    since = prev["captured_at"] if prev else None
    return {
        "known": s.known,
        "known_prev": known_prev,
        "delta": s.known - known_prev,
        "since": since,
        "days": days,
    }


async def request_sync(device_id: str | None = None) -> dict:
    """Ask a connected Connector to push a fresh WordList, then return the
    refreshed summary.

    `device_id` targets a specific device; None uses default routing (single
    connected device → it). The Connector handles `sync-known` by reading
    Migaku's IndexedDB and POSTing to /api/known/upload before it acks; so by
    the time this returns the cache is current. Raises ConnectorOffline /
    ConnectorTimeout if it can't be reached.
    """
    from ..connector.service import registry

    ack = await registry.send_command({"cmd": "sync-known"}, timeout=120.0, device_id=device_id)
    s = summary()
    return {
        "written": (ack.get("result") or {}).get("written"),
        "counts": (ack.get("result") or {}).get("counts"),
        "summary": s.model_dump(),
        "total": s.total,
        "known": s.known,
        "learning": s.learning,
        "unknown": s.unknown,
        "ignored": s.ignored,
        "updated_at": s.updated_at,
    }


def summary() -> KnownWordsSummary:
    """Counts of known_words by status."""
    with connect() as cx:
        rows = cx.execute(
            "SELECT status, COUNT(*) AS n FROM known_words GROUP BY status"
        ).fetchall()
        total = cx.execute("SELECT COUNT(*) AS n FROM known_words").fetchone()["n"]
        updated = cx.execute("SELECT MAX(updated_at) AS u FROM known_words").fetchone()["u"]
    by = {r["status"].upper(): r["n"] for r in rows}
    return KnownWordsSummary(
        total=total,
        known=by.get("KNOWN", 0),
        learning=by.get("LEARNING", 0),
        unknown=by.get("UNKNOWN", 0),
        ignored=by.get("IGNORED", 0),
        updated_at=updated,
    )


# --------------------------------------------------------------------------
# Job handler (deprecated — known sync is now Connector-driven via the route)
# --------------------------------------------------------------------------


def _job_known_sync(payload: dict) -> None:
    """Deprecated no-op.

    Known-words now arrive by Connector push (POST /api/known/upload); a manual
    refresh goes through POST /api/known/sync, which relays `sync-known` on the
    app loop. A background job can't drive the WS (it lives on the app loop), so
    this handler just logs and returns — kept registered so stale queued rows
    don't pile up as 'no handler' errors.
    """
    log.info("known_sync job is deprecated (Connector push); ignoring payload=%s", payload)


def register_jobs() -> None:
    try:
        from ..jobs.service import register

        register("known_sync", _job_known_sync)
    except Exception as e:  # pragma: no cover
        log.warning("could not register known_sync handler: %s", e)


register_jobs()
