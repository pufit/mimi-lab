"""Tokens + shared-secret auth for the Connector topology.

Single-user model (notes/REMOTE_PLAYBACK_DESIGN.md §6):
  * `settings.mimi_lab_token` is ONE long-lived shared secret. The Connector
    holds it; it authenticates the Connector's WebSocket and its upload POSTs.
  * Per-play **media** URLs carry a short-lived *signed* token (`?token=…`) minted
    by the server, scoped to one episode + resource, so the bytes are self-validating
    (no server-side session state). The token is an HMAC over the same secret.

If `mimi_lab_token` is blank we are in **loopback/dev** mode: auth is disabled
(everything is allowed) — fine for a single host on localhost, but you MUST set a
token before exposing the server through a configured remote-access endpoint.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Optional

from fastapi import Header, HTTPException, Query

from .config import settings


def auth_enabled() -> bool:
    """True when a shared secret is configured (auth is enforced)."""
    return bool(settings.mimi_lab_token)


def _secret() -> bytes:
    return settings.mimi_lab_token.encode("utf-8")


def check_secret(token: Optional[str]) -> bool:
    """Constant-time compare a presented bearer token to the shared secret.

    Returns True when auth is disabled (no secret configured) — dev/loopback.
    """
    if not auth_enabled():
        return True
    if not token:
        return False
    return hmac.compare_digest(token, settings.mimi_lab_token)


def bearer_from(authorization: Optional[str], token: Optional[str]) -> Optional[str]:
    """Pull a presented secret from either a `?token=` query or a
    `Authorization: Bearer <token>` header."""
    if token:
        return token
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return None


def require_token(
    authorization: Optional[str] = Header(default=None),
    token: Optional[str] = Query(default=None),
) -> bool:
    """FastAPI dependency guarding the Connector's authenticated POST endpoints
    (known/upload, comprehension/upload). 401 unless the shared secret matches
    (or auth is disabled in dev)."""
    if not check_secret(bearer_from(authorization, token)):
        raise HTTPException(status_code=401, detail="invalid or missing token")
    return True


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def make_media_token(
    episode_id: int, scope: str, ttl: Optional[int] = None
) -> str:
    """Mint a short-lived signed token for one media resource.

    `scope` is e.g. "video" / "subtitle". The token encodes {eid, scp, exp} and
    is signed with HMAC-SHA256 over the shared secret. When no secret is
    configured the token is still produced (so URLs are uniform) but
    `verify_media_token` will accept anything in that dev mode.
    """
    ttl = settings.media_token_ttl if ttl is None else ttl
    payload = {"eid": int(episode_id), "scp": scope, "exp": int(time.time()) + int(ttl)}
    body = _b64e(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    sig = _b64e(hmac.new(_secret(), body.encode("ascii"), hashlib.sha256).digest())
    return f"{body}.{sig}"


def verify_media_token(token: Optional[str], episode_id: int, scope: str) -> bool:
    """Validate a media token against the episode id + scope + expiry.

    Returns True when auth is disabled (dev). Otherwise the signature, scope,
    episode id and expiry must all check out.
    """
    if not auth_enabled():
        return True
    if not token or "." not in token:
        return False
    body, _, sig = token.partition(".")
    expected = _b64e(hmac.new(_secret(), body.encode("ascii"), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expected):
        return False
    try:
        payload = json.loads(_b64d(body))
    except Exception:
        return False
    if payload.get("scp") != scope:
        return False
    if int(payload.get("eid", -1)) != int(episode_id):
        return False
    if int(payload.get("exp", 0)) < int(time.time()):
        return False
    return True
