"""Connector WebSocket relay + status (notes/REMOTE_PLAYBACK_DESIGN.md §3).

  WS   /api/connector/ws?token=…   a Connector dials out, registers (with its
                                   device_id/device_name) + heartbeats; multiple
                                   devices attach concurrently
  GET  /api/connector/status       aggregate {connected, chrome, migaku} for the
                                   UI chip + per-device `devices` list
"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import PlainTextResponse, Response

from ..config import settings
from ..security import bearer_from, check_secret, require_token
from . import packaging, service

log = logging.getLogger("mimi_lab.connector")

router = APIRouter(prefix="/connector", tags=["connector"])


@router.get("/status")
def status() -> dict:
    """Liveness of the user-side Connector(s) — legacy aggregate fields for the
    UI chip plus a per-device list (`devices`) when several machines connect."""
    return service.registry.status()


@router.get("/setup")
def setup(request: Request) -> dict:
    """Copy-paste setup details for the Connector (Settings → Connector page).

    Returns the server URL + shared token the Connector needs. Single-user, self-
    hosted: the UI is the trusted surface, same as the rest of the app.
    """
    base = (settings.server_public_url or str(request.base_url)).rstrip("/")
    token = settings.mimi_lab_token
    return {
        "server_url": base,
        "ws_url": base.replace("http", "ws", 1) + "/api/connector/ws",
        "has_token": bool(token),
        "token": token,
        # one-line, self-updating install command for the watching machine
        "install_cmd": f'curl -fsSL "{base}/api/connector/install.sh?token={token}" | bash',
    }


@router.get("/install.sh", dependencies=[Depends(require_token)])
def install_sh(request: Request, token: str | None = Query(default=None)):
    """A baked `curl … | bash` installer for the watching machine.

    Downloads the Connector bundle, installs deps, and runs a self-updating
    launcher. Auth via `?token=` (the same token gets baked in for the launcher).
    """
    base = (settings.server_public_url or str(request.base_url)).rstrip("/")
    script = packaging.install_script(base, token or settings.mimi_lab_token or "")
    return PlainTextResponse(script, media_type="text/x-shellscript")


@router.get("/bundle.tgz", dependencies=[Depends(require_token)])
def bundle_tgz():
    """The Connector source as a tar.gz (no node_modules/.env). Auth via `?token=`."""
    return Response(
        content=packaging.build_bundle(),
        media_type="application/gzip",
        headers={"content-disposition": 'attachment; filename="mimi-lab-connector.tgz"'},
    )


@router.post("/selfcheck")
async def selfcheck(request: Request, device_id: str | None = Query(default=None)):
    """End-to-end smoke test of the whole Migaku integration: the Connector
    plays the server-hosted TestShow fixture and returns Migaku's own verify +
    panel stats. The result is compared to the recorded baseline — this is the
    drift guard for the browser-facing couplings (file-input seam, token
    attrs, ComprehensionStats panel), which the tokenizer drift check can't see.

    `?device_id=` targets a specific device; default routing otherwise.
    """
    import json as _json

    from ..db import kv_get, kv_set
    from ..security import make_media_token

    base = (settings.server_public_url or str(request.base_url)).rstrip("/")
    cmd = {
        "cmd": "selfcheck",
        "video_url": f"{base}/api/media/testshow/video?token={make_media_token(0, 'testshow-video')}",
        "sub_url": f"{base}/api/media/testshow/subtitle?token={make_media_token(0, 'testshow-subtitle')}",
    }
    try:
        ack = await service.registry.send_command(cmd, timeout=90.0, device_id=device_id)
    except Exception as e:
        return {"ok": False, "error": str(e)}

    result = ack.get("result") or {}
    verify = result.get("verify") or {}
    stats = result.get("stats") or {}
    checks = {
        "played": bool(verify.get("hasVideo")),
        "tokenized": (verify.get("migakuTokens") or 0) > 0,
        "panel_scraped": isinstance(stats.get("pct"), (int, float)),
    }

    baseline_raw = kv_get("selfcheck.baseline")
    drift = None
    if checks["panel_scraped"]:
        current = {"pct": stats.get("pct"), "known": stats.get("known"),
                   "unknown": stats.get("unknown"), "ignored": stats.get("ignored")}
        if baseline_raw:
            try:
                baseline = _json.loads(baseline_raw)
                drift = {k: {"baseline": baseline.get(k), "current": current.get(k)}
                         for k in current
                         if baseline.get(k) is not None and baseline.get(k) != current.get(k)}
            except Exception:
                drift = None
        else:
            kv_set("selfcheck.baseline", _json.dumps(current))

    ok = all(checks.values()) and not drift
    return {"ok": ok, "checks": checks, "verify": verify, "stats": stats,
            "drift": drift or None, "ext_version": result.get("extVersion")}


@router.websocket("/ws")
async def ws(websocket: WebSocket, token: str | None = Query(default=None)) -> None:
    """A Connector's outbound link. It registers (carrying its device identity),
    heartbeats, and receives play / sync-known / selfcheck commands (acking
    each). One socket per device; devices coexist."""
    presented = bearer_from(websocket.headers.get("authorization"), token)
    if not check_secret(presented):
        # 4401 = our convention for "unauthorized" on the WS close frame.
        await websocket.close(code=4401)
        log.warning("connector ws rejected: bad/missing token")
        return

    await websocket.accept()
    await service.registry.attach(websocket)
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except Exception:
                log.debug("connector: dropping non-JSON frame")
                continue
            if isinstance(msg, dict):
                await service.registry.handle_message(websocket, msg)
    except WebSocketDisconnect:
        pass
    except Exception as e:  # pragma: no cover - defensive
        log.warning("connector ws error: %s", e)
    finally:
        await service.registry.detach(websocket)
