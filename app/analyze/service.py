"""Comprehension-without-download.

Fetch jimaku subtitles by AniList ID for a title's episodes and compute
comprehension — no torrent or video needed. This makes the comprehension
badge meaningful for the whole MAL library (especially plan-to-watch: see how
hard a show is *before* you start it).

Reuses the existing pipeline: it ensures episode rows exist, then enqueues
`subtitle_fetch` for each (which fetches jimaku → ingests → enqueues
`comprehension`). The subtitle pipeline already handles the no-video case.
"""
from __future__ import annotations

import logging

from ..db import connect
from ..jobs.service import enqueue

log = logging.getLogger("mimi_lab.analyze")

DEFAULT_SAMPLE = 3            # episodes to sample for a quick preview
STAGGER_SECONDS = 3          # space out jimaku calls to be polite


def _title(anilist_id: int):
    cx = connect()
    try:
        return cx.execute(
            "SELECT anilist_id, total_episodes, romaji FROM titles WHERE anilist_id=?",
            (anilist_id,),
        ).fetchone()
    finally:
        cx.close()


def analyze_title(anilist_id: int, max_eps: int | None = None) -> dict:
    """Ensure the full episode list exists and enqueue a whole-title subtitle
    fetch — comprehension is computed per episode as the subs ingest, for EVERY
    episode jimaku has. No download required. (`max_eps` accepted for back-compat
    but ignored: we score the whole title now, not a sample.)"""
    t = _title(anilist_id)
    if not t:
        return {"error": "title not found", "anilist_id": anilist_id}
    try:
        from ..catalog import service as catalog
        catalog.ensure_episodes(anilist_id)
    except Exception as e:  # pragma: no cover
        log.warning("ensure_episodes(%s) failed: %s", anilist_id, e)
    enqueue("title_subtitle_fetch", {"anilist_id": anilist_id})
    return {"anilist_id": anilist_id, "title": t["romaji"], "queued": 1}


def analyze_library(status: str | None = None, max_eps: int = 1) -> dict:
    """Bulk: enqueue a whole-title subtitle fetch for every title that isn't fully
    scored yet — every episode jimaku has gets comprehension, no downloads.
    `status` optionally restricts to one MAL status. (`max_eps` is ignored.)"""
    cx = connect()
    try:
        q = (
            "SELECT t.anilist_id FROM titles t WHERE ("
            " t.total_episodes IS NULL OR "
            " (SELECT COUNT(*) FROM episodes e WHERE e.anilist_id=t.anilist_id "
            "  AND e.comprehension_pct IS NOT NULL) < t.total_episodes)"
        )
        params: list = []
        if status:
            q += " AND t.mal_status=?"
            params.append(status)
        ids = [r["anilist_id"] for r in cx.execute(q, params).fetchall()]
    finally:
        cx.close()

    from ..catalog import service as catalog
    for aid in ids:
        try:
            catalog.ensure_episodes(aid)
        except Exception:
            pass
        enqueue("title_subtitle_fetch", {"anilist_id": aid})
    log.info("analyze_library: %d titles queued for whole-title fetch", len(ids))
    return {"titles": len(ids), "queued": len(ids)}
