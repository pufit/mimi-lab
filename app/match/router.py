"""Match HTTP routes (mounted at /api/match). Thin — logic in service.py."""
from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException, Query

from ..models import MatchCandidate, MatchQueueItem, MatchResult
from . import service

router = APIRouter(prefix="/match", tags=["match"])


@router.get("/preview", response_model=MatchResult)
def preview(filename: str):
    """Parse + search + rank a filename without persisting anything."""
    return service.match_file(filename)


@router.get("/queue", response_model=list[MatchQueueItem])
def queue(state: str = "pending"):
    return service.list_queue(state)


@router.get("/search", response_model=list[MatchCandidate])
def search(q: str = Query(..., description="title to search AniList for"),
           ep_number: int | None = None):
    """Manual AniList search for resolving a queue item by hand."""
    return service.search_candidates(q, ep_number=ep_number)


@router.post("/queue/{queue_id}/confirm")
def confirm(queue_id: int, anilist_id: int = Body(..., embed=True)):
    try:
        return service.confirm(queue_id, anilist_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/queue/{queue_id}/suggest")
def suggest(queue_id: int):
    """Use Claude Haiku to suggest the AniList title for a hard-to-match file."""
    try:
        return service.suggest_match(queue_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.delete("/queue/{queue_id}")
def delete_queue_item(queue_id: int):
    """Remove an item from the match queue."""
    if not service.delete_queue_item(queue_id):
        raise HTTPException(status_code=404, detail="queue item not found")
    return {"ok": True, "deleted": queue_id}


@router.post("/idmap/refresh")
def idmap_refresh():
    return service.refresh_idmap()
