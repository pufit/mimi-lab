"""Periodic job registration for the pipeline automation.

`register_periodics()` wires the recurring sweeps onto the APScheduler. The
main app calls it AFTER the scheduler has started (add_periodic is a no-op
while the scheduler is None, so order matters). Handler functions are imported
lazily inside the function to avoid import cycles at module load.

Periodics:
  * poll_downloads   every  30s  (id 'poll_downloads')  — refresh torrents
  * rss_check        every 300s  (id 'rss_check')       — poll RSS follows
  * late_sub_sweep   every 3600s (id 'late_sub_sweep')  — retry missing subs

Everything except the SRS tick (SRS_DESIGN §9.2) is gated on
`settings.pipeline_periodics`, so a dev instance pointed at a COPY of the
database (PIPELINE_PERIODICS=0) cannot download, transcode or push to MAL.
"""
from __future__ import annotations

import logging

log = logging.getLogger("mimi_lab.jobs.periodics")


def register_periodics() -> None:
    """Register all recurring jobs onto the scheduler. Best-effort & idempotent.

    Each handler is wrapped so one bad import never blocks the others.
    """
    from ..config import settings
    from .service import add_periodic

    # A dev instance pointed at a COPY of the database (MIMI_LAB_DB=…/scratch.db)
    # inherits the live job queue and would happily download, transcode into the
    # real library and push to MAL. PIPELINE_PERIODICS=0 registers only the local,
    # srs_*-scoped periodics. Default (and the deployment) is unchanged: True.
    pipeline = bool(getattr(settings, "pipeline_periodics", True))
    if not pipeline:
        log.warning("pipeline_periodics=0: acquisition/media/MAL/maintenance "
                    "periodics are NOT registered (dev instance)")

    # poll_downloads — refresh active torrents, enqueue postprocess on complete
    if pipeline:
        try:
            from ..acquire.service import poll_downloads
            add_periodic(lambda: poll_downloads(), seconds=30, job_id="poll_downloads")
        except Exception as e:  # pragma: no cover
            log.warning("could not register poll_downloads periodic: %s", e)

    # rss_check — poll RSS follows for new releases
    if pipeline:
        try:
            from ..acquire.service import handle_rss_check
            add_periodic(lambda: handle_rss_check({}), seconds=300, job_id="rss_check")
        except Exception as e:  # pragma: no cover
            log.warning("could not register rss_check periodic: %s", e)

    # late_sub_sweep — retry episodes that still lack a usable subtitle
    if pipeline:
        try:
            from ..subs.service import late_sub_sweep
            add_periodic(lambda: late_sub_sweep({}), seconds=3600, job_id="late_sub_sweep")
        except Exception as e:  # pragma: no cover
            log.warning("could not register late_sub_sweep periodic: %s", e)

    # migaku_drift_check — detect a Migaku extension update that broke/changed the
    # reverse-engineered tokenizer (daily); also run once shortly after startup.
    if pipeline:
        try:
            from ..learn.service import migaku_drift_check
            add_periodic(lambda: migaku_drift_check(), seconds=86400, job_id="migaku_drift_check")
            from .service import enqueue
            enqueue("migaku_drift_check", {}, delay_seconds=60)
        except Exception as e:  # pragma: no cover
            log.warning("could not register migaku_drift_check periodic: %s", e)

    # mal_sync — the "nightly pull" the docs promised but nothing ever scheduled
    # (last real sync was a manual button press). Push-on-watched is separate
    # (catalog.set_watched enqueues a targeted push).
    if pipeline:
        try:
            from .service import enqueue as _enq
            add_periodic(lambda: _enq("mal_sync", {}), seconds=86400, job_id="mal_sync_nightly")
        except Exception as e:  # pragma: no cover
            log.warning("could not register mal_sync periodic: %s", e)

    # SRS (SRS_DESIGN §9.2): a 6-hour tick that enqueues the auto top-up, the
    # daily Migaku reconcile and the weekly clip audit, and does the cheap
    # inline housekeeping (stale-run sweep, stale pending clips, relink). The
    # boot-time generate check is delayed 300 s and exits `nothing_to_do` on an
    # empty deck, so it never generates before the curated import.
    try:
        from ..srs.service import periodic_tick
        add_periodic(periodic_tick, seconds=6 * 3600, job_id="srs_tick")
        from .service import enqueue as _enq
        _enq("srs_generate", {"trigger": "auto"}, delay_seconds=300, priority=60)
    except Exception as e:  # pragma: no cover
        log.warning("could not register srs periodics: %s", e)

    # maintenance: backups, log rotation, DB hygiene, retention, daily stats
    if pipeline:
        try:
            from .. import maintenance
            add_periodic(maintenance.backup_db, seconds=86400, job_id="backup_db")
            add_periodic(maintenance.rotate_logs, seconds=3600, job_id="rotate_logs")
            add_periodic(maintenance.db_hygiene, seconds=7 * 86400, job_id="db_hygiene")
            add_periodic(maintenance.retention_sweep, seconds=86400, job_id="retention_sweep")
            add_periodic(maintenance.capture_daily_stats, seconds=86400,
                         job_id="capture_daily_stats")
        except Exception as e:  # pragma: no cover
            log.warning("could not register maintenance periodics: %s", e)

    log.info("periodics registered")
