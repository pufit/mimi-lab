"""Subtitle pipeline HTTP routes."""
from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException

from ..models import SubtitleLine
from . import service

router = APIRouter(prefix="/subs", tags=["subs"])


@router.post("/fetch/{episode_id}")
def fetch(episode_id: int):
    """Run the full jimaku -> align -> ingest pipeline for an episode."""
    try:
        return service.fetch_for_episode(episode_id)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.post("/align-sweep")
def align_sweep(force: bool = False):
    """Enqueue re-alignment for episodes that could align better (e.g. an
    English reference track has since appeared). The handler existed but was
    reachable only from a REPL before this route."""
    return service.subtitle_align_sweep({"force": force})


# NOTE: static /english/* routes MUST precede the parametric /english/{episode_id}
# — FastAPI matches in declaration order, so "sweep"/"config" would otherwise be
# captured as an (invalid) episode_id.
@router.post("/english/sweep")
def english_sweep(cap: int = 20, force: bool = False):
    """Backfill: enqueue English fetches for episodes with a video but no English
    subtitle yet (capped, staggered). `force=true` re-fetches even episodes that
    already have an English row (e.g. to redo low-quality matches)."""
    from . import english
    return english.english_sub_sweep({"cap": cap, "force": force})


@router.get("/english/config")
def get_english_config():
    """Current English secondary-subtitle settings (for the Settings UI)."""
    from . import english
    return english.english_config()


@router.put("/english/config")
def put_english_config(payload: dict = Body(...)):
    """Switch the English source at runtime: {"source": "human" | "llm"}."""
    from . import english
    try:
        english.set_english_source(payload.get("source", ""))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return english.english_config()


@router.post("/english/{episode_id}")
def fetch_english(episode_id: int):
    """Resolve + store the English secondary subtitle for an episode (embedded
    softsub or Claude MT, per settings.english_source) and merge translations."""
    from . import english
    try:
        return english.fetch_english_for_episode(episode_id)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/episode/{episode_id}/lines", response_model=list[SubtitleLine])
def episode_lines(episode_id: int):
    """The ingested subtitle corpus for an episode (ordered)."""
    from ..learn import service as learn_service

    return learn_service.episode_lines(episode_id)
