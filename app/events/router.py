"""In-app events / notifications HTTP routes.

Served under /api (see app/main.py), so:
  GET  /api/events?unread=&limit=     -> recent events
  GET  /api/events/unread-count       -> {count}
  POST /api/events/read  {ids?,all?}  -> {updated}
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from . import bus, service

router = APIRouter(prefix="/events", tags=["events"])


@router.get("/stream")
async def stream() -> StreamingResponse:
    """Server-Sent Events: pushes {type:'job'|'event', ...} nudges so the UI can
    invalidate the affected queries the instant a background job finishes
    (comprehension pills, download rows, queue badge) instead of waiting for a
    poll. Text stream; the client reconnects automatically on drop."""
    return StreamingResponse(
        bus.subscribe(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # disable proxy buffering (nginx/tailscale)
        },
    )


class ReadRequest(BaseModel):
    ids: Optional[list[int]] = None
    all: bool = False


@router.get("")
def list_events(unread: bool = False, limit: int = 50) -> list[dict]:
    """Recent events (newest first). `unread=true` filters to unread only."""
    return service.list_events(limit=limit, unread_only=unread)


@router.get("/unread-count")
def unread_count() -> dict:
    """Number of unread events (for the badge)."""
    return {"count": service.unread_count()}


@router.post("/read")
def mark_read(req: ReadRequest) -> dict:
    """Mark events read by id list, or all unread when `all=true`."""
    updated = service.mark_read(ids=req.ids, all=req.all)
    return {"updated": updated}
