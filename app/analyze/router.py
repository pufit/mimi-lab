from __future__ import annotations

from typing import Optional

from fastapi import APIRouter

from . import service

router = APIRouter(prefix="/analyze", tags=["analyze"])


@router.post("/title/{anilist_id}")
def analyze_title(anilist_id: int, max_eps: Optional[int] = None):
    """Fetch jimaku subs + compute comprehension for a title's episodes,
    no download required. Work runs in the background; poll the title's
    episodes for results."""
    return service.analyze_title(anilist_id, max_eps)


@router.post("/library")
def analyze_library(status: Optional[str] = None, max_eps: int = 1):
    """Bulk: analyze comprehension for every title that doesn't have it yet
    (fetch jimaku subs + score — no downloads). Optionally filter by MAL status."""
    return service.analyze_library(status, max_eps)
