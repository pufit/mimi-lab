"""MyAnimeList sync service — two-way list sync against MAL API v2.

OAuth2 is Authorization Code + PKCE, but MAL only supports
``code_challenge_method=plain`` (NO S256): the challenge equals the verifier.
We generate a 128-char verifier from the unreserved set ``[A-Za-z0-9-._~]``,
and ``client_secret`` is REQUIRED on the token exchange (register the app as
type ``web``).

Pipelines (notes/ARCHITECTURE.md §5.3, last-writer-wins):
  PULL (nightly):  GET /users/@me/animelist?fields=list_status&limit=1000 (paged
                   via paging.next) -> map mal_id->anilist_id (Fribb) ->
                   upsert titles' mal_* columns.
  PUSH (on change): PATCH /anime/{id}/my_list_status (x-www-form-urlencoded;
                   write key `num_watched_episodes`) with status/score/progress.

Tokens (access/refresh/expiry) live in `kv`; the refresh token rotates on every
refresh and is persisted. Creds come from settings (BLANK in tests) so every
network path degrades gracefully instead of raising.
"""
from __future__ import annotations

import logging
import secrets
import time
from typing import Optional

import httpx

from ..config import settings
from ..db import cursor, kv_get, kv_set

log = logging.getLogger("mimi_lab.mal")

# --- MAL endpoints ---------------------------------------------------------
API_BASE = "https://api.myanimelist.net/v2"
OAUTH_AUTHORIZE = "https://myanimelist.net/v1/oauth2/authorize"
OAUTH_TOKEN = "https://myanimelist.net/v1/oauth2/token"

# --- PKCE verifier ---------------------------------------------------------
# Unreserved characters per RFC 7636 §4.1: ALPHA / DIGIT / "-" / "." / "_" / "~"
_VERIFIER_ALPHABET = (
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
)
VERIFIER_LEN = 128

# --- kv keys ---------------------------------------------------------------
KV_VERIFIER = "mal_verifier"
KV_STATE = "mal_oauth_state"
KV_ACCESS = "mal_access"
KV_REFRESH = "mal_refresh"
KV_EXPIRES_AT = "mal_expires_at"
KV_LAST_SYNC = "mal_last_sync"

# refresh a little early so a request never races the expiry
_REFRESH_MARGIN_S = 60
_RATE_DELAY_S = 1.0  # no documented MAL rate limit -> ~1 req/s

# --- status enums ----------------------------------------------------------
# MAL list_status.status enum <-> our local mal_status column.
# They use the same vocabulary, so the maps are identity (+ normalisation /
# validation). Kept explicit so the round-trip is verifiable in the selftest.
_MAL_STATUSES = (
    "watching",
    "completed",
    "on_hold",
    "dropped",
    "plan_to_watch",
)


# ===========================================================================
# PKCE + OAuth
# ===========================================================================
def gen_verifier() -> str:
    """Generate a 128-char PKCE code verifier from the unreserved alphabet."""
    return "".join(secrets.choice(_VERIFIER_ALPHABET) for _ in range(VERIFIER_LEN))


def configured() -> bool:
    """True only when both MAL client credentials are present."""
    return bool(settings.mal_client_id and settings.mal_client_secret)


def auth_url(state: Optional[str] = None) -> str:
    """Build the MAL authorize URL and persist the verifier + state to kv.

    PKCE plain: ``code_challenge == code_verifier`` and
    ``code_challenge_method=plain``. A fresh verifier is generated and stored so
    the matching `exchange_code` can read it back. Raises RuntimeError if the
    client id is not configured.
    """
    if not settings.mal_client_id:
        raise RuntimeError("MAL is not configured (settings.mal_client_id is blank)")

    verifier = gen_verifier()
    if state is None:
        state = secrets.token_urlsafe(24)

    # persist before redirecting so the callback can complete the exchange
    kv_set(KV_VERIFIER, verifier)
    kv_set(KV_STATE, state)

    params = {
        "response_type": "code",
        "client_id": settings.mal_client_id,
        "code_challenge": verifier,        # plain: challenge == verifier
        "code_challenge_method": "plain",
        "state": state,
        "redirect_uri": settings.mal_redirect_uri,
    }
    # httpx.QueryParams percent-encodes each value correctly (redirect_uri etc.)
    return f"{OAUTH_AUTHORIZE}?{httpx.QueryParams(params)}"


def exchange_code(code: str) -> dict:
    """Exchange an authorization `code` for tokens (form POST w/ secret+verifier).

    Stores mal_access / mal_refresh / mal_expires_at in kv. Returns a small
    summary {ok, expires_in, ...} (never the raw tokens).
    """
    if not configured():
        return {"ok": False, "reason": "mal not configured"}

    verifier = kv_get(KV_VERIFIER)
    if not verifier:
        return {"ok": False, "reason": "no stored code_verifier; call auth_url first"}

    data = {
        "client_id": settings.mal_client_id,
        "client_secret": settings.mal_client_secret,  # REQUIRED on exchange
        "grant_type": "authorization_code",
        "code": code,
        "code_verifier": verifier,
        "redirect_uri": settings.mal_redirect_uri,
    }
    try:
        with httpx.Client(timeout=30) as c:
            r = c.post(
                OAUTH_TOKEN,
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        r.raise_for_status()
        tok = r.json()
    except Exception as e:
        log.warning("MAL token exchange failed: %s", e)
        return {"ok": False, "reason": str(e)}

    _store_tokens(tok)
    return {"ok": True, "expires_in": tok.get("expires_in"), "token_type": tok.get("token_type")}


def refresh() -> dict:
    """Refresh the access token using the stored (rotating) refresh token."""
    if not configured():
        return {"ok": False, "reason": "mal not configured"}
    refresh_token = kv_get(KV_REFRESH)
    if not refresh_token:
        return {"ok": False, "reason": "no refresh token; not authenticated"}

    data = {
        "client_id": settings.mal_client_id,
        "client_secret": settings.mal_client_secret,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }
    try:
        with httpx.Client(timeout=30) as c:
            r = c.post(
                OAUTH_TOKEN,
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        r.raise_for_status()
        tok = r.json()
    except Exception as e:
        log.warning("MAL token refresh failed: %s", e)
        return {"ok": False, "reason": str(e)}

    _store_tokens(tok)
    return {"ok": True, "expires_in": tok.get("expires_in")}


def _store_tokens(tok: dict) -> None:
    """Persist access/refresh/expiry to kv. Refresh token rotates on every
    grant, so always overwrite it when present."""
    access = tok.get("access_token")
    refresh_token = tok.get("refresh_token")
    expires_in = tok.get("expires_in")
    if access:
        kv_set(KV_ACCESS, access)
    if refresh_token:
        kv_set(KV_REFRESH, refresh_token)
    if expires_in is not None:
        try:
            kv_set(KV_EXPIRES_AT, str(int(time.time()) + int(expires_in)))
        except (TypeError, ValueError):
            pass


def _token() -> Optional[str]:
    """Return a valid access token, auto-refreshing if expired/near-expiry.

    Returns None when not authenticated (no token) — callers degrade to a
    not-authed result rather than raising.
    """
    access = kv_get(KV_ACCESS)
    if not access:
        return None
    expires_at = kv_get(KV_EXPIRES_AT)
    try:
        exp = int(expires_at) if expires_at else 0
    except (TypeError, ValueError):
        exp = 0

    if exp and time.time() >= (exp - _REFRESH_MARGIN_S):
        res = refresh()
        if res.get("ok"):
            return kv_get(KV_ACCESS)
        # refresh failed; fall back to the (possibly stale) token we have
        log.warning("token refresh failed, using existing access token: %s", res.get("reason"))
    return access


def authenticated() -> bool:
    return bool(kv_get(KV_ACCESS))


# ===========================================================================
# MAL API calls
# ===========================================================================
def get_list(limit: int = 1000) -> list[dict]:
    """Fetch the full @me anime list (status only), following `paging.next`.

    Returns a list of raw MAL nodes ``[{node:{id,title,...}, list_status:{...}}]``.
    Degrades to [] when not authenticated or on a hard error.
    """
    token = _token()
    if not token:
        log.info("get_list: not authenticated")
        return []

    out: list[dict] = []
    url = (
        f"{API_BASE}/users/@me/animelist"
        f"?fields=list_status&limit={int(limit)}&offset=0&nsfw=true"
    )
    headers = {"Authorization": f"Bearer {token}"}
    pages = 0
    try:
        with httpx.Client(timeout=60) as c:
            while url:
                r = _get_with_backoff(c, url, headers)
                if r is None:
                    break
                body = r.json()
                out.extend(body.get("data") or [])
                url = (body.get("paging") or {}).get("next")
                pages += 1
                if url:
                    time.sleep(_RATE_DELAY_S)
                if pages > 100:  # hard stop; ~100k entries
                    break
    except Exception as e:
        log.warning("get_list failed after %d page(s): %s", pages, e)
    log.info("get_list: %d entries across %d page(s)", len(out), pages)
    return out


def _get_with_backoff(c: httpx.Client, url: str, headers: dict, *, max_retries: int = 4):
    """GET with exponential backoff. Returns the response or None on hard fail."""
    backoff = 1.0
    for attempt in range(max_retries):
        try:
            r = c.get(url, headers=headers)
            if r.status_code == 429 or r.status_code >= 500:
                wait = float(r.headers.get("Retry-After", backoff))
                log.warning("MAL %s on GET; sleeping %.1fs", r.status_code, wait)
                time.sleep(wait)
                backoff = min(backoff * 2, 30)
                continue
            r.raise_for_status()
            return r
        except httpx.HTTPStatusError as e:
            log.warning("MAL GET HTTP %s: %s", e.response.status_code, url)
            return None
        except Exception as e:
            log.warning("MAL GET error (attempt %d): %s", attempt + 1, e)
            if attempt == max_retries - 1:
                return None
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)
    return None


def update_status(
    mal_id: int,
    status: Optional[str] = None,
    score: Optional[int] = None,
    num_watched_episodes: Optional[int] = None,
) -> dict:
    """PATCH /anime/{id}/my_list_status (x-www-form-urlencoded).

    Write key for progress is **num_watched_episodes** (MAL's spelling). Only
    the provided fields are sent. Returns {ok, status?} and degrades on error.
    """
    token = _token()
    if not token:
        return {"ok": False, "reason": "not authenticated"}

    form: dict = {}
    if status is not None:
        st = local_to_mal(status)
        if st:
            form["status"] = st
    if score is not None:
        try:
            form["score"] = max(0, min(10, int(score)))  # MAL score is 0–10
        except (TypeError, ValueError):
            pass
    if num_watched_episodes is not None:
        try:
            form["num_watched_episodes"] = max(0, int(num_watched_episodes))
        except (TypeError, ValueError):
            pass

    if not form:
        return {"ok": False, "reason": "nothing to update"}

    url = f"{API_BASE}/anime/{int(mal_id)}/my_list_status"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    backoff = 1.0
    for attempt in range(4):
        try:
            with httpx.Client(timeout=30) as c:
                r = c.patch(url, data=form, headers=headers)
            if r.status_code == 429 or r.status_code >= 500:
                wait = float(r.headers.get("Retry-After", backoff))
                time.sleep(wait)
                backoff = min(backoff * 2, 30)
                continue
            r.raise_for_status()
            return {"ok": True, "mal_id": int(mal_id), "list_status": r.json()}
        except httpx.HTTPStatusError as e:
            log.warning("update_status HTTP %s for mal_id=%s", e.response.status_code, mal_id)
            return {"ok": False, "reason": f"http {e.response.status_code}"}
        except Exception as e:
            log.warning("update_status error (attempt %d): %s", attempt + 1, e)
            if attempt == 3:
                return {"ok": False, "reason": str(e)}
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)
    return {"ok": False, "reason": "exhausted retries"}


# ===========================================================================
# status mapping (5-way, round-trips)
# ===========================================================================
def mal_to_local(status: Optional[str]) -> Optional[str]:
    """Normalise a MAL list_status.status to our stored value.

    MAL and local share the vocabulary; this validates + lower-cases. Unknown
    values map to None.
    """
    if not status:
        return None
    s = str(status).strip().lower()
    return s if s in _MAL_STATUSES else None


def local_to_mal(status: Optional[str]) -> Optional[str]:
    """Map a local mal_status back to MAL's status enum (identity + validate)."""
    if not status:
        return None
    s = str(status).strip().lower()
    return s if s in _MAL_STATUSES else None


# ===========================================================================
# PULL / PUSH / status
# ===========================================================================
def pull() -> dict:
    """Pull the @me list, map mal_id->anilist_id, upsert titles' mal_* columns.

    Uses the Fribb idmap (lazy `app.match.service`) to resolve anilist ids and
    `app.catalog.service.upsert_title` to create the title row if missing, then
    writes mal_status/mal_score/mal_progress/mal_updated_at directly. Stub rows
    (no cover/romaji yet) are then enriched from AniList in one batched query.
    Returns {count, mapped, unmapped, enriched}.
    """
    nodes = get_list()
    count = len(nodes)
    mapped = 0
    unmapped = 0

    if not nodes:
        return {"count": 0, "mapped": 0, "unmapped": 0, "enriched": 0}

    # lazy imports: avoid import cycles + let the module load even if siblings
    # are mid-build.
    try:
        from ..match import service as match_service
    except Exception:  # pragma: no cover
        match_service = None
    try:
        from ..catalog import service as catalog_service
    except Exception:  # pragma: no cover
        catalog_service = None

    for n in nodes:
        node = n.get("node") or {}
        ls = n.get("list_status") or {}
        mal_id = node.get("id")
        if mal_id is None:
            unmapped += 1
            continue

        anilist_id = None
        if match_service is not None:
            try:
                anilist_id = match_service.anilist_id_for(int(mal_id))
            except Exception as e:  # idmap not downloaded, etc.
                log.debug("anilist_id_for(%s) failed: %s", mal_id, e)

        if anilist_id is None:
            unmapped += 1
            continue

        # ensure a title row exists, then write the mal_* fields
        if catalog_service is not None:
            try:
                catalog_service.upsert_title(
                    {"id": int(anilist_id), "mal_id": int(mal_id), "english": node.get("title")}
                )
            except Exception as e:
                log.debug("upsert_title(%s) skipped: %s", anilist_id, e)

        _write_mal_fields(int(anilist_id), int(mal_id), ls)
        mapped += 1

    # Titles first seen via this pull exist only as stubs (ids + the MAL title
    # string) — no cover/romaji/format, so the grid renders a blank card.
    # Backfill every stub from AniList in one batched query. Scans the whole
    # table (not just this pull) so previously-stubbed rows self-heal too.
    enriched = 0
    if mapped and catalog_service is not None and match_service is not None:
        try:
            with cursor() as cx:
                stub_ids = [
                    r["anilist_id"] for r in cx.execute(
                        "SELECT anilist_id FROM titles "
                        "WHERE cover_url IS NULL OR cover_url='' "
                        "   OR romaji IS NULL OR romaji=''"
                    ).fetchall()
                ]
            if stub_ids:
                for media in match_service.anilist_media_by_ids(stub_ids):
                    try:
                        catalog_service.upsert_title(media)
                        enriched += 1
                    except Exception as e:
                        log.debug("enrich upsert_title(%s) skipped: %s", media.get("id"), e)
                log.info("pull: enriched %d/%d stub title(s) from AniList",
                         enriched, len(stub_ids))
        except Exception as e:
            log.warning("pull: stub enrichment failed: %s", e)

    kv_set(KV_LAST_SYNC, str(int(time.time())))
    log.info("pull: %d nodes, %d mapped, %d unmapped", count, mapped, unmapped)
    return {"count": count, "mapped": mapped, "unmapped": unmapped, "enriched": enriched}


def _write_mal_fields(anilist_id: int, mal_id: int, list_status: dict) -> None:
    """Write mal_status/score/progress/updated_at onto the title row.

    Creates a bare title row if catalog didn't (so the pull never silently drops
    a mapped entry). Only updates when the title exists or can be inserted.
    """
    status = mal_to_local(list_status.get("status"))
    score = list_status.get("score")
    progress = list_status.get("num_episodes_watched")
    updated_at = list_status.get("updated_at")
    try:
        score = int(score) if score is not None else None
    except (TypeError, ValueError):
        score = None
    try:
        progress = int(progress) if progress is not None else None
    except (TypeError, ValueError):
        progress = None

    with cursor() as cx:
        cx.execute(
            """
            INSERT INTO titles (anilist_id, mal_id, mal_status, mal_score,
                                mal_progress, mal_updated_at, updated_at)
            VALUES (?,?,?,?,?,?, datetime('now'))
            ON CONFLICT(anilist_id) DO UPDATE SET
              mal_id         = COALESCE(excluded.mal_id, titles.mal_id),
              mal_status     = excluded.mal_status,
              mal_score      = excluded.mal_score,
              mal_progress   = excluded.mal_progress,
              mal_updated_at = excluded.mal_updated_at,
              updated_at     = datetime('now')
            """,
            (anilist_id, mal_id, status, score, progress, updated_at),
        )


def push(anilist_id: int) -> dict:
    """Push a single title's local watch state to MAL.

    Resolves anilist_id -> mal_id (title row first, then Fribb idmap), reads the
    local mal_status/mal_score and watched-episode count, and PATCHes MAL. LWW:
    we only push our own watched/score signal; reading is owned by pull().
    Returns {ok, ...} and degrades when unauthenticated/unmapped.
    """
    if not authenticated():
        return {"ok": False, "reason": "not authenticated"}

    with cursor() as cx:
        row = cx.execute(
            "SELECT mal_id, mal_status, mal_score, mal_progress FROM titles WHERE anilist_id=?",
            (int(anilist_id),),
        ).fetchone()
        # local progress = highest watched ep we actually have on disk
        prog_row = cx.execute(
            "SELECT MAX(ep_number) AS p FROM episodes WHERE anilist_id=? AND watched=1",
            (int(anilist_id),),
        ).fetchone()

    mal_id = row["mal_id"] if row else None
    if mal_id is None:
        try:
            from ..match import service as match_service

            mal_id = match_service.mal_id_for(int(anilist_id))
        except Exception as e:
            log.debug("push: mal_id_for(%s) failed: %s", anilist_id, e)

    if mal_id is None:
        return {"ok": False, "reason": "no mal_id for this title (unmapped)"}

    local_progress = prog_row["p"] if prog_row and prog_row["p"] is not None else None
    if local_progress is None and row is not None:
        local_progress = row["mal_progress"]

    status = row["mal_status"] if row else None
    score = row["mal_score"] if row else None

    return update_status(
        int(mal_id),
        status=status,
        score=score,
        num_watched_episodes=local_progress,
    )


def status() -> dict:
    """Auth + sync status for the UI / health checks (never returns tokens)."""
    last_sync = kv_get(KV_LAST_SYNC)
    expires_at = kv_get(KV_EXPIRES_AT)
    with cursor() as cx:
        synced = cx.execute(
            "SELECT COUNT(*) AS n FROM titles WHERE mal_status IS NOT NULL"
        ).fetchone()["n"]
    return {
        "configured": configured(),
        "authed": authenticated(),
        "has_refresh_token": bool(kv_get(KV_REFRESH)),
        "expires_at": int(expires_at) if expires_at else None,
        "last_sync": int(last_sync) if last_sync else None,
        "synced_titles": synced,
    }


# ===========================================================================
# job handler
# ===========================================================================
def _job_mal_sync(payload: dict) -> None:
    """`mal_sync` job.

    payload: {} -> full pull; {"push": <anilist_id>} -> push that title ONLY
    (no pull — pushes fire on every watched-toggle and mustn't drag a full
    list pull each time); {"push": id, "pull": true} -> both.

    Raises on failure so the queue's retry/backoff applies (a silent
    swallow-and-done here previously made sync failures invisible).
    """
    payload = payload or {}
    push_id = payload.get("push")
    do_pull = payload.get("pull", push_id is None)
    if push_id is not None:
        if not authenticated():
            log.info("mal_sync push(%s) skipped: not authenticated", push_id)
        else:
            res = push(int(push_id))
            reason = res.get("reason") or ""
            benign = reason.startswith("no mal_id") or reason == "nothing to update"
            if not res.get("ok") and not benign:
                raise RuntimeError(f"mal push({push_id}) failed: {reason}")
    if do_pull:
        if not authenticated():
            log.info("mal_sync pull skipped: not authenticated")
            return
        res = pull()
        # A 0-item pull while we previously synced a populated list is a failure
        # in disguise (expired token -> get_list degrades to []): raise so it
        # retries + surfaces instead of silently "succeeding" with nothing.
        if int(res.get("count") or 0) == 0:
            with cursor() as cx:
                had = cx.execute(
                    "SELECT COUNT(*) AS n FROM titles WHERE mal_status IS NOT NULL"
                ).fetchone()["n"]
            if had > 0:
                raise RuntimeError("mal pull returned 0 items while a previously "
                                   "synced list exists — token trouble?")


def register_jobs() -> None:
    try:
        from ..jobs.service import register

        register("mal_sync", _job_mal_sync)
    except Exception as e:  # pragma: no cover
        log.warning("could not register mal_sync handler: %s", e)


register_jobs()
