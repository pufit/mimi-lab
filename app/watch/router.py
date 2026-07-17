from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ..connector.service import ConnectorOffline, ConnectorTimeout
from . import service

router = APIRouter(prefix="/watch", tags=["watch"])

_OFFLINE = "Connector offline — start it on the machine where you watch (Chrome + Migaku)."


class PlayRequest(BaseModel):
    episode_id: int
    seek_ms: Optional[int] = None
    # Target Connector device; None = default routing (single device → it,
    # else Migaku-ready / most recently used).
    device_id: Optional[str] = None


class MomentRequest(BaseModel):
    line_id: int
    device_id: Optional[str] = None


@router.post("/play")
async def play(req: PlayRequest, request: Request):
    try:
        return await service.play_episode(
            req.episode_id, req.seek_ms, request_base=str(request.base_url),
            device_id=req.device_id,
        )
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ConnectorOffline as e:
        raise HTTPException(status_code=409, detail=str(e) if req.device_id else _OFFLINE)
    except ConnectorTimeout as e:
        raise HTTPException(status_code=504, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"connector error: {e}")


@router.post("/play-moment")
async def play_moment(req: MomentRequest, request: Request):
    try:
        return await service.play_moment(
            req.line_id, request_base=str(request.base_url), device_id=req.device_id
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ConnectorOffline as e:
        raise HTTPException(status_code=409, detail=str(e) if req.device_id else _OFFLINE)
    except ConnectorTimeout as e:
        raise HTTPException(status_code=504, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"connector error: {e}")


@router.get("/browser-play/{episode_id}")
def browser_play(episode_id: int, request: Request):
    """Signed media URLs for the in-browser fallback player.

    When the Connector is offline (or you're on a device without it), the SPA
    can still stream the episode with a plain HTML5 <video> + WebVTT tracks —
    no Migaku features, but watching is never a dead end.
    """
    import os as _os

    from ..media.streaming import resolve_episode_media
    from ..security import make_media_token

    video, sub, sub2 = resolve_episode_media(episode_id)
    if not video or not _os.path.exists(video):
        raise HTTPException(status_code=404, detail="episode has no playable video on disk")

    def _url(scope: str, fmt: str | None = None) -> str:
        token = make_media_token(episode_id, scope)
        extra = f"&format={fmt}" if fmt else ""
        return f"/api/media/episode/{episode_id}/{scope}?token={token}{extra}"

    seek_ms = None
    try:
        from ..db import connect
        with connect() as cx:
            row = cx.execute(
                "SELECT watch_progress_ms, duration_ms, watched FROM episodes WHERE id=?",
                (episode_id,),
            ).fetchone()
        if row and not row["watched"] and row["watch_progress_ms"] and row["watch_progress_ms"] > 30_000:
            seek_ms = int(row["watch_progress_ms"])
    except Exception:
        pass

    return {
        "episode_id": episode_id,
        "video_url": _url("video"),
        "sub_url": _url("subtitle", "vtt") if sub else None,
        "sub2_url": _url("subtitle2", "vtt") if sub2 else None,
        "seek_ms": seek_ms,
    }


class ProgressReport(BaseModel):
    episode_id: int
    position_ms: int
    duration_ms: Optional[int] = None


@router.post("/progress")
def report_progress(req: ProgressReport):
    """Watch-progress from the in-browser fallback player (same shape as the
    Connector's telemetry — auto-watched, rewatch logging + MAL push apply
    equally)."""
    service.record_telemetry(req.model_dump(), source="browser")
    return {"ok": True}


# Back-compat alias for the old UI status probe; the canonical route is
# GET /api/connector/status (app/connector/router.py).
@router.get("/connector/status")
def connector_status():
    return service.connector_status()
