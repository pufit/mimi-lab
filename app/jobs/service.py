"""DB-backed job queue + APScheduler periodics. Single-process, multi-worker.

    from app.jobs.service import register, enqueue, add_periodic
    register("subtitle_fetch", handle_subtitle_fetch)   # at import time
    enqueue("subtitle_fetch", {"episode_id": 12})       # from a route/pipeline

Job states: queued -> running -> done | error (terminal) | parked | cancelled.
'parked' = no handler was registered for the type when the job was claimed
(deploy drift: an old server picked up a job enqueued by newer code, or a
module failed to import). Parked jobs do NOT burn attempts; they are requeued
automatically at the next startup once their handler exists.
"""
from __future__ import annotations

import json
import logging
import threading
import traceback
from typing import Callable, Optional

from apscheduler.schedulers.background import BackgroundScheduler

from ..config import settings
from ..db import cursor

log = logging.getLogger("mimi_lab.jobs")

_handlers: dict[str, Callable[[dict], None]] = {}
_scheduler: Optional[BackgroundScheduler] = None
_workers: list[threading.Thread] = []
_stop = threading.Event()
MAX_ATTEMPTS = 5

# Default priority per job type (lower runs first). Interactive work the user is
# waiting on (comprehension pills) beats background transcodes/fetches.
_PRIORITY: dict[str, int] = {
    "comprehension": 10,
    "comprehension_exact": 10,
    "subtitle_align": 20,
    "subtitle_fetch": 50,
    "title_subtitle_fetch": 60,
    "english_subtitle_fetch": 70,
    "mal_sync": 40,
    "postprocess": 100,
}
_DEFAULT_PRIORITY = 80


def register(job_type: str, fn: Callable[[dict], None]) -> None:
    _handlers[job_type] = fn


def registered_types() -> list[str]:
    return sorted(_handlers)


def enqueue(
    job_type: str,
    payload: Optional[dict] = None,
    delay_seconds: int = 0,
    priority: Optional[int] = None,
    dedup: bool = True,
) -> None:
    """Enqueue a job. By default de-duplicates: if an identical (type, payload)
    job is already queued or running, this is a no-op — so overlapping schedulers
    and rescans don't pile up duplicate postprocess/subtitle_fetch work.

    Dedup is atomic (INSERT ... WHERE NOT EXISTS) so two threads can't both
    pass a check-then-insert race.
    """
    payload_json = json.dumps(payload or {}, sort_keys=True)
    prio = _PRIORITY.get(job_type, _DEFAULT_PRIORITY) if priority is None else priority
    with cursor() as cx:
        if dedup:
            cx.execute(
                "INSERT INTO jobs(type,payload_json,state,priority,run_after) "
                "SELECT ?,?,'queued',?, datetime('now', ?) "
                "WHERE NOT EXISTS (SELECT 1 FROM jobs WHERE type=? AND payload_json=? "
                "                  AND state IN ('queued','running'))",
                (job_type, payload_json, prio, f"+{int(delay_seconds)} seconds",
                 job_type, payload_json),
            )
        else:
            cx.execute(
                "INSERT INTO jobs(type,payload_json,state,priority,run_after) "
                "VALUES(?,?,'queued',?, datetime('now', ?))",
                (job_type, payload_json, prio, f"+{int(delay_seconds)} seconds"),
            )


def _claim_one() -> Optional[dict]:
    """Atomically claim the highest-priority ready job.

    A single UPDATE ... RETURNING is race-free across multiple worker threads
    (SQLite serializes writers), so a worker pool can't double-claim a row —
    unlike the old SELECT-then-UPDATE which was only safe with one worker.
    NOTE: this already increments `attempts`; the claimed row's attempts value
    IS the number of the run now starting (1-based).
    """
    with cursor() as cx:
        row = cx.execute(
            "UPDATE jobs SET state='running', attempts=attempts+1, updated_at=datetime('now') "
            "WHERE id=(SELECT id FROM jobs WHERE state='queued' AND run_after<=datetime('now') "
            "          ORDER BY priority ASC, id ASC LIMIT 1) "
            "RETURNING *"
        ).fetchone()
        return dict(row) if row else None


def _publish(job_type: str, ok: bool) -> None:
    """Live SSE nudge so the UI refreshes affected data immediately —
    including on failure (a failed download/fetch should update the UI too)."""
    try:
        from ..events import bus
        bus.publish("job", {"job": job_type, "ok": ok})
    except Exception:
        pass


def _fail(job: dict, err: str) -> None:
    # _claim_one already incremented attempts; job["attempts"] is the run that
    # just failed. Terminal after MAX_ATTEMPTS actual runs (was off-by-one:
    # the old code double-counted and went terminal after 4).
    attempts = int(job["attempts"] or 1)
    terminal = attempts >= MAX_ATTEMPTS
    state = "error" if terminal else "queued"
    delay = min(3600, 30 * (2 ** attempts))
    with cursor() as cx:
        cx.execute(
            "UPDATE jobs SET state=?, last_error=?, run_after=datetime('now', ?), updated_at=datetime('now') WHERE id=?",
            (state, err[:2000], f"+{delay} seconds", job["id"]),
        )
    log.warning("job %s (%s) failed (attempt %d/%d): %s",
                job["id"], job["type"], attempts, MAX_ATTEMPTS,
                err.splitlines()[0] if err else "")
    if terminal:
        _publish(job["type"], ok=False)
        # surface terminal failures in the notification feed (they were
        # previously invisible outside the jobs table)
        try:
            from ..events import service as events
            events.record(
                "jobs",
                f"Job failed: {job['type']}",
                kind="error",
                detail=(err.splitlines()[0] if err else "")[:300],
            )
        except Exception:
            pass


def _park(job: dict) -> None:
    """No handler registered for this type: park instead of burning attempts.
    Un-parked automatically at startup once the handler exists (deploy drift)."""
    with cursor() as cx:
        cx.execute(
            "UPDATE jobs SET state='parked', attempts=MAX(0, attempts-1), "
            "last_error=?, updated_at=datetime('now') WHERE id=?",
            (f"parked: no handler for job type '{job['type']}'", job["id"]),
        )
    log.warning("job %s parked: no handler for type '%s'", job["id"], job["type"])


def _run_job(job: dict) -> None:
    fn = _handlers.get(job["type"])
    if not fn:
        _park(job)
        return
    try:
        fn(json.loads(job["payload_json"] or "{}"))
        with cursor() as cx:
            cx.execute("UPDATE jobs SET state='done', updated_at=datetime('now') WHERE id=?", (job["id"],))
        _publish(job["type"], ok=True)
    except Exception as e:
        _fail(job, f"{e}\n{traceback.format_exc()}")


def _loop() -> None:
    while not _stop.is_set():
        try:
            job = _claim_one()
            if job:
                _run_job(job)
            else:
                _stop.wait(2.0)
        except Exception as e:  # pragma: no cover
            log.exception("worker loop error: %s", e)
            _stop.wait(5.0)


def add_periodic(func: Callable, seconds: int, job_id: str) -> None:
    if _scheduler is not None:
        _scheduler.add_job(func, "interval", seconds=seconds, id=job_id, replace_existing=True)


def recover_stale_jobs() -> int:
    """Requeue jobs stranded in 'running' by a crash/restart.

    Runs once at startup BEFORE the worker threads exist, so any row still
    marked 'running' is a job whose worker died mid-flight (SIGKILL, power
    loss, redeploy). Left alone it stays 'running' forever and is never
    retried. Requeue each (run_after=now); if it has already burned its whole
    attempt budget, mark it 'error' so it doesn't loop forever.

    Returns the number of rows recovered.
    """
    with cursor() as cx:
        rows = cx.execute("SELECT id, type, attempts FROM jobs WHERE state='running'").fetchall()
        for r in rows:
            if r["attempts"] >= MAX_ATTEMPTS:
                cx.execute(
                    "UPDATE jobs SET state='error', last_error=COALESCE(last_error,'')||' [recovered: stale running, attempts exhausted]', "
                    "updated_at=datetime('now') WHERE id=?",
                    (r["id"],),
                )
            else:
                cx.execute(
                    "UPDATE jobs SET state='queued', run_after=datetime('now'), updated_at=datetime('now') WHERE id=?",
                    (r["id"],),
                )
        if rows:
            log.warning("recovered %d stale 'running' job(s) on startup", len(rows))
        return len(rows)


def unpark_jobs() -> int:
    """Requeue parked jobs (and legacy 'no handler' error jobs) whose type now
    has a registered handler. Runs at startup after all modules imported —
    this heals deploy drift that left jobs in terminal 'error'.
    """
    if not _handlers:
        return 0
    marks = ",".join("?" for _ in _handlers)
    types = list(_handlers)
    with cursor() as cx:
        cur = cx.execute(
            f"UPDATE jobs SET state='queued', attempts=0, last_error=NULL, "
            f"run_after=datetime('now'), updated_at=datetime('now') "
            f"WHERE (state='parked' OR (state='error' AND last_error LIKE 'no handler%')) "
            f"AND type IN ({marks})",
            types,
        )
        n = cur.rowcount or 0
    if n:
        log.warning("unparked %d job(s) whose handler is now registered", n)
    return n


# ---------------------------------------------------------------------------
# admin surface (used by the jobs router)
# ---------------------------------------------------------------------------

def job_stats() -> dict:
    """Counts per state + per (type,state) for the error set. Cheap."""
    with cursor() as cx:
        states = {r["state"]: r["n"] for r in cx.execute(
            "SELECT state, COUNT(*) n FROM jobs GROUP BY state")}
        errors = [dict(r) for r in cx.execute(
            "SELECT type, COUNT(*) n FROM jobs WHERE state='error' GROUP BY type ORDER BY n DESC")]
        oldest_queued = cx.execute(
            "SELECT MIN(created_at) m FROM jobs WHERE state='queued'").fetchone()["m"]
    return {"states": states, "errors_by_type": errors,
            "oldest_queued": oldest_queued, "registered": registered_types()}


def list_jobs(state: Optional[str] = None, job_type: Optional[str] = None,
              limit: int = 100) -> list[dict]:
    q = "SELECT id, type, state, attempts, priority, payload_json, last_error, created_at, updated_at, run_after FROM jobs"
    conds, args = [], []
    if state:
        conds.append("state=?"); args.append(state)
    if job_type:
        conds.append("type=?"); args.append(job_type)
    if conds:
        q += " WHERE " + " AND ".join(conds)
    q += " ORDER BY id DESC LIMIT ?"
    args.append(max(1, min(int(limit), 500)))
    with cursor() as cx:
        return [dict(r) for r in cx.execute(q, args)]


def retry_job(job_id: int) -> bool:
    """Requeue a single error/parked/cancelled job with a fresh attempt budget."""
    with cursor() as cx:
        cur = cx.execute(
            "UPDATE jobs SET state='queued', attempts=0, last_error=NULL, "
            "run_after=datetime('now'), updated_at=datetime('now') "
            "WHERE id=? AND state IN ('error','parked','cancelled')",
            (job_id,),
        )
        return (cur.rowcount or 0) > 0


def retry_errors(job_type: Optional[str] = None) -> int:
    """Requeue all terminal-error jobs (optionally one type). Fresh budget."""
    with cursor() as cx:
        if job_type:
            cur = cx.execute(
                "UPDATE jobs SET state='queued', attempts=0, last_error=NULL, "
                "run_after=datetime('now'), updated_at=datetime('now') "
                "WHERE state='error' AND type=?",
                (job_type,),
            )
        else:
            cur = cx.execute(
                "UPDATE jobs SET state='queued', attempts=0, last_error=NULL, "
                "run_after=datetime('now'), updated_at=datetime('now') "
                "WHERE state='error'",
            )
        return cur.rowcount or 0


def cancel_job(job_id: int) -> bool:
    """Cancel a queued/parked job. A running job cannot be interrupted
    (threads), but marking it cancelled prevents requeue on failure."""
    with cursor() as cx:
        cur = cx.execute(
            "UPDATE jobs SET state='cancelled', updated_at=datetime('now') "
            "WHERE id=? AND state IN ('queued','parked')",
            (job_id,),
        )
        return (cur.rowcount or 0) > 0


def prune_jobs() -> dict:
    """Delete old finished jobs + old events so the tables don't grow
    unbounded, especially when each error row carries a full traceback. 0 in
    config = keep forever. Events: read ones age out at the
    configured retention; *any* event ages out at 4x retention (an unread
    feed is not an archive), and the table is hard-capped at 20k rows.
    """
    removed_jobs = removed_events = 0
    jd = int(getattr(settings, "job_retention_days", 0) or 0)
    ed = int(getattr(settings, "event_retention_days", 0) or 0)
    with cursor() as cx:
        if jd > 0:
            cur = cx.execute(
                "DELETE FROM jobs WHERE state IN ('done','error','cancelled') "
                "AND updated_at < datetime('now', ?)",
                (f"-{jd} days",),
            )
            removed_jobs = cur.rowcount or 0
        if ed > 0:
            cur = cx.execute(
                "DELETE FROM events WHERE read=1 AND created_at < datetime('now', ?)",
                (f"-{ed} days",),
            )
            removed_events = cur.rowcount or 0
            cur = cx.execute(
                "DELETE FROM events WHERE created_at < datetime('now', ?)",
                (f"-{4 * ed} days",),
            )
            removed_events += cur.rowcount or 0
        cur = cx.execute(
            "DELETE FROM events WHERE id NOT IN (SELECT id FROM events ORDER BY id DESC LIMIT 20000)"
        )
        removed_events += cur.rowcount or 0
    if removed_jobs or removed_events:
        log.info("prune: removed %d job(s), %d event(s)", removed_jobs, removed_events)
    return {"jobs": removed_jobs, "events": removed_events}


def start_scheduler() -> None:
    global _scheduler, _workers
    if _workers and any(w.is_alive() for w in _workers):
        return
    try:
        recover_stale_jobs()
    except Exception as e:  # pragma: no cover - never block startup on recovery
        log.warning("stale-job recovery failed: %s", e)
    try:
        unpark_jobs()
    except Exception as e:  # pragma: no cover
        log.warning("unpark failed: %s", e)
    _stop.clear()
    n = max(1, int(getattr(settings, "job_workers", 1) or 1))
    _workers = []
    for i in range(n):
        w = threading.Thread(target=_loop, name=f"jobworker-{i}", daemon=True)
        w.start()
        _workers.append(w)
    _scheduler = BackgroundScheduler(daemon=True)
    _scheduler.start()
    # daily retention sweep (best-effort; also safe to call manually)
    try:
        _scheduler.add_job(prune_jobs, "interval", hours=24, id="prune_jobs", replace_existing=True)
    except Exception as e:  # pragma: no cover
        log.warning("could not schedule prune_jobs: %s", e)
    log.info("jobs: %d worker(s) + scheduler started", n)


def stop_scheduler() -> None:
    _stop.set()
    if _scheduler is not None:
        try:
            _scheduler.shutdown(wait=False)
        except Exception:
            pass
