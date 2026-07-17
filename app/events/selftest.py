"""Self-test for the events backend + pipeline automation wiring.

Run:  .venv/bin/python -m app.events.selftest

Covers:
  * events round-trip: record -> list -> unread-count -> mark-read
  * job handlers registered: comprehension, comprehension_exact, late_sub_sweep
  * late_sub_sweep enqueues a subtitle_fetch for a video-but-no-sub episode
  * register_periodics() runs cleanly (scheduler started first)
  * `from app.main import app` imports clean

All fake DB rows are cleaned up at the end.
"""
from __future__ import annotations

import sys

from ..db import connect, cursor, init_db


def _section(name: str) -> None:
    print(f"\n--- {name} ---")


def main() -> int:
    init_db()
    failures: list[str] = []

    def check(cond: bool, msg: str) -> None:
        status = "PASS" if cond else "FAIL"
        print(f"  [{status}] {msg}")
        if not cond:
            failures.append(msg)

    # ----------------------------------------------------------------- events
    _section("events round-trip")
    from . import service as events

    marker = "SELFTEST::events"
    before_unread = events.unread_count()
    eid = events.record("system", marker, "info", detail="hello", meta={"k": 1})
    check(isinstance(eid, int) and eid > 0, f"record() returned an id ({eid})")

    listed = events.list_events(limit=10)
    found = next((e for e in listed if e["id"] == eid), None)
    check(found is not None, "recorded event appears in list_events()")
    if found:
        check(found["category"] == "system" and found["title"] == marker
              and found["kind"] == "info" and found["read"] is False,
              "listed event has correct fields (category/title/kind/read)")

    after_unread = events.unread_count()
    check(after_unread == before_unread + 1,
          f"unread_count incremented ({before_unread} -> {after_unread})")

    only_unread = events.list_events(limit=50, unread_only=True)
    check(all(e["read"] is False for e in only_unread),
          "unread_only=True returns only unread events")

    n = events.mark_read(ids=[eid])
    check(n == 1, f"mark_read([{eid}]) updated 1 row")
    after_read = events.unread_count()
    check(after_read == before_unread, f"unread_count back to baseline ({after_read})")

    relisted = events.list_events(limit=50)
    re_found = next((e for e in relisted if e["id"] == eid), None)
    check(re_found is not None and re_found["read"] is True,
          "event now marked read in list")

    # mark_read(all=True) should not raise and returns an int >= 0
    n_all = events.mark_read(all=True)
    check(isinstance(n_all, int) and n_all >= 0,
          f"mark_read(all=True) returned an int ({n_all})")

    # --------------------------------------------------------------- handlers
    _section("job handlers registered")
    # importing the services registers their handlers at import time
    import app.subs.service  # noqa: F401
    import app.learn.service  # noqa: F401
    from ..jobs.service import _handlers

    for h in ("subtitle_fetch", "comprehension", "comprehension_exact", "late_sub_sweep"):
        check(h in _handlers, f"handler '{h}' is registered")

    # --------------------------------------------------- late_sub_sweep enqueue
    _section("late_sub_sweep enqueues subtitle_fetch")
    from ..subs.service import late_sub_sweep

    fake_anilist_id = -987654  # negative id -> won't collide with real catalog rows
    ep_id = None
    job_id = None
    try:
        with cursor() as cx:
            cx.execute("INSERT OR IGNORE INTO titles(anilist_id, romaji) VALUES(?, ?)",
                       (fake_anilist_id, "SELFTEST Show"))
            cur = cx.execute(
                "INSERT INTO episodes(anilist_id, ep_number, video_path) VALUES(?,?,?)",
                (fake_anilist_id, 1, "/tmp/selftest_fake_video.mp4"),
            )
            ep_id = cur.lastrowid

        # snapshot existing subtitle_fetch jobs for this episode, then sweep
        res = late_sub_sweep({"cap": 50})
        check(ep_id in res.get("episode_ids", []),
              f"sweep targeted the seeded episode (id={ep_id})")

        with connect() as cx:
            row = cx.execute(
                "SELECT id, payload_json FROM jobs "
                "WHERE type='subtitle_fetch' AND payload_json LIKE ? "
                "ORDER BY id DESC LIMIT 1",
                (f'%"episode_id": {ep_id}%',),
            ).fetchone()
        check(row is not None, "a subtitle_fetch job was enqueued for the seeded episode")
        if row:
            job_id = row["id"]
    finally:
        # cleanup fake rows
        with cursor() as cx:
            if job_id is not None:
                cx.execute("DELETE FROM jobs WHERE id=?", (job_id,))
            # delete any stray subtitle_fetch jobs we created for this episode
            if ep_id is not None:
                cx.execute(
                    "DELETE FROM jobs WHERE type='subtitle_fetch' AND payload_json LIKE ?",
                    (f'%"episode_id": {ep_id}%',),
                )
                cx.execute("DELETE FROM episodes WHERE id=?", (ep_id,))
            cx.execute("DELETE FROM titles WHERE anilist_id=?", (fake_anilist_id,))
        # remove the selftest events we created
        with cursor() as cx:
            cx.execute("DELETE FROM events WHERE title=?", (marker,))

    # ------------------------------------------------------------ periodics
    _section("register_periodics()")
    from ..jobs.service import start_scheduler, stop_scheduler
    from ..jobs.periodics import register_periodics

    try:
        start_scheduler()
        register_periodics()
        check(True, "register_periodics() ran without error")
    except Exception as e:
        check(False, f"register_periodics() raised: {e}")
    finally:
        stop_scheduler()

    # ----------------------------------------------------------- app import
    _section("app.main imports clean")
    try:
        import importlib
        import app.main as appmain
        importlib.reload(appmain)
        check(getattr(appmain, "app", None) is not None, "from app.main import app works")
    except Exception as e:
        check(False, f"importing app.main raised: {e}")

    # --------------------------------------------------------------- verdict
    _section("RESULT")
    if failures:
        print(f"\nFAIL — {len(failures)} check(s) failed:")
        for f in failures:
            print(f"   - {f}")
        return 1
    print("\nPASS — all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
