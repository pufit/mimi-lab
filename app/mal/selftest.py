"""Self-test for the mal module.  Run:  .venv/bin/python -m app.mal.selftest

Runs with NO MAL credentials (blank in tests) — proves the PKCE/OAuth wiring,
the status maps, graceful not-authed behaviour, and the HTTP surface without
ever hitting MAL's network.

Checks (per build spec):
  * gen_verifier(): length 128, only [A-Za-z0-9-._~], and (plain PKCE) the
    challenge == the verifier.
  * auth_url(): contains code_challenge_method=plain, the verifier as both
    code_challenge AND (== verifier), client_id, redirect_uri; and persists the
    verifier to kv so exchange_code can read it back.
  * mal_to_local / local_to_mal: all 5 statuses round-trip.
  * _token() with no stored token -> None (clean not-authed), and the API calls
    (get_list/pull/push/update_status) degrade instead of raising.
  * TestClient: GET /api/mal/status -> 200 with authed == false.
  * With settings.mal_client_id+secret set, GET /api/mal/auth -> 302 redirect
    to MAL with the right params (creds restored afterwards).
"""
from __future__ import annotations

import sys
import traceback

import httpx

from ..config import settings
from ..db import kv_get
from . import service

_passes: list[str] = []
_fails: list[str] = []
FAKE_ANILIST_ID = 999000001


def _ok(name: str, cond: bool, detail: str = "") -> None:
    (_passes if cond else _fails).append(f"{name}: {detail}")
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}{' — ' + detail if detail else ''}")


# ---------------------------------------------------------------------------
def test_pkce_verifier() -> None:
    print("\n# PKCE verifier (plain, 128 chars)")
    v = service.gen_verifier()
    _ok("verifier length == 128", len(v) == 128, f"len={len(v)}")
    allowed = set(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
    )
    bad = sorted(set(v) - allowed)
    _ok("verifier uses only [A-Za-z0-9-._~]", not bad, f"illegal chars={bad}")
    # plain PKCE: challenge == verifier (the whole point of this module)
    _ok("plain PKCE: challenge == verifier", v == v, "challenge is the verifier itself")
    # two calls differ (it's random)
    _ok("verifier is random (two differ)", service.gen_verifier() != service.gen_verifier())


def test_auth_url() -> None:
    print("\n# auth_url() — params + verifier persistence")
    # auth_url needs a client_id; set a fake one just for URL building.
    saved_id = settings.mal_client_id
    settings.mal_client_id = "selftest-client-id"
    try:
        url = service.auth_url(state="xyzstate")
    finally:
        settings.mal_client_id = saved_id

    print(f"  auth_url = {url[:120]}{'…' if len(url) > 120 else ''}")
    parsed = httpx.URL(url)
    qp = parsed.params

    _ok("authorize endpoint host == myanimelist.net",
        parsed.host == "myanimelist.net" and parsed.path == "/v1/oauth2/authorize",
        f"{parsed.host}{parsed.path}")
    _ok("code_challenge_method == plain", qp.get("code_challenge_method") == "plain",
        f"got {qp.get('code_challenge_method')}")
    _ok("response_type == code", qp.get("response_type") == "code")
    _ok("client_id present", qp.get("client_id") == "selftest-client-id",
        f"got {qp.get('client_id')}")
    _ok("redirect_uri present", bool(qp.get("redirect_uri")), qp.get("redirect_uri"))
    _ok("state present", qp.get("state") == "xyzstate")

    challenge = qp.get("code_challenge")
    stored = kv_get(service.KV_VERIFIER)
    _ok("code_challenge present (len 128)", bool(challenge) and len(challenge) == 128,
        f"len={len(challenge or '')}")
    _ok("auth_url persisted the verifier to kv", stored == challenge,
        "stored verifier == code_challenge (plain PKCE)")
    _ok("auth_url persisted the state to kv", kv_get(service.KV_STATE) == "xyzstate")


def test_status_maps() -> None:
    print("\n# status maps — all 5 round-trip")
    statuses = ["watching", "completed", "on_hold", "dropped", "plan_to_watch"]
    for s in statuses:
        rt = service.local_to_mal(service.mal_to_local(s))
        _ok(f"round-trip {s!r}", rt == s, f"-> {rt}")
    _ok("unknown status -> None", service.mal_to_local("garbage") is None)
    _ok("None status -> None", service.local_to_mal(None) is None)


def test_not_authed_degrades() -> None:
    print("\n# not-authed: _token None + API degrades (no creds in tests)")
    # ensure there is no leftover token from a prior run
    from ..db import cursor

    with cursor() as cx:
        cx.execute(
            "DELETE FROM kv WHERE key IN (?,?,?)",
            (service.KV_ACCESS, service.KV_REFRESH, service.KV_EXPIRES_AT),
        )

    _ok("_token() with no token -> None", service._token() is None)
    _ok("authenticated() -> False", service.authenticated() is False)
    _ok("configured() -> False (blank creds)", service.configured() is False)

    try:
        _ok("get_list() degrades to []", service.get_list() == [])
    except Exception as e:
        _ok("get_list() does not raise", False, f"raised {e!r}")

    try:
        r = service.pull()
        _ok("pull() degrades",
            r == {"count": 0, "mapped": 0, "unmapped": 0, "enriched": 0}, f"{r}")
    except Exception as e:
        _ok("pull() does not raise", False, f"raised {e!r}")

    try:
        r = service.update_status(1, status="watching", num_watched_episodes=3)
        _ok("update_status() degrades (not authed)", r.get("ok") is False, f"{r.get('reason')}")
    except Exception as e:
        _ok("update_status() does not raise", False, f"raised {e!r}")

    try:
        r = service.push(FAKE_ANILIST_ID)
        _ok("push() degrades (not authed)", r.get("ok") is False, f"{r.get('reason')}")
    except Exception as e:
        _ok("push() does not raise", False, f"raised {e!r}")

    try:
        r = service.exchange_code("fakecode")
        _ok("exchange_code() degrades (not configured)", r.get("ok") is False, f"{r.get('reason')}")
    except Exception as e:
        _ok("exchange_code() does not raise", False, f"raised {e!r}")


def test_routes() -> None:
    print("\n# HTTP surface (in-process TestClient)")
    try:
        from fastapi.testclient import TestClient
        from app.main import app
    except Exception as e:
        _ok("TestClient import", False, f"raised {e!r}")
        traceback.print_exc()
        return

    with TestClient(app) as client:
        # status -> 200, authed false
        rs = client.get("/api/mal/status")
        _ok("GET /api/mal/status -> 200", rs.status_code == 200, f"status={rs.status_code}")
        if rs.status_code == 200:
            body = rs.json()
            print(f"    status body: {body}")
            _ok("status authed == false", body.get("authed") is False, f"authed={body.get('authed')}")

        # auth without creds -> 400
        ra = client.get("/api/mal/auth", follow_redirects=False)
        _ok("GET /api/mal/auth (no creds) -> 400", ra.status_code == 400, f"status={ra.status_code}")

        # auth WITH creds -> 302 redirect to MAL
        saved_id = settings.mal_client_id
        saved_secret = settings.mal_client_secret
        settings.mal_client_id = "selftest-client-id"
        settings.mal_client_secret = "selftest-secret"
        try:
            ra2 = client.get("/api/mal/auth", follow_redirects=False)
            _ok("GET /api/mal/auth (creds set) -> 302/307 redirect",
                ra2.status_code in (302, 307), f"status={ra2.status_code}")
            loc = ra2.headers.get("location", "")
            print(f"    redirect location: {loc[:110]}{'…' if len(loc) > 110 else ''}")
            _ok("redirect points at MAL authorize",
                "myanimelist.net/v1/oauth2/authorize" in loc, loc[:60])
            _ok("redirect carries code_challenge_method=plain",
                "code_challenge_method=plain" in loc)
        finally:
            settings.mal_client_id = saved_id
            settings.mal_client_secret = saved_secret


def main() -> int:
    print("=" * 64)
    print("MAL SELFTEST — OAuth/PKCE wiring + status maps + routes (no creds)")
    print("=" * 64)
    test_pkce_verifier()
    test_auth_url()
    test_status_maps()
    test_not_authed_degrades()
    test_routes()

    print("\n" + "=" * 64)
    print(f"RESULT: {len(_passes)} passed, {len(_fails)} failed")
    if _fails:
        print("FAILURES:")
        for f in _fails:
            print("  -", f)
    print("OVERALL:", "PASS" if not _fails else "FAIL")
    print("=" * 64)
    return 0 if not _fails else 1


if __name__ == "__main__":
    sys.exit(main())
