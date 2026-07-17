"""Media HTTP routes — thin wrappers over app.media.service.

Served under /api/media/* (see app/main.py).

Also hosts the range-capable streaming endpoints the Connector's Migaku fetches
from on Play (notes/REMOTE_PLAYBACK_DESIGN.md §3): the bytes flow server → Migaku
directly, never through the Connector process.
"""
from __future__ import annotations

import os

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request

from ..config import settings
from ..security import require_token, verify_media_token
from . import service, streaming

router = APIRouter(prefix="/media", tags=["media"])


def _validate_managed_path(path: str) -> str:
    """Reject paths outside the library/inbox roots.

    /probe + /postprocess take a filesystem path; without this an authenticated
    caller could ffprobe/transcode ANY file on the server (file-existence oracle
    + CPU DoS). Restrict to the dirs the pipeline actually manages.
    """
    import os

    real = os.path.realpath(path)
    roots = [os.path.realpath(str(settings.library_dir)), os.path.realpath(str(settings.inbox_dir))]
    if not any(real == r or real.startswith(r + os.sep) for r in roots):
        raise HTTPException(status_code=403, detail="path outside managed directories")
    return real


def _serve(episode_id: int, scope: str, request: Request, token: str | None):
    """Shared guard+resolve for the streaming endpoints."""
    if not verify_media_token(token, episode_id, scope):
        raise HTTPException(status_code=401, detail="invalid or expired media token")
    video, sub, sub2 = streaming.resolve_episode_media(episode_id)
    path = {"video": video, "subtitle": sub, "subtitle2": sub2}.get(scope)
    if not path or not os.path.exists(path):
        raise HTTPException(status_code=404, detail=f"episode {episode_id} has no {scope} on disk")
    return path


@router.get("/episode/{episode_id}/video")
@router.head("/episode/{episode_id}/video")
def episode_video(episode_id: int, request: Request, token: str | None = Query(default=None)):
    """Stream the episode MP4 with HTTP Range. `?token=` is a short-lived signed token."""
    path = _serve(episode_id, "video", request, token)
    return streaming.range_response(
        request, path, default_type="video/mp4", download_name=os.path.basename(path)
    )


def _maybe_vtt(path: str, fmt: str | None):
    """`?format=vtt` converts an .srt/.ass on the fly — the in-browser fallback
    player's <track> element only accepts WebVTT."""
    if (fmt or "").lower() != "vtt":
        return None
    import pysubs2
    from fastapi.responses import Response

    subs = pysubs2.load(path)
    return Response(
        content=subs.to_string("vtt"),
        media_type="text/vtt; charset=utf-8",
    )


@router.get("/episode/{episode_id}/subtitle")
@router.head("/episode/{episode_id}/subtitle")
def episode_subtitle(episode_id: int, request: Request, token: str | None = Query(default=None),
                     format: str | None = Query(default=None)):
    """Stream the episode's best Japanese (study) subtitle. Range-capable but tiny.
    `?format=vtt` converts for the in-browser player's <track>."""
    path = _serve(episode_id, "subtitle", request, token)
    vtt = _maybe_vtt(path, format)
    if vtt is not None:
        return vtt
    return streaming.range_response(
        request, path, default_type="text/plain; charset=utf-8",
        download_name=os.path.basename(path),
    )


@router.get("/episode/{episode_id}/subtitle2")
@router.head("/episode/{episode_id}/subtitle2")
def episode_subtitle2(episode_id: int, request: Request, token: str | None = Query(default=None),
                      format: str | None = Query(default=None)):
    """Stream the episode's English secondary (reference) subtitle, if any."""
    path = _serve(episode_id, "subtitle2", request, token)
    vtt = _maybe_vtt(path, format)
    if vtt is not None:
        return vtt
    return streaming.range_response(
        request, path, default_type="text/plain; charset=utf-8",
        download_name=os.path.basename(path),
    )


# ---------------------------------------------------------------------------
# TestShow fixture endpoints — serve lib/TestShow for the Connector self-check
# (a smoke Play that verifies the whole inject → tokenize → panel chain against
# a known baseline; see /api/connector/selfcheck).
# ---------------------------------------------------------------------------
_TESTSHOW_DIR = settings.mimi_lab_db.parent.parent / "lib" / "TestShow"
_TESTSHOW_FILES = {
    "video": "TestShow - S01E01.mp4",
    "subtitle": "TestShow - S01E01.ja.srt",
}


@router.get("/testshow/{scope}")
@router.head("/testshow/{scope}")
def testshow_media(scope: str, request: Request, token: str | None = Query(default=None)):
    if scope not in _TESTSHOW_FILES:
        raise HTTPException(status_code=404, detail="unknown scope")
    if not verify_media_token(token, 0, f"testshow-{scope}"):
        raise HTTPException(status_code=401, detail="invalid or expired media token")
    path = _TESTSHOW_DIR / _TESTSHOW_FILES[scope]
    if not path.exists():
        raise HTTPException(status_code=404, detail="TestShow fixture missing")
    return streaming.range_response(
        request, str(path),
        default_type="video/mp4" if scope == "video" else "text/plain; charset=utf-8",
        download_name=path.name,
    )


@router.post("/probe", dependencies=[Depends(require_token)])
def probe(payload: dict = Body(...)):
    path = payload.get("path")
    if not path:
        raise HTTPException(status_code=422, detail="path is required")
    _validate_managed_path(path)
    try:
        return service.probe(path)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/postprocess", dependencies=[Depends(require_token)])
def postprocess(payload: dict = Body(...)):
    """Run the post-process pipeline synchronously (for testing/admin).

    Auth: the shared token (no-op in loopback/dev mode). Body: {path: "<video
    file or dir>"} or {download_id: <int>}. A `path` must live under the managed
    library/inbox roots.
    """
    if not payload.get("path") and payload.get("download_id") is None:
        raise HTTPException(status_code=422, detail="path or download_id required")
    if payload.get("path"):
        _validate_managed_path(payload["path"])
    return service.postprocess(payload)
