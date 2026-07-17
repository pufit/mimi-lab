"""Catalog HTTP routes (mounted at /api/catalog). Thin — logic in service.py."""
from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException

from ..models import Episode, Title
from . import service

router = APIRouter(prefix="/catalog", tags=["catalog"])


@router.get("/titles", response_model=list[Title])
def list_titles():
    return service.get_titles()


@router.get("/titles/{anilist_id}", response_model=Title)
def get_title(anilist_id: int):
    t = service.get_title(anilist_id)
    if not t:
        raise HTTPException(status_code=404, detail="title not found")
    return t


@router.get("/titles/{anilist_id}/episodes", response_model=list[Episode])
def list_episodes(anilist_id: int):
    return service.list_episodes(anilist_id)


@router.delete("/titles/{anilist_id}")
def delete_title(anilist_id: int, delete_files: bool = True):
    """Remove a title + ALL its data: episodes, subtitle corpus (lines/lemmas/
    FTS), downloads, clips, and managed files on disk. Irreversible (nightly
    DB backups aside)."""
    res = service.delete_title(anilist_id, delete_files=delete_files)
    if not res.get("ok"):
        raise HTTPException(status_code=404, detail=res.get("reason", "delete failed"))
    return res


@router.post("/scan")
def scan():
    return service.scan_library()


@router.post("/repair/phantom-episodes")
def repair_phantom_episodes(anilist_id: int | None = None):
    """Remove episode rows past a title's real episode count (legacy
    over-counting from absolute-numbered subtitle files). See
    catalog.prune_phantom_episodes."""
    return service.prune_phantom_episodes(anilist_id)


@router.post("/episodes/{episode_id}/watched")
def set_watched(episode_id: int, watched: bool = Body(True, embed=True)):
    ok = service.set_watched(episode_id, watched)
    if not ok:
        raise HTTPException(status_code=404, detail="episode not found")
    return {"episode_id": episode_id, "watched": watched}


@router.post("/titles/{anilist_id}/watched-up-to")
def watched_up_to(anilist_id: int, ep_number: int = Body(..., embed=True)):
    """Mark every episode up to (and including) ep_number watched — the
    'I've already seen this show up to ep N' bulk affordance."""
    return service.set_watched_up_to(anilist_id, ep_number)


@router.get("/continue")
def continue_watching():
    """The 'continue watching' rail: in-progress episodes (resume) + the next
    unwatched local episode of recently watched shows."""
    return service.continue_watching()
