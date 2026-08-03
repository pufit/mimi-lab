"""Learning layer HTTP routes."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from ..models import ComprehensionResult, Moment, NewWord
from ..security import require_token
from . import service

router = APIRouter(prefix="/learn", tags=["learn"])


class ComprehensionUpload(BaseModel):
    episode_id: int
    stats: dict  # Migaku's panel output {pct, rating, known, unknown, ignored, learning}


@router.get("/sweet-spot")
def sweet_spot(lo: float = 80.0, hi: float = 95.0, limit: int = 60, include_watched: bool = False):
    """Episodes in the i+1 comprehension band — 'what to watch next at my level'."""
    return service.sweet_spot(lo=lo, hi=hi, limit=limit, include_watched=include_watched)


class AnkiExportRequest(BaseModel):
    line_ids: list[int]


@router.post("/anki/export")
def anki_export(req: AnkiExportRequest):
    """Return a tab-separated Anki import (sentence/reading/translation/source) for
    the given Moments lines — the sentence bank built from your own library."""
    from fastapi.responses import PlainTextResponse
    tsv = service.anki_export(req.line_ids)
    return PlainTextResponse(
        tsv,
        media_type="text/tab-separated-values",
        headers={"content-disposition": 'attachment; filename="mimi-lab-anki.tsv"'},
    )


@router.get("/health/migaku")
def migaku_health():
    """On-demand Migaku drift self-test: tokenizes the TestShow fixture via the
    sidecar and compares to the stored baseline. Does NOT record an event (the
    daily job does that); returns the report for the Settings page."""
    return service.migaku_drift_check(record=False)


@router.get("/comprehension/{episode_id}", response_model=ComprehensionResult)
def comprehension(episode_id: int):
    """Aligned comprehension (server-side tokenizer + Migaku formula)."""
    try:
        return service.comprehension_aligned(episode_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post(
    "/comprehension/upload",
    response_model=ComprehensionResult,
    dependencies=[Depends(require_token)],
)
def comprehension_upload(payload: ComprehensionUpload):
    """Receive Migaku-exact comprehension PUSHED by the Connector (auth).

    The Connector scrapes Migaku's ComprehensionStats panel after an episode is
    opened (notes/REMOTE_PLAYBACK_DESIGN.md §3, P2) and POSTs it here; we persist the
    exact pct/rating onto the episode. A malformed scrape (pct missing/out of
    range — Migaku UI drift) is rejected with 422 instead of poisoning the row.
    """
    try:
        return service.record_exact_comprehension(payload.episode_id, payload.stats)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.get("/moments", response_model=list[Moment])
def moments(
    q: str = Query(..., min_length=1),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    anilist_id: int | None = Query(None, description="filter to one show"),
    sort: str = Query("position", pattern="^(position|iplus1)$"),
    downloaded: bool = Query(True, description="only lines from episodes with a local video file"),
):
    """Word search -> moments (line + media anchors).

    Defaults to DOWNLOADED content only, so every result can be played in the
    browser / clipped; `downloaded=false` searches the full analysis corpus
    (subs-only shows included). sort=iplus1 puts the easiest mining sentences
    (fewest unknown words) first.
    """
    return service.moments_search(q, limit=limit, offset=offset,
                                  anilist_id=anilist_id, sort=sort,
                                  downloaded_only=downloaded)


@router.post("/clip/{line_id}")
def clip(line_id: int):
    """Extract a screenshot + audio clip for a subtitle line."""
    try:
        return service.extract_clip(line_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/moments/{line_id}/translate")
def translate_moment(line_id: int):
    """On-demand MT for one subtitle line; cached into the corpus."""
    try:
        return service.translate_line(line_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/episode/{episode_id}/new-words", response_model=list[NewWord])
def new_words(episode_id: int, limit: int = Query(200, ge=1, le=1000)):
    """Unknown lemmas in an episode with frequency rank + gloss (best-effort)."""
    return service.new_words(episode_id, limit=limit)


@router.get("/leverage")
def leverage(refresh: bool = Query(False), top: int = Query(60, ge=1, le=200)):
    """The Study queue: unknown words ranked by how many near-threshold episodes
    they help push into the 80%+ sweet spot (greedy, cumulative). Cached until
    the known-set changes."""
    return service.word_leverage(refresh=refresh, top=top)


@router.get("/episode/{episode_id}/transcript")
def transcript(episode_id: int):
    """Full episode transcript with per-token known-status (pre-watch reading)."""
    try:
        return service.episode_transcript(episode_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/anki/export.apkg")
def anki_export_apkg(req: AnkiExportRequest, deck: str = Query("Mimi Lab")):
    """A real Anki deck (.apkg) for the given lines — sentence, furigana,
    translation, source, plus the audio/screenshot clip where video exists."""
    import os
    from fastapi.responses import FileResponse
    from starlette.background import BackgroundTask

    path = service.anki_export_apkg(req.line_ids, deck_name=deck)
    if not path:
        raise HTTPException(status_code=404, detail="no exportable lines")
    return FileResponse(
        path,
        media_type="application/octet-stream",
        filename="mimi-lab.apkg",
        background=BackgroundTask(lambda: os.unlink(path)),
    )


@router.get("/stats")
def stats(days: int = Query(120, ge=7, le=730)):
    """Known-words growth, comprehension history, and watch activity series."""
    return service.learning_stats(days=days)
