"""Acquire HTTP routes — thin wrappers over app.acquire.service.

Served under /api/acquire/* (see app/main.py).
"""
from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException, Query

from ..models import Download, EpisodeReleases, NyaaResult, RssFollow, SeasonReleases
from . import service

router = APIRouter(prefix="/acquire", tags=["acquire"])


def _nyaa_from_payload(raw: dict) -> NyaaResult:
    """Build a NyaaResult from a loose request body (a chosen release)."""
    return NyaaResult(
        nyaa_id=raw.get("nyaa_id"),
        title=raw.get("title") or raw.get("magnet") or "manual download",
        magnet=raw.get("magnet"),
        torrent_url=raw.get("torrent_url"),
        size=raw.get("size"),
        seeders=raw.get("seeders"),
        leechers=raw.get("leechers"),
        trusted=raw.get("trusted", False),
        timestamp=raw.get("timestamp"),
    )


@router.get("/search", response_model=list[NyaaResult])
def search(
    q: str = Query(..., description="nyaa search query"),
    trusted: bool = Query(True, description="trusted uploads only (f=2)"),
    category: str = Query("1_2", description="nyaa category (default Anime/English)"),
):
    return service.nyaa_search(q, category=category, trusted=trusted)


@router.post("/download", response_model=Download)
def download(payload: dict = Body(...)):
    """Add a download. Body may be a full NyaaResult, or {magnet, title, ...}."""
    if not payload.get("title") and not payload.get("magnet") and not payload.get("torrent_url"):
        raise HTTPException(status_code=422, detail="need at least title+magnet/torrent_url")
    try:
        result = NyaaResult(
            nyaa_id=payload.get("nyaa_id"),
            title=payload.get("title") or payload.get("magnet") or "manual download",
            magnet=payload.get("magnet"),
            torrent_url=payload.get("torrent_url"),
            size=payload.get("size"),
            seeders=payload.get("seeders"),
            leechers=payload.get("leechers"),
            trusted=payload.get("trusted", False),
            timestamp=payload.get("timestamp"),
        )
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"bad NyaaResult: {e}")
    return service.add_download(result)


@router.get("/downloads", response_model=list[Download])
def downloads():
    return service.list_downloads()


@router.delete("/downloads/{download_id}")
def cancel_download(download_id: int):
    """Cancel a download: remove its torrent from Transmission (discarding partial
    data) and mark the row cancelled."""
    res = service.cancel_download(download_id)
    if not res.get("ok"):
        raise HTTPException(status_code=404, detail=res.get("reason", "download not found"))
    return res


@router.get("/follows", response_model=list[RssFollow])
def follows():
    return service.list_follows()


@router.post("/follows", response_model=RssFollow)
def add_follow(payload: dict = Body(...)):
    query = payload.get("query")
    if not query:
        raise HTTPException(status_code=422, detail="query is required")
    return service.add_follow(
        query=query,
        title=payload.get("title"),
        anilist_id=payload.get("anilist_id"),
        category=payload.get("category", "1_2"),
        trusted_only=payload.get("trusted_only", True),
        resolution=payload.get("resolution", "1080p"),
    )


@router.post("/title/{anilist_id}/follow", response_model=RssFollow)
def follow_title(anilist_id: int):
    """One-click Follow for a library title (derives the nyaa query from its name)."""
    try:
        return service.follow_title(anilist_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.delete("/follows/{follow_id}")
def delete_follow(follow_id: int):
    if not service.remove_follow(follow_id):
        raise HTTPException(status_code=404, detail="follow not found")
    return {"ok": True, "deleted": follow_id}


@router.get("/qbt")
def qbt_status():
    """Lightweight torrent-daemon reachability probe (for the UI)."""
    return {"available": service.qbt_available()}


@router.get("/episode/{episode_id}/releases", response_model=EpisodeReleases)
def episode_releases(episode_id: int, llm: bool = Query(True, description="use Haiku to recommend the best release")):
    """List nyaa releases for an episode (the download dropdown), with a
    recommended top pick (heuristic, optionally refined by Claude Haiku)."""
    return service.list_episode_releases(episode_id, use_llm=llm)


@router.post("/episode/{episode_id}")
def download_episode(episode_id: int, payload: dict | None = Body(default=None)):
    """Download a release for this episode via Transmission. Body may carry a
    chosen release (`{"release": {...NyaaResult...}}` or a bare NyaaResult);
    with no body, the heuristic best is auto-picked."""
    chosen = None
    if payload:
        raw = payload.get("release", payload) if isinstance(payload, dict) else None
        if isinstance(raw, dict) and (raw.get("title") or raw.get("magnet") or raw.get("torrent_url")):
            try:
                chosen = NyaaResult(
                    nyaa_id=raw.get("nyaa_id"),
                    title=raw.get("title") or raw.get("magnet") or "manual download",
                    magnet=raw.get("magnet"),
                    torrent_url=raw.get("torrent_url"),
                    size=raw.get("size"),
                    seeders=raw.get("seeders"),
                    leechers=raw.get("leechers"),
                    trusted=raw.get("trusted", False),
                    timestamp=raw.get("timestamp"),
                )
            except Exception as e:
                raise HTTPException(status_code=422, detail=f"bad release: {e}")
    res = service.download_episode(episode_id, chosen=chosen)
    if not res.get("ok"):
        raise HTTPException(status_code=404, detail=res.get("reason", "no release found"))
    return res


@router.get("/title/{anilist_id}/batches", response_model=SeasonReleases)
def title_batches(
    anilist_id: int,
    llm: bool = Query(True, description="use Haiku to recommend the best season pack"),
):
    """List nyaa season packs / batches for a whole title (the 'Download season'
    picker), with a recommended top pick (heuristic, optionally Haiku-refined)."""
    return service.list_season_releases(anilist_id, use_llm=llm)


@router.post("/title/{anilist_id}/batch")
def download_batch(anilist_id: int, payload: dict = Body(...)):
    """Download a season pack for a title via Transmission. Body carries the chosen
    release plus an optional episode selection:
        {"release": {...NyaaResult..., "episode_count": N}, "episodes": [1,2,3]}
    `episodes` (season-relative numbers) restricts the download to those episodes;
    omit it (or send all) to grab the whole pack. On completion every selected
    episode is imported into this title."""
    raw = payload.get("release", payload) if isinstance(payload, dict) else None
    if not (isinstance(raw, dict) and (raw.get("magnet") or raw.get("torrent_url"))):
        raise HTTPException(status_code=422, detail="need a release with magnet/torrent_url")
    try:
        chosen = _nyaa_from_payload(raw)
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"bad release: {e}")
    episodes = payload.get("episodes") if isinstance(payload, dict) else None
    if not isinstance(episodes, list):
        episodes = None
    res = service.download_batch(
        anilist_id, chosen,
        episode_count=raw.get("episode_count"),
        selected_episodes=episodes,
    )
    if not res.get("ok"):
        raise HTTPException(status_code=404, detail=res.get("reason", "could not start batch"))
    return res


@router.post("/downloads/{download_id}/retry")
def retry_download(download_id: int):
    """Retry a failed download row.

    - 'lost' / 'error' / 'queued'   -> re-add the torrent to Transmission
    - 'pp_failed'                    -> re-enqueue the postprocess job
    """
    res = service.retry_download(download_id)
    if not res.get("ok"):
        raise HTTPException(status_code=404, detail=res.get("reason", "not retryable"))
    return res
