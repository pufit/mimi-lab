"""Self-test for app.srs (WP-A) — run:

    MIMI_LAB_DB=/tmp/srs-a-selftest.db CLIPS_DIR=/tmp/srs-a-clips \\
        .venv/bin/python -m app.srs.selftest

Everything runs against a throw-away DB created in a temp directory: the
environment is rewritten BEFORE any `app.*` import (the same isolation
`app/connector/selftest.py` uses), so the live `data/mimi_lab.db` is never
opened, whatever `MIMI_LAB_DB` the caller passed.

Covers SRS_DESIGN §10.1 for WP-A: the FSRS-6 golden vectors of §3.12, the
state machine and day boundary (incl. DST), failed-repeat/demotion semantics,
the stack operations and their invariants, queue composition, review
idempotency + 409 + undo, the resume table, relink/re-ingest, hard delete, the
importer, the learn integration and an HTTP smoke over every §7.2 route used by
the client.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
TMP = tempfile.mkdtemp(prefix="srs_selftest_")

# MUST happen before any app.* import (app.config.settings is built at import).
os.environ.update(
    MIMI_LAB_DB=f"{TMP}/test.db",
    LIBRARY_DIR=f"{TMP}/lib",
    INBOX_DIR=f"{TMP}/inbox",
    CLIPS_DIR=f"{TMP}/clips",
    MIMI_LAB_TOKEN="selftest",
    SERVER_PUBLIC_URL="",
    ANTHROPIC_API_KEY="",
    # force the local fugashi tokenizer: the Migaku sidecar may be running on
    # this machine and the test must not depend on it.
    MIGAKU_TOK_URL="http://127.0.0.1:9",
)

from app.db import connect, cursor, init_db          # noqa: E402
from app.models import (                             # noqa: E402
    SrsBulkRequest,
    SrsCreateCard,
    SrsMove,
    SrsPatchCard,
    SrsResort,
    SrsReviewRequest,
    SrsSettingsPatch,
    SrsSwapMoment,
)
from app.srs import scheduler, service               # noqa: E402
from app.srs.constants import KNOWN_INTERVAL_DAYS, MAX_DEMOTIONS  # noqa: E402
from app.srs.snapshot import CardSpec, norm_text, snapshot_line, strip_bidi  # noqa: E402

_fails: list[str] = []
_checks = 0
UTC = timezone.utc
VIDEO = str(REPO / "lib" / "TestShow" / "TestShow - S01E01.mp4")

LINES = [
    (1, 1000, 4000, "これはテストの字幕です。", "This is a test subtitle."),
    (2, 5000, 8000, "今日はいい天気ですね。", "Nice weather today, isn't it?"),
    (3, 9000, 12000, "日本語を勉強しています。", "I'm studying Japanese."),
    (4, 13000, 17000, "ミガクのプレイヤーで自動再生のテスト中です。",
     "Testing autoplay in the Migaku player."),
    # not in the .srt: a second occurrence of 勉強 and of 天気, so alternate
    # moments and moment swaps have somewhere to go.
    (5, 18000, 21000, "毎日勉強するのは大変ですが、天気がいいと楽しいです。",
     "Studying every day is hard, but it's fun when the weather is nice."),
]


def check(name: str, cond, detail: str = "") -> bool:
    global _checks
    _checks += 1
    ok = bool(cond)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        _fails.append(name)
    return ok


def section(title: str) -> None:
    print(f"\n--- {title} ---")


def close(a, b, tol=1e-3) -> bool:
    return a is not None and b is not None and abs(float(a) - float(b)) <= tol


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

def seed() -> None:
    from app.learn.service import furigana, tokenize

    with cursor() as cx:
        cx.execute("INSERT INTO titles(anilist_id, romaji, english) VALUES(1,'TestShow','Test Show')")
        cx.execute(
            "INSERT INTO episodes(id, anilist_id, ep_number, title, video_path, duration_ms, watched) "
            "VALUES(1, 1, 1, 'Episode 1', ?, 30000, 1)",
            (VIDEO,),
        )
        cx.execute(
            "INSERT INTO subtitles(id, episode_id, source, lang, path, format, version) "
            "VALUES(1, 1, 'test', 'ja', 'x.srt', 'srt', 1)"
        )
        for idx, start, end, text, tr in LINES:
            cx.execute(
                "INSERT INTO subtitle_lines(subtitle_id, episode_id, idx, start_ms, end_ms, text, "
                "text_furigana, translation) VALUES(1,1,?,?,?,?,?,?)",
                (idx, start, end, text, furigana(text), tr),
            )
        for r in cx.execute("SELECT id, text FROM subtitle_lines").fetchall():
            for t in tokenize(r["text"]):
                cx.execute(
                    "INSERT INTO line_lemmas(line_id, episode_id, lemma, reading, pos, surface, token_source) "
                    "VALUES(?,1,?,?,?,?, 'local')",
                    (r["id"], t["lemma"], t["reading"], t["pos"], t["surface"]),
                )
        lemmas = {r["lemma"] for r in cx.execute("SELECT DISTINCT lemma FROM line_lemmas")}
        for n, lemma in enumerate(sorted(lemmas), start=1):
            cx.execute("INSERT OR IGNORE INTO lemma_freq(lemma, rank) VALUES(?,?)", (lemma, 500 + n))
            if lemma != "天気":
                cx.execute(
                    "INSERT OR IGNORE INTO known_words(dict_form, reading, status) VALUES(?,'', 'KNOWN')",
                    (lemma,),
                )


def line_id_of(idx: int) -> int:
    with connect() as cx:
        return int(cx.execute("SELECT id FROM subtitle_lines WHERE idx=?", (idx,)).fetchone()["id"])


def insert_card(lemma: str, **over) -> int:
    """Direct insert of a stack card sharing line 2's snapshot — used where the
    test needs many cards and the moment itself is irrelevant."""
    snap_line = over.pop("line_idx", 2)
    with cursor() as cx:
        row = cx.execute(
            "SELECT id, start_ms, end_ms, text, text_furigana, translation FROM subtitle_lines WHERE idx=?",
            (snap_line,),
        ).fetchone()
        cols = {
            "lemma": lemma, "source": "auto", "state": "new",
            "queue_pos": service.stack_bottom(cx), "line_id": row["id"], "episode_id": 1,
            "anilist_id": 1, "show_title": "TestShow", "ep_number": 1,
            "start_ms": row["start_ms"], "end_ms": row["end_ms"], "text": row["text"],
            "norm_text": norm_text(row["text"]), "text_furigana": row["text_furigana"],
            "translation": row["translation"], "translation_source": "human",
            "target_surface": "天気", "clip_status": "pending", "clip_version": 1,
        }
        cols.update(over)
        cur = cx.execute(
            f"INSERT INTO srs_cards({', '.join(cols)}) VALUES({', '.join('?' * len(cols))})",
            list(cols.values()),
        )
        cid = int(cur.lastrowid)
        service.renumber_stack(cx)
    return cid


def card_row(card_id: int) -> dict:
    with connect() as cx:
        return dict(cx.execute("SELECT * FROM srs_cards WHERE id=?", (card_id,)).fetchone())


def set_card(card_id: int, **cols) -> None:
    with cursor() as cx:
        cx.execute(
            f"UPDATE srs_cards SET {', '.join(f'{k}=?' for k in cols)} WHERE id=?",
            (*cols.values(), card_id),
        )


def wipe_cards() -> None:
    with cursor() as cx:
        cx.execute("DELETE FROM srs_reviews")
        cx.execute("DELETE FROM srs_cards")
        cx.execute("DELETE FROM srs_moments")
        cx.execute("DELETE FROM srs_words")


class Clock:
    """Patched into `service._now` so reviews can be driven across days."""

    def __init__(self, t: datetime) -> None:
        self.t = t

    def __call__(self) -> datetime:
        return self.t

    def set(self, t: datetime) -> "Clock":
        self.t = t
        return self

    def plus(self, **kw) -> "Clock":
        self.t = self.t + timedelta(**kw)
        return self


def _raises_not_found(fn) -> bool:
    try:
        fn()
    except service.SrsNotFound:
        return True
    except Exception:
        return False
    return False


def rate(card_id: int, rating: int, client_id: str, elapsed_ms: int | None = 1000):
    return service.review(SrsReviewRequest(
        card_id=card_id, rating=rating, elapsed_ms=elapsed_ms, client_id=client_id))


# ---------------------------------------------------------------------------
# 1. Scheduler — §3.12 golden vectors
# ---------------------------------------------------------------------------

def test_golden() -> None:
    section("FSRS-6 golden vectors (§3.12)")
    now = datetime(2026, 9, 10, 13, 0, tzinfo=UTC)

    def blank(**kw):
        c = dict(state="new", step=None, stability=None, difficulty=None,
                 last_review_at=None, scheduled_days=0, reps=0, lapses=0)
        c.update(kw)
        return c

    g = scheduler.schedule(blank(), 3, now)
    check("new+Good → S 2.3065 / D 2.1181 / learning step 1 / +10m",
          close(g["stability"], 2.3065) and close(g["difficulty"], 2.1181)
          and g["state"] == "learning" and g["step"] == 1
          and g["due_at"] - now == timedelta(minutes=10),
          f"S={g['stability']:.4f} D={g['difficulty']:.4f}")

    g2 = scheduler.schedule(
        blank(state="learning", step=1, stability=g["stability"], difficulty=g["difficulty"],
              last_review_at=now), 3, now + timedelta(minutes=10))
    check("…Good@+10m → S 2.3065 / D 2.1112 / review I=2",
          close(g2["stability"], 2.3065) and close(g2["difficulty"], 2.1112)
          and g2["state"] == "review" and g2["scheduled_days"] == 2,
          f"S={g2['stability']:.4f} D={g2['difficulty']:.4f} I={g2['scheduled_days']}")

    a = scheduler.schedule(blank(), 1, now)
    check("new+Again → S 0.2120 / D 6.4133 / step 0 / +1m",
          close(a["stability"], 0.2120) and close(a["difficulty"], 6.4133)
          and a["step"] == 0 and a["due_at"] - now == timedelta(minutes=1))
    a2 = scheduler.schedule(
        blank(state="learning", step=0, stability=a["stability"], difficulty=a["difficulty"],
              last_review_at=now), 3, now + timedelta(minutes=1))
    check("…Good@+1m → S 0.2467 / D 6.4021 / step 1",
          close(a2["stability"], 0.2467) and close(a2["difficulty"], 6.4021) and a2["step"] == 1,
          f"S={a2['stability']:.4f}")
    a3 = scheduler.schedule(
        blank(state="learning", step=1, stability=a2["stability"], difficulty=a2["difficulty"],
              last_review_at=now + timedelta(minutes=1)), 3, now + timedelta(minutes=11))
    check("…Good@+11m → S 0.2842 / D 6.3909 / review I=1",
          close(a3["stability"], 0.2842) and close(a3["difficulty"], 6.3909)
          and a3["scheduled_days"] == 1, f"S={a3['stability']:.4f}")

    e = scheduler.schedule(blank(), 4, now)
    check("new+Easy → S 8.2956 / D 1.0000 / review I=8",
          close(e["stability"], 8.2956) and close(e["difficulty"], 1.0)
          and e["scheduled_days"] == 8)

    rev = blank(state="review", stability=20.0, difficulty=5.0, scheduled_days=20,
                last_review_at=now - timedelta(days=20))
    check("R(t=20, S=20) == 0.9000", close(scheduler.retrievability(20, 20.0), 0.9))
    want = {2: (43.6043, 6.6660, 44), 3: (59.2490, 4.9902, 59), 4: (93.5094, 3.3145, 94)}
    for g_, (s_, d_, i_) in want.items():
        r = scheduler.schedule(rev, g_, now)
        check(f"review D=5 S=20 t=20 rating {g_} → S {s_} D {d_} I {i_}",
              close(r["stability"], s_) and close(r["difficulty"], d_)
              and r["scheduled_days"] == i_,
              f"S={r['stability']:.4f} D={r['difficulty']:.4f} I={r['scheduled_days']}")
    ag = scheduler.schedule(rev, 1, now)
    check("…Again → S_forget 1.9436 / D 8.3418 / relearning +10m",
          close(ag["stability"], 1.9436) and close(ag["difficulty"], 8.3418)
          and ag["state"] == "relearning" and ag["due_at"] - now == timedelta(minutes=10))
    rl = scheduler.schedule(
        blank(state="relearning", step=0, stability=ag["stability"], difficulty=ag["difficulty"],
              last_review_at=now), 3, now + timedelta(minutes=10))
    check("…Good@+10m → S 1.9548 / D 8.3286 / review I=2",
          close(rl["stability"], 1.9548) and close(rl["difficulty"], 8.3286)
          and rl["scheduled_days"] == 2)

    rev40 = blank(state="review", stability=20.0, difficulty=5.0, scheduled_days=40,
                  last_review_at=now - timedelta(days=40))
    r40 = scheduler.schedule(rev40, 3, now)
    check("review t=40 Good → S 81.8281 / I 82 (R 0.8459)",
          close(r40["stability"], 81.8281) and r40["scheduled_days"] == 82
          and close(scheduler.retrievability(40, 20.0), 0.8459))

    d0 = [round(scheduler.initial_difficulty(g_), 4) for g_ in (1, 2, 3, 4)]
    # NOTE: §3.12's table prints 4.1236 for Hard, which contradicts its own §3.1
    # formula (w4 - e^(w5·(G-1)) + 1 = 5.1122) and the other three values; the
    # formula (the py-fsrs port) wins. Reported to the orchestrator.
    check("D0 by grade == the §3.1 formula (6.4133 / 5.1122 / 2.1181 / 1.0000)",
          d0 == [6.4133, 5.1122, 2.1181, 1.0], str(d0))

    check("I(S) == round(S) at r=0.9",
          all(scheduler.interval_days(s) == round(s) for s in (1.2, 2.6, 9.4, 45.5, 200.0)))
    check("S_forget <= S", scheduler.stability_after_forget(20.0, 5.0, 0.9) <= 20.0)
    check("S_recall increasing in G",
          scheduler.stability_after_recall(20, 5, 0.9, 2) < scheduler.stability_after_recall(20, 5, 0.9, 3)
          < scheduler.stability_after_recall(20, 5, 0.9, 4))
    check("same-day S_short never lowers S for G>=2",
          all(scheduler.stability_short_term(2.3065, g_) >= 2.3065 for g_ in (2, 3, 4)))

    # fuzz determinism + preview == schedule with fuzz off
    import random as _random

    f1 = scheduler.schedule(rev, 3, now, rng=_random.Random(7))["scheduled_days"]
    f2 = scheduler.schedule(rev, 3, now, rng=_random.Random(7))["scheduled_days"]
    check("fuzz is deterministic for a seeded RNG", f1 == f2, f"{f1} == {f2}")
    check("fuzz stays near the unfuzzed interval", abs(f1 - 59) <= 6, f"I_fuzzed={f1}")
    pv = scheduler.preview(blank(), now)
    check("preview(new) == {1m, 5m, 10m, 8d}",
          pv == {"again": "1m", "hard": "5m", "good": "10m", "easy": "8d"}, str(pv))


def test_state_machine() -> None:
    section("state machine (§3.2) and step timings")
    now = datetime(2026, 9, 10, 13, 0, tzinfo=UTC)

    def c(state, step, **kw):
        base = dict(state=state, step=step, stability=5.0, difficulty=5.0,
                    last_review_at=now - timedelta(days=3), scheduled_days=5, reps=3, lapses=0)
        base.update(kw)
        return base

    table = {
        ("learning", 0): {1: ("learning", 0, 1), 2: ("learning", 0, 5.5), 3: ("learning", 1, 10), 4: ("review", None, None)},
        ("learning", 1): {1: ("learning", 0, 1), 2: ("learning", 1, 10), 3: ("review", None, None), 4: ("review", None, None)},
        ("relearning", 0): {1: ("relearning", 0, 10), 2: ("relearning", 0, 15), 3: ("review", None, None), 4: ("review", None, None)},
        ("review", None): {1: ("relearning", 0, 10), 2: ("review", None, None), 3: ("review", None, None), 4: ("review", None, None)},
    }
    ok = True
    detail = ""
    for (state, step), row in table.items():
        for rating, (want_state, want_step, minutes) in row.items():
            r = scheduler.schedule(c(state, step), rating, now)
            good = r["state"] == want_state and r["step"] == want_step
            if minutes is not None:
                good = good and abs((r["due_at"] - now).total_seconds() - minutes * 60) < 1
            else:
                good = good and r["scheduled_days"] >= 1 and r["due_at"] > now
            if not good:
                ok = False
                detail = f"{state}/{step} rating {rating} → {r['state']}/{r['step']}"
    check("all 4x4 transitions match §3.2", ok, detail)
    check("Again in review counts a lapse",
          scheduler.schedule(c("review", None), 1, now)["lapses"] == 1)
    check("step is NULL when entering review",
          scheduler.schedule(c("learning", 1), 3, now)["step"] is None)
    check("reps += 1 on every rating",
          all(scheduler.schedule(c("review", None), g, now)["reps"] == 4 for g in (1, 2, 3, 4)))
    grad = scheduler.schedule(c("learning", 1), 3, now)
    check("graduated due_at == day_start(today + I)",
          grad["due_at"] == scheduler.day_start(scheduler.srs_day(now) + timedelta(days=grad["scheduled_days"])),
          str(grad["due_at"]))


def test_days() -> None:
    section("SRS day boundary and DST (§3.4)")
    # 03:59 / 04:01 local on a normal EDT day (UTC = local + 4 h)
    check("03:59 local belongs to the previous SRS day",
          scheduler.srs_day(datetime(2026, 9, 10, 7, 59, tzinfo=UTC)) == date(2026, 9, 9))
    check("04:01 local starts the new SRS day",
          scheduler.srs_day(datetime(2026, 9, 10, 8, 1, tzinfo=UTC)) == date(2026, 9, 10))
    check("day_start(2026-11-05) == 09:00Z (EST)",
          scheduler.day_start(date(2026, 11, 5)) == datetime(2026, 11, 5, 9, 0, tzinfo=UTC),
          str(scheduler.day_start(date(2026, 11, 5))))
    check("srs_day(2026-11-05 08:30Z) == 2026-11-04",
          scheduler.srs_day(datetime(2026, 11, 5, 8, 30, tzinfo=UTC)) == date(2026, 11, 4))
    check("srs_day(2026-11-05 09:30Z) == 2026-11-05",
          scheduler.srs_day(datetime(2026, 11, 5, 9, 30, tzinfo=UTC)) == date(2026, 11, 5))
    check("day_start before the 2026-11-01 DST change is 08:00Z (EDT)",
          scheduler.day_start(date(2026, 10, 30)) == datetime(2026, 10, 30, 8, 0, tzinfo=UTC))
    check("day_start after the 2027-03-14 DST change is 08:00Z again",
          scheduler.day_start(date(2027, 3, 20)) == datetime(2027, 3, 20, 8, 0, tzinfo=UTC))
    check("the fall-back SRS day is 25 h long (never a captured tzinfo)",
          (scheduler.day_start(date(2026, 11, 1)) - scheduler.day_start(date(2026, 10, 31)))
          == timedelta(hours=25)
          and (scheduler.day_start(date(2026, 11, 2)) - scheduler.day_start(date(2026, 11, 1)))
          == timedelta(hours=24))

    # elapsed t is SRS-day based, not a timestamp delta
    t23 = datetime(2026, 9, 11, 3, 0, tzinfo=UTC)          # 23:00 local Sep 10
    t09 = datetime(2026, 9, 11, 13, 0, tzinfo=UTC)         # 09:00 local Sep 11
    check("23:00 → 09:00 next day gives t = 1 (long-term branch)",
          scheduler.elapsed_days(t23, t09) == 1, f"timestamp delta days={(t09 - t23).days}")
    t22 = datetime(2026, 9, 11, 2, 0, tzinfo=UTC)          # 22:00 local Sep 10
    t08 = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)         # 08:00 local Sep 11
    check("22:00 → 08:00 gives t = 1", scheduler.elapsed_days(t22, t08) == 1)
    grown = scheduler.schedule(
        dict(state="review", step=None, stability=1.0, difficulty=5.0, last_review_at=t23,
             scheduled_days=1, reps=2, lapses=0), 3, t09)
    check("long-term branch actually grows S across that boundary", grown["stability"] > 1.0,
          f"S={grown['stability']:.4f}")


# ---------------------------------------------------------------------------
# 2. Failed repeats, demotion (§3.7)
# ---------------------------------------------------------------------------

def test_failed_repeats(clock: Clock) -> None:
    section("failed repeats and auto-demotion (§3.7)")
    wipe_cards()
    cid = insert_card("天気")
    clock.set(datetime(2026, 9, 10, 13, 0, tzinfo=UTC))     # 09:00 local
    rate(cid, 3, "f1")
    clock.plus(minutes=10)
    rate(cid, 3, "f2")                                     # → review I=2
    clock.plus(minutes=1)
    r = rate(cid, 1, "f3")                                 # same day Again
    with connect() as cx:
        cf = cx.execute("SELECT counted_fail FROM srs_reviews WHERE client_id='f3'").fetchone()["counted_fail"]
    check("same-day Agains on introduction day count 0",
          cf == 0 and card_row(cid)["fail_count"] == 0, f"counted_fail={cf}")

    # three same-day relearning Agains count once (next day)
    clock.set(datetime(2026, 9, 12, 13, 0, tzinfo=UTC))
    rate(cid, 1, "f4")
    clock.plus(minutes=11)
    rate(cid, 1, "f5")
    clock.plus(minutes=11)
    rate(cid, 1, "f6")
    with connect() as cx:
        counted = [r["counted_fail"] for r in cx.execute(
            "SELECT counted_fail FROM srs_reviews WHERE client_id IN ('f4','f5','f6') ORDER BY id")]
    check("three Agains in one relearning session count once",
          counted == [1, 0, 0] and card_row(cid)["fail_count"] == 1, str(counted))

    # 03:55 Good → 04:05 Again is not a failed repeat (8 h gap rule)
    wipe_cards()
    cid = insert_card("天気")
    clock.set(datetime(2026, 9, 15, 7, 55, tzinfo=UTC))     # 03:55 local
    rate(cid, 3, "g1")
    clock.set(datetime(2026, 9, 15, 8, 5, tzinfo=UTC))      # 04:05 local, new SRS day
    rate(cid, 1, "g2")
    with connect() as cx:
        cf = cx.execute("SELECT counted_fail FROM srs_reviews WHERE client_id='g2'").fetchone()["counted_fail"]
    check("03:55 Good → 04:05 Again is not a failed repeat (8 h gap)", cf == 0)

    # any passing rating with I >= 21 resets fail_count; I < 21 does not
    set_card(cid, fail_count=1, state="review", step=None, stability=2.0, difficulty=7.0,
             scheduled_days=2, last_review_at=service._sql(clock.t - timedelta(days=2)))
    clock.plus(days=1)
    rate(cid, 3, "g3")
    check("passing rating with I < 21 does not reset fail_count",
          card_row(cid)["fail_count"] == 1, f"I={card_row(cid)['scheduled_days']}")
    set_card(cid, fail_count=1, state="review", step=None, stability=30.0, difficulty=5.0,
             scheduled_days=30, last_review_at=service._sql(clock.t - timedelta(days=30)))
    clock.plus(days=1)
    rate(cid, 2, "g4")
    row = card_row(cid)
    check("Hard earning I >= 21 resets fail_count",
          row["fail_count"] == 0 and row["scheduled_days"] >= KNOWN_INTERVAL_DAYS,
          f"I={row['scheduled_days']}")

    # the worked example: demotion exactly at the second counted fail
    wipe_cards()
    cid = insert_card("天気")
    clock.set(datetime(2026, 9, 20, 13, 0, tzinfo=UTC))
    rate(cid, 3, "d1")
    clock.plus(minutes=10)
    rate(cid, 3, "d2")
    clock.set(datetime(2026, 9, 22, 13, 0, tzinfo=UTC))
    rate(cid, 1, "d3")                                     # failed repeat #1
    check("first counted fail sets fail_count=1", card_row(cid)["fail_count"] == 1)
    clock.plus(minutes=11)
    rate(cid, 3, "d4")
    clock.set(datetime(2026, 9, 25, 13, 0, tzinfo=UTC))
    other = insert_card("勉強")                             # someone else in the stack
    res = rate(cid, 1, "d5")                               # failed repeat #2 → demote
    row = card_row(cid)
    with connect() as cx:
        maxpos = cx.execute("SELECT MAX(queue_pos) m FROM srs_cards WHERE state='new'").fetchone()["m"]
    check("second counted fail demotes the card",
          res.demoted and row["state"] == "new" and row["queue_pos"] == maxpos,
          f"state={row['state']} pos={row['queue_pos']} of {maxpos}")
    check("demotion wipes the scheduler incl. last_review_at/introduced_at",
          row["stability"] is None and row["difficulty"] is None and row["step"] is None
          and row["due_at"] is None and row["last_review_at"] is None
          and row["introduced_at"] is None and row["scheduled_days"] == 0
          and row["fail_count"] == 0 and row["last_fail_day"] is None)
    check("demotion keeps lifetime counters and bumps demoted_count",
          row["reps"] >= 5 and row["demoted_count"] == 1 and row["demoted_at"] is not None,
          f"reps={row['reps']} lapses={row['lapses']}")
    check("demotion message names the word and the miss count",
          res.message and "天気" in res.message and "2" in res.message, str(res.message))
    check("the other stack card kept its place", card_row(other)["queue_pos"] in (1, 2))

    # a demoted card's first re-rating Again is free; the next day counts
    clock.set(datetime(2026, 10, 5, 13, 0, tzinfo=UTC))
    rate(cid, 1, "d6")
    check("demoted card's first re-rating Again is not a failed repeat",
          card_row(cid)["fail_count"] == 0)
    clock.set(datetime(2026, 10, 6, 13, 0, tzinfo=UTC))
    rate(cid, 1, "d7")
    check("the next morning's Again counts", card_row(cid)["fail_count"] == 1)

    # third demotion parks the card
    set_card(cid, demoted_count=MAX_DEMOTIONS - 1, fail_count=1,
             last_fail_day="2026-10-06", state="review", step=None, stability=5.0,
             difficulty=5.0, scheduled_days=5,
             last_review_at=service._sql(clock.t - timedelta(days=2)))
    clock.plus(days=2)
    res = rate(cid, 1, "d8")
    row = card_row(cid)
    check("the third demotion parks the card (suspended/auto_demotions)",
          row["state"] == "suspended" and row["suspend_reason"] == "auto_demotions"
          and row["queue_pos"] is None and res.suspended,
          f"state={row['state']} reason={row['suspend_reason']}")

    # demote_after_fails = 0 never demotes
    service.settings_put(SrsSettingsPatch(demote_after_fails=0))
    wipe_cards()
    cid = insert_card("天気")
    clock.set(datetime(2026, 11, 10, 13, 0, tzinfo=UTC))
    rate(cid, 3, "z1")
    clock.plus(minutes=10)
    rate(cid, 3, "z2")
    for n in range(3):
        clock.plus(days=2)
        rate(cid, 1, f"z{n + 3}")
        clock.plus(minutes=11)
        rate(cid, 3, f"zg{n}")
    check("demote_after_fails=0 never demotes",
          card_row(cid)["state"] in ("review", "relearning") and card_row(cid)["demoted_count"] == 0,
          f"fail_count={card_row(cid)['fail_count']}")
    service.settings_put(SrsSettingsPatch(demote_after_fails=2))


def test_demote_swaps_moment(clock: Clock) -> None:
    section("demotion swaps to a fresh moment (§3.7.3)")
    wipe_cards()
    cid = insert_card("天気", line_idx=2)
    # the current primary moment, plus an accepted alternate on another line
    with cursor() as cx:
        alt_line = line_id_of(5)
        for lid, surface, accepted in ((line_id_of(2), "天気", 0), (alt_line, "天気", 1)):
            row = cx.execute("SELECT * FROM subtitle_lines WHERE id=?", (lid,)).fetchone()
            cx.execute(
                "INSERT INTO srs_moments(lemma, line_id, episode_id, idx, start_ms, end_ms, norm_text, "
                "text, target_surface, accepted, verdict, clarity) VALUES(?,?,1,?,?,?,?,?,?,?,'accept',0.9)",
                ("天気", lid, row["idx"], row["start_ms"], row["end_ms"],
                 norm_text(row["text"]), row["text"], surface, accepted),
            )
    before = card_row(cid)
    clock.set(datetime(2026, 12, 1, 13, 0, tzinfo=UTC))
    rate(cid, 3, "s1")
    clock.plus(minutes=10)
    rate(cid, 3, "s2")
    clock.set(datetime(2026, 12, 3, 13, 0, tzinfo=UTC))
    rate(cid, 1, "s3")
    clock.plus(minutes=11)
    rate(cid, 3, "s4")
    clock.set(datetime(2026, 12, 6, 13, 0, tzinfo=UTC))
    clip_dir = Path(TMP) / "clips" / "srs" / str(cid)
    clip_dir.mkdir(parents=True, exist_ok=True)
    (clip_dir / "clip.mp4").write_bytes(b"old clip")
    res = rate(cid, 1, "s5")
    after = card_row(cid)
    check("demotion swapped the primary moment",
          res.demoted and after["line_id"] == alt_line and after["text"] != before["text"],
          f"line {before['line_id']} → {after['line_id']}")
    check("swap bumps clip_version and re-requests the clip",
          after["clip_version"] == before["clip_version"] + 1 and after["clip_status"] == "pending")
    with connect() as cx:
        job = cx.execute(
            "SELECT payload_json FROM jobs WHERE type='srs_clip' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    check("srs_clip {card_id, v} queued for the new version",
          job and json.loads(job["payload_json"]) == {"card_id": cid, "v": after["clip_version"]},
          job["payload_json"] if job else "no job")
    alts = json.loads(after["alt_moment_ids_json"] or "[]")
    check("the previous moment went to the front of alt_moment_ids", len(alts) >= 1, str(alts))
    check("the old clip directory is kept as <dir>.prev until the new clip lands",
          not (clip_dir / "clip.mp4").exists()
          and (clip_dir.with_name(f"{cid}.prev") / "clip.mp4").read_bytes() == b"old clip")

    undone = service.undo_review(res.review_id)
    restored = card_row(cid)
    check("undoing a demotion restores the state, the stack position and the snapshot",
          restored["state"] == "review" and restored["line_id"] == before["line_id"]
          and restored["text"] == before["text"] and restored["queue_pos"] is None
          and undone.card.id == cid,
          f"state={restored['state']} line={restored['line_id']}")
    check("undoing a demotion restores the previous clip directory",
          (clip_dir / "clip.mp4").exists()
          and (clip_dir / "clip.mp4").read_bytes() == b"old clip"
          and restored["clip_version"] == before["clip_version"],
          f"v={restored['clip_version']}")


# ---------------------------------------------------------------------------
# 3. Review idempotency, conflicts, undo (§3.10, §3.11)
# ---------------------------------------------------------------------------

def test_review_api(clock: Clock) -> None:
    section("review idempotency, 409 and undo (§3.10/§3.11)")
    wipe_cards()
    clock.set(datetime(2027, 1, 10, 13, 0, tzinfo=UTC))
    cid = insert_card("天気")
    first = rate(cid, 3, "same-client")
    again = rate(cid, 3, "same-client")
    with connect() as cx:
        n = cx.execute("SELECT COUNT(*) n FROM srs_reviews WHERE client_id='same-client'").fetchone()["n"]
    check("repeated client_id writes once and reports duplicate",
          n == 1 and again.duplicate and again.review_id == first.review_id, f"rows={n}")

    set_card(cid, state="known")
    try:
        rate(cid, 3, "conflict-1")
        check("rating a retired card raises 409", False, "no exception")
    except service.SrsConflict as e:
        body = e.payload or {}
        check("rating a retired card → 409 with the card in the body",
              body.get("card", {}).get("id") == cid and "known" in body.get("detail", ""),
              str(body.get("detail")))

    # undo: unlimited within the day, targeted, 409 on a stale target, 404 tomorrow
    wipe_cards()
    a = insert_card("天気")
    b = insert_card("勉強")
    pos_before = card_row(a)["queue_pos"]
    ra = rate(a, 3, "u1")
    rb = rate(b, 3, "u2")
    out = service.undo_review(ra.review_id)               # targeted, older card
    check("targeted undo works after a later card was rated",
          out.undone_review_id == ra.review_id and card_row(a)["state"] == "new"
          and card_row(a)["queue_pos"] == pos_before,
          f"state={card_row(a)['state']} pos={card_row(a)['queue_pos']}")
    with connect() as cx:
        undone = cx.execute("SELECT undone FROM srs_reviews WHERE id=?", (ra.review_id,)).fetchone()["undone"]
    check("the undone review row is kept with undone=1", undone == 1)
    ra3 = rate(a, 3, "u3")
    ra2 = rate(a, 1, "u4")
    try:
        service.undo_review(ra3.review_id)               # same card has a newer review
        check("undo of a superseded review → 409", False, "no exception")
    except service.SrsConflict:
        check("undo of a superseded review → 409", True)
    except service.SrsNotFound:
        check("undo of a superseded review → 409", False, "got 404")
    check("undoing an already-undone review → 404",
          _raises_not_found(lambda: service.undo_review(ra.review_id)))
    service.undo_review(ra2.review_id)
    service.undo_review()                                  # newest overall (b or a)
    clock.plus(days=1)
    try:
        service.undo_review()
        check("undo the next day → 404", False, "no exception")
    except service.SrsNotFound:
        check("undo the next day → 404", True)

    # known-threshold crossing flags
    wipe_cards()
    c = insert_card("天気")
    set_card(c, state="review", step=None, stability=25.0, difficulty=5.0, scheduled_days=20,
             last_review_at=service._sql(clock.t - timedelta(days=20)), reps=4)
    res = rate(c, 3, "k1")
    check("crossing 21 d reports became_known / known_crossed=up",
          res.became_known and res.known_crossed == "up" and res.card.is_known,
          f"I={res.card.scheduled_days}")
    res = rate(c, 1, "k2")
    check("a lapse reports known_crossed=down",
          res.known_crossed == "down" and not res.card.is_known)


# ---------------------------------------------------------------------------
# 4. Queue composition (§3.6)
# ---------------------------------------------------------------------------

def test_queue(clock: Clock) -> None:
    section("queue composition (§3.6)")
    wipe_cards()
    clock.set(datetime(2027, 2, 10, 13, 0, tzinfo=UTC))
    service.settings_put(SrsSettingsPatch(new_per_day=2))
    news = [insert_card(f"新{i}") for i in range(5)]
    q = service.queue(limit=20)
    check("new_per_day limits the served new cards", len(q.cards) == 2, f"{len(q.cards)} cards")
    check("queue is idempotent without answering",
          [c.id for c in service.queue(limit=20).cards] == [c.id for c in q.cards])
    check("extra_new bypasses the limit once",
          len(service.queue(limit=20, extra_new=3).cards) == 5)
    check("every served card carries a preview",
          all(c.preview and c.preview.good for c in q.cards), str(q.cards[0].preview))
    check("queue carries server_time and the SRS day",
          q.server_time.endswith("Z") and q.day == "2027-02-10", f"{q.server_time} {q.day}")

    # ready clips are served before pending ones, order otherwise preserved
    set_card(news[3], clip_status="ready")
    q = service.queue(limit=20, extra_new=5)
    check("ready clips are served before pending ones",
          q.cards[0].id == news[3], f"head={q.cards[0].lemma}")
    check("pending cards keep their relative order",
          [c.id for c in q.cards[1:]] == news[:3] + news[4:])
    set_card(news[3], clip_status="pending")

    # study_now bypasses the limit and comes first
    service.card_action(news[4], "study_next")
    q = service.queue(limit=20)
    check("study_now card is served first and bypasses new_per_day",
          q.cards[0].id == news[4] and q.cards[0].study_now and len(q.cards) == 3,
          f"{[c.lemma for c in q.cards]}")

    # R R R N interleave with learning first
    for i in range(4):
        cid = insert_card(f"復習{i}")
        set_card(cid, state="review", step=None, stability=10.0, difficulty=5.0, scheduled_days=10,
                 queue_pos=None, study_now=0,
                 due_at=service._sql(clock.t - timedelta(days=1)),
                 last_review_at=service._sql(clock.t - timedelta(days=11)))
    learn_id = insert_card("学習中")
    set_card(learn_id, state="learning", step=1, stability=2.0, difficulty=5.0, queue_pos=None,
             study_now=0, due_at=service._sql(clock.t - timedelta(minutes=5)),
             last_review_at=service._sql(clock.t - timedelta(minutes=15)))
    q = service.queue(limit=20)
    kinds = ["L" if c.state in ("learning", "relearning") else ("R" if c.state == "review" else "N")
             for c in q.cards]
    check("learning first, then R R R N", "".join(kinds).startswith("LRRRN"), "".join(kinds))

    # learn-ahead
    soon_id = insert_card("もうすぐ")
    set_card(soon_id, state="learning", step=1, stability=2.0, difficulty=5.0, queue_pos=None,
             study_now=0, due_at=service._sql(clock.t + timedelta(minutes=5)),
             last_review_at=service._sql(clock.t - timedelta(minutes=5)))
    q = service.queue(limit=20)
    check("learning cards due within 20 min come back in learning_soon",
          [c.id for c in q.learning_soon] == [soon_id], f"{len(q.learning_soon)} soon")

    # buried cards disappear from every queue
    service.card_action(news[0], "bury")
    q = service.queue(limit=20, extra_new=10)
    check("a buried card is hidden from the queue", news[0] not in [c.id for c in q.cards])
    service.card_action(news[0], "unbury")
    check("unbury brings it back",
          news[0] in [c.id for c in service.queue(limit=20, extra_new=10).cards])

    # counts
    q = service.queue(limit=20)
    check("queue counts are filled",
          q.counts.review_due == 4 and q.counts.learning_due == 1
          and q.counts.new_stack_total == 5 and q.counts.clips_pending == 5,
          f"{q.counts.model_dump()}")
    service.settings_put(SrsSettingsPatch(new_per_day=10))


# ---------------------------------------------------------------------------
# 5. Stack operations and invariants (§4.2)
# ---------------------------------------------------------------------------

def assert_stack_invariants(label: str) -> None:
    with connect() as cx:
        rows = [dict(r) for r in cx.execute("SELECT * FROM srs_cards")]
    new_pos = sorted(r["queue_pos"] for r in rows if r["state"] == "new")
    ok = new_pos == list(range(1, len(new_pos) + 1))
    ok = ok and all(r["queue_pos"] is None for r in rows if r["state"] != "new")
    ok = ok and all(not r["study_now"] for r in rows if r["state"] != "new")
    ok = ok and all(r["step"] is None for r in rows
                    if r["state"] in ("new", "review", "known", "rejected"))
    ok = ok and all(r["last_review_at"] is None and r["introduced_at"] is None
                    for r in rows if r["state"] == "new")
    lemmas = [r["lemma"] for r in rows]
    ok = ok and len(lemmas) == len(set(lemmas))
    check(f"stack invariants hold after {label}", ok,
          f"positions={new_pos}")


def test_stack(clock: Clock) -> None:
    section("stack operations (§4.2)")
    wipe_cards()
    clock.set(datetime(2027, 3, 1, 13, 0, tzinfo=UTC))
    ids = [insert_card(f"語{i}") for i in range(5)]
    check("fresh stack is dense 1..N",
          [card_row(i)["queue_pos"] for i in ids] == [1, 2, 3, 4, 5])

    service.stack_move(SrsMove(card_id=ids[4], position=0))
    order = [r["id"] for r in _stack_order()]
    check("move to position 0 puts the card on top and keeps the rest in order",
          order == [ids[4]] + ids[:4], str(order))
    service.stack_move(SrsMove(card_id=ids[4], position=99))
    order = [r["id"] for r in _stack_order()]
    check("move clamps to the end", order == ids[:4] + [ids[4]], str(order))
    assert_stack_invariants("move")

    service.card_action(ids[2], "study_next")
    row = card_row(ids[2])
    check("study_next puts the card on top and sets study_now",
          row["queue_pos"] == 1 and row["study_now"] == 1)
    service.card_action(ids[2], "bottom")
    row = card_row(ids[2])
    check("bottom sends it to the end and clears study_now",
          row["queue_pos"] == 5 and row["study_now"] == 0)
    assert_stack_invariants("study_next/bottom")

    # suspend → resume (new) → bottom of the stack
    service.card_action(ids[0], "suspend")
    check("suspend leaves the stack and records where to return",
          card_row(ids[0])["state"] == "suspended"
          and card_row(ids[0])["state_before_suspend"] == "new"
          and card_row(ids[0])["queue_pos"] is None)
    service.card_action(ids[0], "resume")
    check("resume of a never-rated card returns it to the bottom",
          card_row(ids[0])["state"] == "new" and card_row(ids[0])["queue_pos"] == 5)
    assert_stack_invariants("suspend/resume")

    # reject → restore
    service.card_action(ids[1], "reject")
    with connect() as cx:
        w = cx.execute("SELECT * FROM srs_words WHERE lemma=?", (card_row(ids[1])["lemma"],)).fetchone()
    check("reject blocklists the word",
          card_row(ids[1])["state"] == "rejected" and w and w["judge_status"] == "rejected"
          and w["user_flag"] == "skip" and w["judge_reason"] == "user_skip")
    service.card_action(ids[1], "restore")
    with connect() as cx:
        w = cx.execute("SELECT * FROM srs_words WHERE lemma=?", (card_row(ids[1])["lemma"],)).fetchone()
    check("restore returns it to the bottom and clears the blocklist",
          card_row(ids[1])["state"] == "new" and w["user_flag"] is None
          and w["judge_status"] == "accepted")
    assert_stack_invariants("reject/restore")

    # known / unknown / forget
    service.card_action(ids[3], "known")
    row = card_row(ids[3])
    check("mark known retires the card",
          row["state"] == "known" and row["known_source"] == "user" and row["queue_pos"] is None
          and row["due_at"] is None)
    service.card_action(ids[3], "unknown")
    check("unknown returns it to the TOP of the stack",
          card_row(ids[3])["state"] == "new" and card_row(ids[3])["queue_pos"] == 1)
    rate(ids[3], 3, "st1")
    service.card_action(ids[3], "forget")
    row = card_row(ids[3])
    check("forget resets the scheduler and prepends",
          row["state"] == "new" and row["queue_pos"] == 1 and row["stability"] is None
          and row["last_review_at"] is None and row["introduced_at"] is None)
    assert_stack_invariants("known/unknown/forget")

    # bulk with previous
    res = service.bulk_action(SrsBulkRequest(card_ids=ids[:3], action="suspend"))
    check("bulk returns updated + previous for the exact inverse",
          res.updated == 3 and len(res.previous) == 3
          and all(p.state == "new" and p.queue_pos for p in res.previous),
          str([p.model_dump() for p in res.previous]))
    service.bulk_action(SrsBulkRequest(card_ids=ids[:3], action="resume"))
    check("bulk resume brings them back", all(card_row(i)["state"] == "new" for i in ids[:3]))
    assert_stack_invariants("bulk")

    # `previous` must be a *pre-mutation* snapshot, and the client's Undo
    # (ascending queue_pos, `move(card, queue_pos - 1)` — the endpoint is
    # 0-based) must reproduce the original order exactly.
    before = [r["id"] for r in _stack_order()]
    res = service.bulk_action(
        SrsBulkRequest(card_ids=[before[2], before[0]], action="bottom")
    )
    snap = {p.card_id: p.queue_pos for p in res.previous}
    check("bulk previous records positions from before the first mutation",
          snap == {before[2]: 3, before[0]: 1}, str(snap))
    for p in sorted(res.previous, key=lambda x: x.queue_pos or 0):
        service.stack_move(SrsMove(card_id=p.card_id, position=(p.queue_pos or 1) - 1))
    after = [r["id"] for r in _stack_order()]
    check("the client's 6 s Undo restores the exact stack order",
          after == before, f"{after} != {before}")
    assert_stack_invariants("bulk bottom + undo")

    # resort with keep_top
    for n, cid in enumerate(_stack_order()):
        set_card(cid["id"], score=float(n), freq_rank=100 - n)
    top_before = [r["id"] for r in _stack_order()][:2]
    service.stack_resort(SrsResort(by="score", keep_top=2))
    order = [r["id"] for r in _stack_order()]
    check("resort keeps the top N fixed", order[:2] == top_before, str(order))
    scores = [card_row(i)["score"] for i in order[2:]]
    check("resort by score orders the rest descending", scores == sorted(scores, reverse=True), str(scores))
    set_card(order[-1], demoted_count=2)
    service.stack_resort(SrsResort(by="score", keep_top=0))
    order = [r["id"] for r in _stack_order()]
    check("resort keeps demoted cards after all fresh ones",
          card_row(order[-1])["demoted_count"] == 2, str(order))
    assert_stack_invariants("resort")

    # confirm-known cards refuse the study verbs
    service.word_confirm_known("確認語")
    with connect() as cx:
        confirm_id = int(cx.execute(
            "SELECT id FROM srs_cards WHERE lemma='確認語'").fetchone()["id"])
    refused = []
    for action in ("unknown", "forget", "demote", "study_next", "bury", "suspend", "resume"):
        try:
            service.card_action(confirm_id, action)
        except service.SrsInvalid:
            refused.append(action)
    check("every study verb is 422 on a confirm card",
          len(refused) == 7, f"refused={refused}")
    check("a confirm card counts as known",
          "確認語" in service.known_forms())
    service.delete_card(confirm_id)


def _stack_order() -> list[dict]:
    with connect() as cx:
        return [dict(r) for r in cx.execute(
            "SELECT * FROM srs_cards WHERE state='new' ORDER BY queue_pos, id")]


def test_resume_table(clock: Clock) -> None:
    section("the resume/restore table (§3.8)")
    wipe_cards()
    clock.set(datetime(2027, 3, 5, 13, 0, tzinfo=UTC))
    now_sql = service._sql(clock.t)
    past = service._sql(clock.t - timedelta(days=3))

    cases = [
        ("known + memory → review, due >= now", dict(state="known", stability=5.0, difficulty=5.0,
                                                     due_at=past, scheduled_days=5), "review"),
        ("known, never rated → new (top)", dict(state="known"), "new"),
        ("suspended from learning → learning", dict(state="suspended", state_before_suspend="learning",
                                                    step=1, stability=2.0, difficulty=5.0), "learning"),
        ("suspended from review → review", dict(state="suspended", state_before_suspend="review",
                                                stability=9.0, difficulty=5.0, due_at=past), "review"),
        ("suspended from new → new (bottom)", dict(state="suspended", state_before_suspend="new"), "new"),
        ("suspended with no memory → new", dict(state="suspended", state_before_suspend="review"), "new"),
        ("rejected → new (bottom)", dict(state="rejected"), "new"),
    ]
    for n, (label, cols, want) in enumerate(cases):
        cid = insert_card(f"再開{n}")
        cols.setdefault("queue_pos", None)
        set_card(cid, **cols)
        service.card_action(cid, "restore" if cols["state"] == "rejected" else "resume")
        row = card_row(cid)
        ok = row["state"] == want
        if want == "review":
            ok = ok and row["due_at"] >= now_sql and row["step"] is None
        if want == "learning":
            ok = ok and row["step"] == 1 and row["due_at"] == now_sql
        check(f"resume: {label}", ok, f"→ {row['state']} due={row['due_at']}")
    assert_stack_invariants("resume table")


# ---------------------------------------------------------------------------
# 6. Snapshot, cards API, moments, relink (§5.7, §2.7)
# ---------------------------------------------------------------------------

def test_snapshot_and_cards() -> None:
    section("snapshot, manual creation, edit, delete (§5.7, §5.12, §2.7)")
    wipe_cards()
    lid = line_id_of(2)
    snap = snapshot_line(lid, "天気")
    tokens = json.loads(snap["tokens_json"])["tokens"]
    targets = [t for t in tokens if t["t"]]
    check("snapshot_line marks exactly one target token",
          len(targets) == 1 and targets[0]["s"] == "天気", str(targets))
    check("snapshot_line keeps the human translation",
          snap["translation_source"] == "human" and "weather" in (snap["translation"] or ""),
          str(snap["translation"]))
    check("snapshot_line records ±2 dialogue context lines",
          len(json.loads(snap["context_json"])) >= 2
          and any(c["is_target"] for c in json.loads(snap["context_json"])))
    check("snapshot_line normalises text and produces ruby furigana",
          snap["norm_text"] == "今日はいい天気ですね" and "<ruby>" in (snap["text_furigana"] or ""),
          snap["norm_text"])
    try:
        snapshot_line(lid, "存在しない語", "存在しない語")
        check("target_surface not in text → ValueError('invalid_surface')", False, "no raise")
    except ValueError as e:
        check("target_surface not in text → ValueError('invalid_surface')", "invalid_surface" in str(e))
    try:
        snapshot_line(999999, "天気")
        check("missing line → LookupError", False, "no raise")
    except LookupError:
        check("missing line → LookupError", True)
    check("bidi controls are stripped",
          strip_bidi("‪今日‬") == "今日"
          and norm_text("‪今日は、いい‬") == "今日はいい")

    out = service.create_card_manual(SrsCreateCard(lemma="天気", line_id=lid, study_next=True))
    cid = out["card"]["id"]
    row = card_row(cid)
    check("manual creation snapshots the card and prepends it",
          row["source"] == "manual" and row["queue_pos"] == 1 and row["study_now"] == 1
          and row["target_surface"] == "天気")
    check("manual creation records what Migaku thinks right now",
          row["migaku_status_seen"] == "ABSENT", str(row["migaku_status_seen"]))
    with connect() as cx:
        job = cx.execute(
            "SELECT payload_json FROM jobs WHERE type='srs_clip' ORDER BY id DESC LIMIT 1").fetchone()
    check("card creation queues srs_clip {card_id, v:1}",
          job and json.loads(job["payload_json"]) == {"card_id": cid, "v": 1},
          job["payload_json"] if job else "none")
    try:
        service.create_card_manual(SrsCreateCard(lemma="天気", line_id=lid))
        check("a second card for the same lemma → 409 with card_id", False, "no raise")
    except service.SrsConflict as e:
        check("a second card for the same lemma → 409 with card_id",
              (e.payload or {}).get("card_id") == cid)
    queued = service.create_card_manual(SrsCreateCard(lemma="未知語"))
    check("POST /srs/cards without a line queues srs_find_moments",
          queued == {"queued": True, "lemma": "未知語"})

    edited = service.patch_card(cid, SrsPatchCard(meaning_short="weather", translation="Nice weather."))
    check("patch stores the edit and marks a user translation",
          edited.meaning_short == "weather" and edited.translation_source == "user")
    try:
        service.patch_card(cid, SrsPatchCard(target_surface="無い"))
        check("patching target_surface off the line → 422", False, "no raise")
    except service.SrsInvalid:
        check("patching target_surface off the line → 422", True)

    detail = service.card_detail(cid)
    check("card detail returns the card, its moments and its reviews",
          detail.card.id == cid and any(m.is_primary for m in detail.moments),
          f"{len(detail.moments)} moments")
    check("card detail reports source_available from the video on disk",
          detail.card.source_available and detail.card.line_available)

    # swap the moment onto another line, then hard-delete
    other = line_id_of(1)
    try:
        service.set_moment(cid, SrsSwapMoment(line_id=other))
        check("swapping onto a line without the word → 409", False, "no raise")
    except service.SrsConflict:
        check("swapping onto a line without the word → 409", True)
    service.delete_card(cid)
    with connect() as cx:
        gone = cx.execute("SELECT COUNT(*) n FROM srs_cards WHERE id=?", (cid,)).fetchone()["n"]
        word = cx.execute("SELECT * FROM srs_words WHERE lemma='天気'").fetchone()
        moment = cx.execute("SELECT * FROM srs_moments WHERE lemma='天気'").fetchone()
    check("hard delete removes the row",
          gone == 0)
    check("hard delete resets srs_words to unjudged and drops card_id",
          word and word["judge_status"] == "unjudged" and word["card_id"] is None)
    check("hard delete marks the primary moment user_rejected",
          moment and moment["verdict"] == "user_rejected" and moment["accepted"] == 0)


def test_relink() -> None:
    section("re-ingest, relink and delete_title (§2.7)")
    wipe_cards()
    lid = line_id_of(3)
    out = service.create_card_manual(SrsCreateCard(lemma="勉強", line_id=lid))
    cid = out["card"]["id"]
    with cursor() as cx:
        plan = "\n".join(str(dict(r)) for r in cx.execute(
            "EXPLAIN QUERY PLAN DELETE FROM subtitle_lines WHERE id=?", (lid,)))
    check("deleting a subtitle line uses idx_srs_cards_line (no SCAN)",
          "idx_srs_cards_line" in plan and "SCAN srs_cards" not in plan, plan.replace("\n", " | ")[:220])
    check("deleting a subtitle line uses idx_srs_moments_line (no SCAN)",
          "idx_srs_moments_line" in plan and "SCAN srs_moments" not in plan)

    # mark one moment user_rejected: the marker must survive the re-ingest
    with cursor() as cx:
        cx.execute("UPDATE srs_moments SET verdict='user_rejected' WHERE lemma='勉強'")
        rows = [dict(r) for r in cx.execute("SELECT * FROM subtitle_lines WHERE episode_id=1")]
        cx.execute("DELETE FROM subtitle_lines WHERE episode_id=1")
        for r in rows:                                     # re-insert with new ids
            cx.execute(
                "INSERT INTO subtitle_lines(subtitle_id, episode_id, idx, start_ms, end_ms, text, "
                "text_furigana, translation) VALUES(1,1,?,?,?,?,?,?)",
                (r["idx"], r["start_ms"] + 20, r["end_ms"], r["text"], r["text_furigana"],
                 r["translation"]),
            )
    check("re-ingest nulls line_id on the card", card_row(cid)["line_id"] is None)
    with cursor() as cx:
        fixed = service.relink_episode(cx, 1)
    row = card_row(cid)
    with connect() as cx:
        m = cx.execute("SELECT * FROM srs_moments WHERE lemma='勉強'").fetchone()
    check("relink re-attaches cards and moments by content",
          fixed >= 2 and row["line_id"] is not None and m["line_id"] is not None,
          f"{fixed} rows fixed")
    check("relink refreshes the timings from the new row", row["start_ms"] == 9020,
          str(row["start_ms"]))
    check("the user_rejected marker survived the re-ingest", m["verdict"] == "user_rejected")

    # deleting the title keeps the card, drops the source
    from app.catalog.service import delete_title

    delete_title(1, delete_files=False)
    row = card_row(cid)
    with connect() as cx:
        card = service._card_model(cx, cx.execute("SELECT * FROM srs_cards WHERE id=?", (cid,)).fetchone())
    check("delete_title keeps the card with line_id NULL and no source",
          row["line_id"] is None and not card.source_available and card.text,
          f"line_id={row['line_id']} source={card.source_available}")
    # restore the fixture for the remaining tests
    with cursor() as cx:
        cx.execute("DELETE FROM srs_cards")
        cx.execute("DELETE FROM srs_moments")
        cx.execute("DELETE FROM srs_words")
    seed()


# ---------------------------------------------------------------------------
# 7. Reconcile (§5.10) and the learn integration (§2.6)
# ---------------------------------------------------------------------------

def set_migaku(lemma: str, status: str | None) -> None:
    with cursor() as cx:
        cx.execute("DELETE FROM known_words WHERE dict_form=?", (lemma,))
        if status:
            cx.execute(
                "INSERT INTO known_words(dict_form, reading, status) VALUES(?,'',?)",
                (lemma, status),
            )


def test_reconcile(clock: Clock) -> None:
    section("edge-triggered Migaku reconcile (§5.10)")
    wipe_cards()
    clock.set(datetime(2027, 4, 1, 13, 0, tzinfo=UTC))
    cid = insert_card("天気", migaku_status_seen="ABSENT")
    set_migaku("天気", "KNOWN")
    service._job_reconcile({})
    row = card_row(cid)
    check("KNOWN in Migaku retires the card",
          row["state"] == "known" and row["known_source"] == "migaku"
          and row["migaku_status_seen"] == "KNOWN" and row["queue_pos"] is None)
    service.card_action(cid, "resume")
    service._job_reconcile({})
    check("resume → reconcile leaves the card alone (edge-triggered)",
          card_row(cid)["state"] == "new", card_row(cid)["state"])
    set_migaku("天気", "UNKNOWN")
    service._job_reconcile({})
    check("a later UNKNOWN never revives/retires, only refreshes what we saw",
          card_row(cid)["state"] == "new" and card_row(cid)["migaku_status_seen"] == "UNKNOWN")
    set_migaku("天気", "KNOWN")
    service._job_reconcile({})
    check("UNKNOWN → KNOWN retires it again", card_row(cid)["state"] == "known")

    # IGNORED parks the card and clears study_now
    wipe_cards()
    cid = insert_card("勉強", study_now=1, migaku_status_seen="ABSENT")
    set_migaku("勉強", "IGNORED")
    service._job_reconcile({})
    row = card_row(cid)
    check("IGNORED in Migaku parks the card",
          row["state"] == "suspended" and row["suspend_reason"] == "migaku_ignored"
          and row["study_now"] == 0 and row["queue_pos"] is None)

    # a manual card for a KNOWN word survives reconcile
    set_migaku("日本語", "KNOWN")
    out = service.create_card_manual(SrsCreateCard(lemma="日本語", line_id=line_id_of(3)))
    check("manual card for a Migaku-KNOWN word warns but is allowed",
          out["warning"] == "known_in_migaku")
    service._job_reconcile({})
    check("…and survives the next reconcile",
          card_row(out["card"]["id"])["state"] == "new")

    # upload_known_words enqueues the reconcile unconditionally
    with cursor() as cx:
        cx.execute("DELETE FROM jobs WHERE type='srs_reconcile'")
    from app.known.service import upload_known_words

    rows = [{"dictForm": r["dict_form"], "reading": r["reading"], "knownStatus": r["status"]}
            for r in connect().execute("SELECT * FROM known_words")]
    upload_known_words(rows)
    with connect() as cx:
        n = cx.execute(
            "SELECT COUNT(*) n FROM jobs WHERE type='srs_reconcile' AND state='queued'").fetchone()["n"]
    check("upload_known_words always enqueues srs_reconcile", n == 1, f"{n} jobs")
    set_migaku("日本語", None)


def test_learn_integration() -> None:
    section("comprehension integration (§2.6)")
    wipe_cards()
    from app.learn import service as L

    set_migaku("天気", None)                                # the SRS is the only opinion here
    cid = insert_card("天気")
    set_card(cid, state="review", step=None, stability=30.0, difficulty=5.0, scheduled_days=30,
             queue_pos=None, due_at=service._sql(datetime(2027, 5, 1, tzinfo=UTC)))
    check("known_forms() contains a mature review card", "天気" in service.known_forms())
    check("active_forms() excludes it", "天気" not in service.active_forms())
    known, ignored = L._known_lookup()
    check("_known_lookup() unions the SRS-known set", "天気" in known)
    with connect() as cx:
        counts = L._unknown_counts(cx, [line_id_of(2)])
    check("_unknown_counts excludes an SRS-known lemma", counts.get(line_id_of(2), 0) == 0,
          str(counts))

    set_card(cid, state="learning", step=1, scheduled_days=0, stability=2.0,
             last_review_at=service._sql(datetime(2027, 4, 1, tzinfo=UTC)))
    check("a learning card is active, not known",
          "天気" in service.active_forms() and "天気" not in service.known_forms())
    tr = L.episode_transcript(1)
    statuses = {t["dict_form"]: t["status"] for ln in tr["lines"] for t in ln["tokens"]}
    check("episode_transcript marks an active card's token LEARNING",
          statuses.get("天気") == "LEARNING", str(statuses.get("天気")))


# ---------------------------------------------------------------------------
# 8. Importer (§5.11)
# ---------------------------------------------------------------------------

def test_importer() -> None:
    section("curated deck import (§5.11)")
    wipe_cards()
    pre = insert_card("既存語")                            # already in the stack
    doc = {
        "generated_at": "2026-09-03", "source": "selftest",
        "cards": [
            {
                "lemma": "天気", "reading": "てんき", "pos": "noun",
                "meaning_short": "weather", "meaning_full": "weather; the sky",
                "why_clear": "いい天気ですね names the weather directly.",
                "usage_note": "everyday small talk", "line_id": line_id_of(2), "episode_id": 1,
                "anilist_id": 1, "show": "TestShow", "ep_number": 1, "start_ms": 5000,
                "end_ms": 8000, "text": "今日はいい天気ですね。",
                "translation": "Nice weather today, isn't it?", "target_surface": "天気",
                "alt_line_ids": [], "clarity": 0.9, "usefulness": 0.95, "priority": 5,
                "tags": ["daily-life"], "jpdb_rank": 1500, "occurrences": 3, "episodes": 1,
                "leverage_crossings": 0, "stack_score": 60.0, "stack_rank": 1,
            },
            {
                # a stale line_id that must be resolved by (episode, norm_text, start_ms)
                "lemma": "勉強", "reading": "べんきょう", "pos": "noun",
                "meaning_short": "study", "line_id": 999999, "episode_id": 1,
                "start_ms": 9000, "end_ms": 12000, "text": "日本語を勉強しています。",
                "translation": "I'm studying Japanese.", "target_surface": "勉強",
                # line 5 also contains 勉強; line 1 does not and must be skipped
                "alt_line_ids": [line_id_of(5), line_id_of(1)], "clarity": 0.8, "usefulness": 0.8,
                "priority": 4, "tags": [], "stack_score": 55.0, "stack_rank": 2,
            },
        ],
    }
    from app.srs.importer import import_cards

    rep = import_cards(doc)
    check("import creates both cards", rep.created == 2 and not rep.errors,
          f"created={rep.created} errors={[e.model_dump() for e in rep.errors]}")
    check("import queues one clip job per card", rep.clip_jobs_queued == 2)
    order = [(r["lemma"], r["queue_pos"]) for r in _stack_order()]
    check("the curated deck goes on top, the existing card is pushed below",
          order[0][0] == "天気" and order[1][0] == "勉強" and order[-1][0] == "既存語", str(order))
    with connect() as cx:
        row = cx.execute("SELECT * FROM srs_cards WHERE lemma='勉強'").fetchone()
        word = cx.execute("SELECT * FROM srs_words WHERE lemma='天気'").fetchone()
        moments = cx.execute("SELECT COUNT(*) n FROM srs_moments WHERE lemma='勉強'").fetchone()["n"]
    check("a stale line_id is resolved by (episode_id, norm_text, start_ms)",
          row["line_id"] == line_id_of(3), str(row["line_id"]))
    check("curated fields land on the card",
          row["source"] == "curated-initial" and row["meaning_short"] == "study"
          and row["score"] == 55.0 and row["priority"] == 4)
    check("srs_words is marked accepted with the card id",
          word and word["judge_status"] == "accepted" and word["card_id"])
    check("the alternate line becomes an accepted srs_moments row", moments == 2, f"{moments} moments")
    check("import shifted, then renumbered the stack densely",
          [p for _, p in order] == list(range(1, len(order) + 1)), str(order))

    rep2 = import_cards(doc)
    check("re-running the import is idempotent",
          rep2.created == 0 and rep2.skipped_existing == 2, str(rep2.model_dump()))

    bad = {"cards": [{"lemma": "無い語", "episode_id": 1, "line_id": 999999,
                      "text": "存在しない行", "start_ms": 0}]}
    rep3 = import_cards(bad)
    check("an unresolvable line is reported as an error, not a crash",
          rep3.created == 0 and len(rep3.errors) == 1, str([e.model_dump() for e in rep3.errors]))
    assert_stack_invariants("import")

    # §5.13: the pre-existing stack is shifted by the deck's *rank span*, not by
    # the number of cards actually created — otherwise one skipped or failed
    # card interleaves the old rows with the curated deck.
    pre = [r["lemma"] for r in _stack_order()]
    gap_doc = {"cards": [
        {"lemma": "今日", "line_id": line_id_of(2), "episode_id": 1,
         "text": "今日はいい天気ですね。", "start_ms": 5000, "target_surface": "今日",
         "translation": "Nice weather today, isn't it?", "stack_rank": 1},
        {"lemma": "無い語二", "episode_id": 1, "line_id": 999998,
         "text": "存在しない行", "start_ms": 0, "stack_rank": 2},
        {"lemma": "日本語", "line_id": line_id_of(3), "episode_id": 1,
         "text": "日本語を勉強しています。", "start_ms": 9000, "target_surface": "日本語",
         "translation": "I'm studying Japanese.", "stack_rank": 3},
    ]}
    rep4 = import_cards(gap_doc)
    order4 = [r["lemma"] for r in _stack_order()]
    check("a failed card mid-deck does not interleave the pre-existing stack",
          rep4.created == 2 and len(rep4.errors) == 1
          and order4[:2] == ["今日", "日本語"] and order4[2:] == pre,
          f"created={rep4.created} order={order4}")
    assert_stack_invariants("import with a gap")


# ---------------------------------------------------------------------------
# 9. Summary, stats, generation surface
# ---------------------------------------------------------------------------

def test_evidence_lines() -> None:
    section("evidence lines: extend, clip window, importer, apply tool (§5.7, §6.2)")
    wipe_cards()
    l1, l2, l3, l4 = line_id_of(1), line_id_of(2), line_id_of(3), line_id_of(4)

    snap = snapshot_line(l3, "勉強", evidence_line_ids=[l2, l1])
    extend = snap["extend"]
    check("snapshot_line resolves evidence ids into chronological extend lines",
          [x["line_id"] for x in extend] == [l1, l2]
          and all(x["role"] == "evidence" for x in extend)
          and all(x["text"] and x["idx"] is not None for x in extend), str(extend))
    check("the snapshot reports the evidence ids it actually kept",
          snap["evidence_line_ids"] == [l1, l2] and snap["extend_truncated"] is False)
    check("a self-sufficient sentence keeps an empty extend",
          snapshot_line(l3, "勉強")["extend"] == [])
    check("the target's own id and a dead id are dropped",
          snapshot_line(l3, "勉強", evidence_line_ids=[l3, 999999])["extend"] == [])
    # 2026-09-03: the distance limit follows the blind judge's widest window
    # (MAX_EVIDENCE_IDX_DISTANCE = 16), so l1 (3 rows away) is accepted; the
    # rule itself is checked with the limit pinned back to 2.
    import app.srs.snapshot as _snap
    check("a line within MAX_EVIDENCE_IDX_DISTANCE of the target is accepted as evidence",
          [x["line_id"] for x in snapshot_line(l4, "テスト", evidence_line_ids=[l1])["extend"]] == [l1])
    _orig_dist = _snap.MAX_EVIDENCE_IDX_DISTANCE
    _snap.MAX_EVIDENCE_IDX_DISTANCE = 2
    try:
        check("a line further than MAX_EVIDENCE_IDX_DISTANCE from the target is not an evidence line",
              snapshot_line(l4, "テスト", evidence_line_ids=[l1])["extend"] == [])
    finally:
        _snap.MAX_EVIDENCE_IDX_DISTANCE = _orig_dist

    from app.srs.snapshot import truncate_extend

    kept, truncated = truncate_extend({"start_ms": 9000, "end_ms": 12000}, extend, cap_ms=9000)
    check("truncate_extend drops the farthest evidence line first and says so",
          truncated and [x["line_id"] for x in kept] == [l2], str(kept))

    # the clip window covers both preceding lines
    from app.srs import clips

    plan = clips.plan_window({"start_ms": 9000, "end_ms": 12000}, extend, None, None, 30000)
    check("the clip window covers the two preceding evidence lines",
          plan.start_ms <= 1000 and plan.end_ms >= 12000 and plan.poster_ms == 9000,
          f"{plan.start_ms}-{plan.end_ms} poster={plan.poster_ms}")

    # --- importer -----------------------------------------------------------
    from app.srs.importer import import_cards

    entry = {
        "lemma": "天気", "reading": "てんき", "pos": "noun", "meaning_short": "weather",
        "line_id": l2, "episode_id": 1, "start_ms": 5000, "end_ms": 8000,
        "text": "今日はいい天気ですね。", "translation": "Nice weather today, isn't it?",
        "target_surface": "天気", "stack_rank": 1,
        "evidence_line_ids": [l1], "evidence_reason": "the previous line asks about the sky",
    }
    rep = import_cards({"cards": [entry]})
    cid = int(card_row(_stack_order()[0]["id"])["id"]) if rep.created else 0
    row = card_row(cid)
    card_extend = json.loads(row["extend_json"] or "[]")
    check("the importer builds extend from the curation entry's evidence_line_ids",
          rep.created == 1 and [x["line_id"] for x in card_extend] == [l1]
          and card_extend[0]["role"] == "evidence", str(card_extend))
    with connect() as cx:
        moment = cx.execute(
            "SELECT * FROM srs_moments WHERE lemma='天気' AND norm_text=?",
            (norm_text("今日はいい天気ですね。"),)).fetchone()
    check("the importer stores the evidence ids on the moment",
          moment and json.loads(moment["evidence_line_ids_json"] or "[]") == [l1],
          str(moment["evidence_line_ids_json"]) if moment else "no moment")

    # --- swap carries the moment's evidence back ----------------------------
    l5 = line_id_of(5)
    service.set_moment(cid, SrsSwapMoment(line_id=l5))
    check("swapping onto a user-picked line has no evidence lines",
          json.loads(card_row(cid)["extend_json"] or "[]") == [],
          card_row(cid)["extend_json"])
    service.set_moment(cid, SrsSwapMoment(moment_id=int(moment["id"])))
    swapped = json.loads(card_row(cid)["extend_json"] or "[]")
    check("swapping back onto the curated moment restores its evidence lines",
          [x["line_id"] for x in swapped] == [l1] and card_row(cid)["clip_version"] == 3,
          f"{swapped} v={card_row(cid)['clip_version']}")

    # --- tools/srs_apply_evidence.py ----------------------------------------
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "srs_apply_evidence", REPO / "tools" / "srs_apply_evidence.py")
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)                       # type: ignore[union-attr]

    set_card(cid, extend_json="[]", clip_version=3, clip_status="ready")
    cards_path = Path(TMP) / "evidence-cards.json"
    cards_path.write_text(json.dumps({"cards": [entry]}, ensure_ascii=False))
    argv = sys.argv
    try:
        sys.argv = ["srs_apply_evidence.py", "--cards", str(cards_path), "--dry-run"]
        rc_dry = tool.main()
        after_dry = card_row(cid)
        sys.argv = ["srs_apply_evidence.py", "--cards", str(cards_path)]
        rc = tool.main()
        after = card_row(cid)
        sys.argv = ["srs_apply_evidence.py", "--cards", str(cards_path)]
        rc2 = tool.main()
        again = card_row(cid)
    finally:
        sys.argv = argv
    with connect() as cx:
        jobs = cx.execute(
            "SELECT payload_json FROM jobs WHERE type='srs_clip' AND payload_json=?",
            (json.dumps({"card_id": cid, "v": 4}, sort_keys=True),)).fetchall()
    check("--dry-run writes nothing", rc_dry == 0 and after_dry["extend_json"] == "[]"
          and after_dry["clip_version"] == 3, str(after_dry["clip_version"]))
    check("the apply tool rebuilds extend and bumps clip_version",
          rc == 0 and [x["line_id"] for x in json.loads(after["extend_json"])] == [l1]
          and after["clip_version"] == 4 and after["clip_status"] == "pending"
          and after["clip_start_ms"] is not None and after["clip_start_ms"] <= 1000,
          str({k: after[k] for k in ("clip_version", "clip_status", "clip_start_ms")}))
    check("the apply tool enqueues exactly one srs_clip job for the new version",
          len(jobs) == 1, str([dict(j) for j in jobs]))
    check("running the apply tool again is a no-op",
          rc2 == 0 and again["clip_version"] == 4
          and again["extend_json"] == after["extend_json"])
    wipe_cards()


def test_summary_stats(clock: Clock) -> None:
    section("summary, stats and the generation surface")
    s = service.summary()
    check("summary zero-fills all seven states", len(s.states) == 7, str(s.states))
    check("summary carries the day, server time and settings",
          s.day and s.server_time.endswith("Z") and s.settings.demote_after_fails == 2)
    check("summary counts the stack and its clips",
          s.new_stack_total == len(_stack_order()) and s.clips.pending >= 1,
          f"stack={s.new_stack_total} clips={s.clips.model_dump()}")
    st = service.stats(days=30)
    check("stats returns a dense day series and a 30-day forecast",
          len(st.days) == 30 and len(st.forecast) == 30)
    check("stats zero-fills states and buckets intervals",
          len(st.states) == 7 and len(st.intervals) == 7)
    g = service.generation(limit=5)
    check("generation reports the judge model and llm availability",
          g.judge_model_id and g.llm_available is False, g.judge_model_id)

    # a run whose batch job died is not "live" → generate is allowed again
    with cursor() as cx:
        cx.execute(
            "INSERT INTO srs_generation_runs(trigger, state, want, started_at) "
            "VALUES('manual','judging',10, datetime('now','-3 hours'))")
        run_id = cx.execute("SELECT MAX(id) m FROM srs_generation_runs").fetchone()["m"]
        cx.execute(
            "INSERT INTO jobs(type, payload_json, state, priority, run_after) "
            "VALUES('srs_judge_batch', ?, 'error', 55, datetime('now'))",
            (json.dumps({"batch_no": 0, "run_id": run_id}, sort_keys=True),),
        )
    from app.models import SrsGenerateRequest

    out = service.start_generation(SrsGenerateRequest(want=5))
    check("a run with only a dead batch job does not block /generate", out["queued"] is True, str(out))
    with cursor() as cx:                                   # now a genuinely live run
        cx.execute(
            "INSERT INTO srs_generation_runs(trigger, state, want, started_at, heartbeat_at) "
            "VALUES('manual','judging',10, datetime('now'), datetime('now'))")
        live_run = cx.execute("SELECT MAX(id) m FROM srs_generation_runs").fetchone()["m"]
        cx.execute(
            "INSERT INTO jobs(type, payload_json, state, priority, run_after) "
            "VALUES('srs_judge_batch', ?, 'queued', 55, datetime('now'))",
            (json.dumps({"batch_no": 0, "run_id": live_run}, sort_keys=True),),
        )
    try:
        service.start_generation(SrsGenerateRequest())
        check("a live batch job blocks /generate with 409", False, "no raise")
    except service.SrsConflict:
        check("a live batch job blocks /generate with 409", True)
    with cursor() as cx:
        cx.execute("DELETE FROM jobs WHERE type='srs_judge_batch'")
        cx.execute("UPDATE srs_generation_runs SET state='done'")

    # candidates + word verbs
    with cursor() as cx:
        cx.execute(
            "INSERT INTO srs_words(lemma, score, judge_status, occ, eps) "
            "VALUES('候補語', 42.0, 'unjudged', 5, 2) ON CONFLICT(lemma) DO NOTHING")
    cands = service.candidates(status="unjudged", limit=10)
    check("candidates lists unjudged words",
          any(c.lemma == "候補語" for c in cands.items), f"{cands.total} total")
    c = service.word_skip("候補語")
    check("skip blocklists the word", c.judge_status == "rejected" and c.user_flag == "skip")
    c = service.word_unskip("候補語")
    check("unskip returns it to unjudged", c.judge_status == "unjudged")
    check("judge now enqueues srs_find_moments",
          service.word_judge("候補語") == {"queued": True, "lemma": "候補語"})


def test_periodic_tick() -> None:
    section("periodic tick (§9.2)")
    from app.db import kv_set

    with cursor() as cx:
        cx.execute("DELETE FROM jobs")
        cx.execute(
            "UPDATE srs_cards SET clip_status='pending', clip_requested_at=datetime('now','-2 hours')")
    kv_set("srs.reconcile.last", "1970-01-01 00:00:00")     # the daily backstop is due
    kv_set("srs.audit.last", "1970-01-01 00:00:00")
    service.periodic_tick()
    with connect() as cx:
        types = {r["type"]: r["priority"] for r in cx.execute(
            "SELECT DISTINCT type, priority FROM jobs")}
    check("tick enqueues generation, reconcile and the clip audit at their priorities",
          types.get("srs_generate") == 60 and types.get("srs_reconcile") == 30
          and types.get("srs_clip_audit") == 90, str(types))
    check("tick re-enqueues stale pending clips", types.get("srs_clip") == 30, str(types))
    service.periodic_tick()
    with connect() as cx:
        n = cx.execute("SELECT COUNT(*) n FROM jobs WHERE type='srs_generate'").fetchone()["n"]
    check("a second tick dedups the generation job", n == 1, f"{n} jobs")


# ---------------------------------------------------------------------------
# 10. HTTP smoke (§10.1)
# ---------------------------------------------------------------------------

def test_http(clock: Clock) -> None:
    section("HTTP smoke over the §7.2 routes")
    from fastapi.testclient import TestClient

    from app.main import app

    c = TestClient(app)                                    # no lifespan: no scheduler threads
    r = c.get("/api/srs/summary")
    check("GET /api/srs/summary → 200 with seven states",
          r.status_code == 200 and len(r.json()["states"]) == 7, str(r.status_code))
    check("GET /api/srs/queue → 200", c.get("/api/srs/queue?limit=5").status_code == 200)
    check("GET /api/srs/cards → 200", c.get("/api/srs/cards?limit=1").status_code == 200)
    check("GET /api/srs/cards?limit=0 returns the whole stack",
          len(c.get("/api/srs/cards?state=new&limit=0").json()["items"]) == len(_stack_order()))
    check("GET /api/srs/candidates → 200", c.get("/api/srs/candidates?limit=3").status_code == 200)
    check("GET /api/srs/generation → 200", c.get("/api/srs/generation").status_code == 200)
    check("GET /api/srs/stats → 200", c.get("/api/srs/stats?days=7").status_code == 200)

    r = c.get("/api/srs/settings")
    check("GET /api/srs/settings → 200 with the defaults",
          r.status_code == 200 and r.json()["demote_after_fails"] == 2)
    check("PUT /api/srs/settings with new_per_day=99 → 422",
          c.put("/api/srs/settings", json={"new_per_day": 99}).status_code == 422)
    check("PUT /api/srs/settings with a valid value → 200",
          c.put("/api/srs/settings", json={"new_per_day": 7}).json()["new_per_day"] == 7)
    c.put("/api/srs/settings", json={"new_per_day": 10})

    stack = c.get("/api/srs/cards?state=new&limit=0").json()["items"]
    cid = stack[-1]["id"]
    r = c.post("/api/srs/stack/move", json={"card_id": cid, "position": 0})
    check("POST /api/srs/stack/move → 200 and the card is on top",
          r.status_code == 200 and r.json()["card"]["queue_pos"] == 1, str(r.status_code))
    r = c.post(f"/api/srs/cards/{cid}/action", json={"action": "bury"})
    check("POST /api/srs/cards/{id}/action → 200", r.status_code == 200 and r.json()["card"]["buried_until"])
    c.post(f"/api/srs/cards/{cid}/action", json={"action": "unbury"})
    check("an action that is illegal from this state → 422",
          c.post(f"/api/srs/cards/{cid}/action", json={"action": "restore"}).status_code == 422)
    check("an unknown card → 404",
          c.post("/api/srs/cards/999999/action", json={"action": "bury"}).status_code == 404)

    body = {"card_id": cid, "rating": 3, "elapsed_ms": 1200, "client_id": "http-1"}
    r = c.post("/api/srs/review", json=body)
    check("POST /api/srs/review → 200", r.status_code == 200 and r.json()["card"]["state"] == "learning",
          str(r.status_code))
    r2 = c.post("/api/srs/review", json=body)
    check("the same client_id returns the stored result with duplicate=true",
          r2.status_code == 200 and r2.json()["duplicate"] is True)
    c.post(f"/api/srs/cards/{cid}/action", json={"action": "known"})
    r3 = c.post("/api/srs/review", json={**body, "client_id": "http-2"})
    check("rating a retired card → 409 carrying the card",
          r3.status_code == 409 and r3.json()["card"]["id"] == cid, str(r3.status_code))
    r4 = c.post("/api/srs/review/undo", json={})
    check("POST /api/srs/review/undo → 200", r4.status_code == 200 and r4.json()["undone_review_id"])
    check("nothing left to undo → 404", c.post("/api/srs/review/undo", json={}).status_code == 404)

    # import: no token needed, path must stay under data/
    path = Path(TMP) / "cards.json"
    fixture = {"cards": [{"lemma": "字幕", "line_id": line_id_of(1), "episode_id": 1,
                          "text": "これはテストの字幕です。", "start_ms": 1000, "end_ms": 4000,
                          "target_surface": "字幕", "stack_rank": 1, "tags": []}]}
    (Path(REPO) / "data").mkdir(exist_ok=True)
    data_fixture = Path(REPO) / "data" / "srs_selftest_cards.json"
    data_fixture.write_text(json.dumps(fixture, ensure_ascii=False), encoding="utf-8")
    path.write_text(json.dumps(fixture), encoding="utf-8")
    try:
        r = c.post("/api/srs/import", json={"path": "data/srs_selftest_cards.json"})
        check("POST /api/srs/import works without a bearer token",
              r.status_code == 200 and r.json()["created"] == 1, str(r.status_code) + r.text[:120])
        r = c.post("/api/srs/import", json={"path": str(path)})
        check("a path outside data/ → 422", r.status_code == 422, str(r.status_code))
    finally:
        data_fixture.unlink(missing_ok=True)

    check("POST /api/learn/anki/export is gone (404)",
          c.post("/api/learn/anki/export", json={"line_ids": []}).status_code == 404)
    check("POST /api/learn/anki/export.apkg is gone (404)",
          c.post("/api/learn/anki/export.apkg", json={"line_ids": []}).status_code == 404)


# ---------------------------------------------------------------------------

def main() -> int:
    print("=== app.srs selftest (WP-A) ===")
    print(f"scratch DB: {TMP}/test.db")
    init_db()
    seed()
    clock = Clock(datetime(2026, 9, 10, 13, 0, tzinfo=UTC))
    service._now = clock                                   # drive reviews across days

    test_golden()
    test_state_machine()
    test_days()
    test_failed_repeats(clock)
    test_demote_swaps_moment(clock)
    test_review_api(clock)
    test_queue(clock)
    test_stack(clock)
    test_resume_table(clock)
    test_snapshot_and_cards()
    test_relink()
    test_reconcile(clock)
    test_learn_integration()
    test_importer()
    test_summary_stats(clock)
    test_periodic_tick()
    test_http(clock)
    test_evidence_lines()   # wipes the deck — keep last

    print()
    if _fails:
        print(f"=== RESULT: FAIL ({len(_fails)}/{_checks} failed) ===")
        for name in _fails:
            print(f"  ✗ {name}")
        return 1
    print(f"=== RESULT: PASS ({_checks} checks) ===")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(code)
