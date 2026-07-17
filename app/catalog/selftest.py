"""Live self-test for the catalog module.

Run:  .venv/bin/python -m app.catalog.selftest

Uses the configured DB with a clearly fake high anilist_id, then deletes the
test rows. For isolation, point MIMI_LAB_DB at a temporary path. Also
exercises the routes in-process via FastAPI TestClient.
"""
from __future__ import annotations

import sys

from ..db import cursor, init_db
from . import service

# clearly-fake high id so we never collide with a real AniList title
FAKE_ID = 99000001
FAKE_MAL = 99000002

_passed = 0
_failed = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"PASS: {label}" + (f" — {detail}" if detail else ""))
    else:
        _failed += 1
        print(f"FAIL: {label}" + (f" — {detail}" if detail else ""))


def _cleanup() -> None:
    with cursor() as cx:
        cx.execute("DELETE FROM episodes WHERE anilist_id=?", (FAKE_ID,))
        cx.execute("DELETE FROM titles WHERE anilist_id=?", (FAKE_ID,))


def main() -> int:
    print("=" * 78)
    print("CATALOG SELFTEST — upsert title/episode + derived stats")
    print("=" * 78)

    init_db()
    _cleanup()  # in case a prior run died mid-way

    try:
        # --- upsert a title (raw AniList-shaped dict) -----------------------
        media = {
            "id": FAKE_ID,
            "idMal": FAKE_MAL,
            "title": {
                "romaji": "TestShow",
                "english": "TestShow",
                "native": "セルフテスト",
            },
            "episodes": 12,
            "format": "TV",
            "seasonYear": 2026,
            "coverImage": {"large": "http://example/cover.jpg"},
            "bannerImage": "http://example/banner.jpg",
            "description": "A <br>test<br/> show.",
        }
        rid = service.upsert_title(media)
        check("upsert_title returns the anilist_id", rid == FAKE_ID, f"got {rid}")

        t = service.get_title(FAKE_ID)
        check("get_title finds the row", t is not None and t.anilist_id == FAKE_ID)
        if t:
            check("title fields stored", t.romaji == "TestShow" and t.format == "TV",
                  f"romaji={t.romaji}, fmt={t.format}")
            check("description HTML cleaned (no <br>)", "<br" not in (t.description or ""),
                  repr(t.description))
            check("mal_id stored from idMal", t.mal_id == FAKE_MAL, f"mal_id={t.mal_id}")

        # --- idempotent re-upsert ------------------------------------------
        service.upsert_title(media)
        with cursor() as cx:
            n = cx.execute("SELECT COUNT(*) c FROM titles WHERE anilist_id=?", (FAKE_ID,)).fetchone()["c"]
        check("re-upsert is idempotent (still 1 row)", n == 1, f"rows={n}")

        # --- episodes -------------------------------------------------------
        ep_id = service.upsert_episode(FAKE_ID, 1, video_path="/lib/TestShow/ep1.mp4", container="mp4")
        check("upsert_episode returns an id", isinstance(ep_id, int) and ep_id > 0, f"id={ep_id}")
        service.upsert_episode(FAKE_ID, 2, video_path="/lib/TestShow/ep2.mp4", container="mp4",
                               comprehension_pct=80.0)
        service.upsert_episode(FAKE_ID, 1, comprehension_pct=60.0)  # update existing

        eps = service.list_episodes(FAKE_ID)
        check("list_episodes fills the declared 12-episode season", len(eps) == 12, f"n={len(eps)}")
        ep1 = next((e for e in eps if e.ep_number == 1), None)
        check("episode update preserved video_path + applied comprehension",
              ep1 is not None and ep1.video_path == "/lib/TestShow/ep1.mp4" and ep1.comprehension_pct == 60.0,
              f"ep1={ep1.video_path if ep1 else None}, pct={ep1.comprehension_pct if ep1 else None}")
        check("has_subtitle False (no subs ingested)", ep1 is not None and ep1.has_subtitle is False)

        # --- set_watched ----------------------------------------------------
        ok = service.set_watched(ep_id, True)
        check("set_watched returns True", ok is True)
        eps2 = service.list_episodes(FAKE_ID)
        ep1b = next((e for e in eps2 if e.id == ep_id), None)
        check("watched flag persisted", ep1b is not None and ep1b.watched is True)

        # --- derived grid stats --------------------------------------------
        titles = service.get_titles()
        mine = next((x for x in titles if x.anilist_id == FAKE_ID), None)
        check("get_titles includes the test title", mine is not None)
        if mine:
            check("episode_count_local == 12", mine.episode_count_local == 12,
                  f"count={mine.episode_count_local}")
            # avg of 60 and 80 == 70
            check("avg_comprehension == 70.0", mine.avg_comprehension == 70.0,
                  f"avg={mine.avg_comprehension}")

        # --- routes in-process ---------------------------------------------
        try:
            from fastapi.testclient import TestClient
            from app.main import app

            with TestClient(app) as client:
                r = client.get("/api/catalog/titles")
                check("GET /api/catalog/titles -> 200", r.status_code == 200, f"status={r.status_code}")
                if r.status_code == 200:
                    ids = [t["anilist_id"] for t in r.json()]
                    check("titles route lists the test title", FAKE_ID in ids)

                r2 = client.get(f"/api/catalog/titles/{FAKE_ID}/episodes")
                check("GET /api/catalog/titles/{id}/episodes -> 200", r2.status_code == 200,
                      f"status={r2.status_code}")

                r3 = client.post(f"/api/catalog/episodes/{ep_id}/watched", json={"watched": False})
                check("POST /api/catalog/episodes/{id}/watched -> 200", r3.status_code == 200,
                      f"status={r3.status_code}")
        except Exception as e:
            check("route checks", False, f"exception: {e}")

    finally:
        _cleanup()
        # verify cleanup
        with cursor() as cx:
            left = cx.execute("SELECT COUNT(*) c FROM titles WHERE anilist_id=?", (FAKE_ID,)).fetchone()["c"]
        check("cleanup removed test rows", left == 0, f"left={left}")

    print("\n" + "=" * 78)
    print(f"CATALOG SELFTEST: {_passed} passed, {_failed} failed")
    print("=" * 78)
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
