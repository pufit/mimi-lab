"""Known-words HTTP routes (notes/REMOTE_PLAYBACK_DESIGN.md §3, P2).

  POST /known/upload   ← the Connector pushes Migaku's WordList (auth)
  POST /known/sync     → ask the connected Connector to push a fresh copy
  GET  /known/summary  counts by status
"""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from ..connector.service import ConnectorOffline, ConnectorTimeout
from ..models import KnownWordsSummary
from ..security import require_token
from . import service

router = APIRouter(prefix="/known", tags=["known"])


@router.post("/upload", dependencies=[Depends(require_token)])
def upload(payload: dict = Body(...)):
    """Receive a pushed WordList from the Connector.

    Body: {"words": [{dictForm, reading, knownStatus}, …]} (a bare list is also
    accepted). Authenticated with the shared token.
    """
    words = payload.get("words") if isinstance(payload, dict) else payload
    if not isinstance(words, list):
        raise HTTPException(status_code=422, detail="expected {words: [...]} or a list")
    return service.upload_known_words(words)


@router.post("/sync")
async def sync(device_id: str | None = Query(default=None)):
    """Ask a connected Connector to push a fresh WordList, then return the
    summary. `?device_id=` targets a specific device; default routing otherwise."""
    try:
        return await service.request_sync(device_id=device_id)
    except ConnectorOffline as e:
        raise HTTPException(
            status_code=409,
            detail=str(e) if device_id
            else "Connector offline — start it on the machine where you watch.",
        )
    except ConnectorTimeout as e:
        raise HTTPException(status_code=504, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"connector error: {e}")


@router.get("/summary", response_model=KnownWordsSummary)
def get_summary():
    """Counts of cached known-words by status."""
    return service.summary()


@router.get("/growth")
def get_growth(days: int = 7):
    """Vocabulary growth over the last `days` — {known, delta, since} — for the
    '+N known this week' stat. Empty history returns delta 0."""
    return service.growth(days=days)
