"""MyAnimeList HTTP routes.

Served under /api (see app.main), so these live at /api/mal/*.

  GET  /mal/auth      -> 302 redirect to MAL's OAuth consent (or 400 if creds unset)
  GET  /mal/callback  -> OAuth redirect target; exchanges ?code for tokens
  POST /mal/sync      -> pull the list now; optional ?push=<anilist_id>
  GET  /mal/status    -> auth + sync status (no tokens)
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse, RedirectResponse

from . import service

router = APIRouter(prefix="/mal", tags=["mal"])


@router.get("/auth")
def auth():
    """Redirect the browser to MAL's OAuth consent screen.

    Persists a fresh PKCE verifier + state server-side first. 400 when MAL
    credentials are not configured.
    """
    if not service.configured():
        raise HTTPException(
            status_code=400,
            detail="MAL is not configured (set MAL_CLIENT_ID / MAL_CLIENT_SECRET).",
        )
    url = service.auth_url()
    return RedirectResponse(url, status_code=302)


@router.get("/callback")
def callback(
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    error: str | None = Query(default=None),
):
    """OAuth redirect target: exchange ?code for tokens.

    Validates the returned ?state against the one we stored. Returns a small
    JSON result so the (single-user) browser tab shows success/failure.
    """
    from ..db import kv_get

    if error:
        raise HTTPException(status_code=400, detail=f"MAL auth error: {error}")
    if not code:
        raise HTTPException(status_code=400, detail="missing ?code")

    expected_state = kv_get(service.KV_STATE)
    if expected_state and state and state != expected_state:
        raise HTTPException(status_code=400, detail="state mismatch")

    result = service.exchange_code(code)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=f"token exchange failed: {result.get('reason')}")
    # Land the user back in the app (the raw-JSON page used to strand the tab);
    # kick an initial pull in the background so the library fills in right away.
    try:
        from ..jobs.service import enqueue
        enqueue("mal_sync", {})
    except Exception:
        pass
    return RedirectResponse("/settings?mal=connected", status_code=303)


@router.post("/sync")
def sync(push: int | None = Query(default=None, description="optional anilist_id to push")):
    """Pull the MAL list now (and optionally push one title)."""
    out = service.pull()
    if push is not None:
        out["push"] = service.push(int(push))
    return out


@router.get("/status")
def get_status():
    """Auth + sync status for the UI."""
    return service.status()
