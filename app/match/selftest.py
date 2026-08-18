"""Self-test for the match module.

Run:  .venv/bin/python -m app.match.selftest

Deterministic parsing and ranking use synthetic fixtures. Optional live AniList
and Fribb checks run only when all three variables are set:

  MIMI_TEST_ANILIST_FILENAME
  MIMI_TEST_ANILIST_ID
  MIMI_TEST_MAL_ID
"""
from __future__ import annotations

import os
import sys

from . import service

SAMPLES = [
    "[ReleaseGroup] Example Series - 01 (1080p).mkv",
    "[ReleaseGroup] Example Series Special - 05 [1080p][HEVC].mkv",
    "[ReleaseGroup] Example Series - 12 [1080p HEVC].mkv",
]
MAIN_ID = 99000001
SPECIAL_ID = 99000002

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


def _fmt(c) -> str:
    return (
        f"    {c.anilist_id:>8} {str(c.format):>4} eps={c.episodes} "
        f"score={c.score:.3f} | {c.romaji} / {c.english}\n"
        f"             reason: {c.reason}"
    )


def _deterministic_checks() -> None:
    print("\nDETERMINISTIC — parse + ranking")
    for name in SAMPLES:
        parsed = service.parse_filename(name)
        check(
            f"parse has title+episode ({parsed.get('anime_title')!r})",
            bool(parsed.get("anime_title")) and parsed.get("episode_number") is not None,
        )

    parsed = service.parse_filename(SAMPLES[2])
    ranked = service.rank_candidates(
        parsed,
        [
            {
                "id": MAIN_ID,
                "romaji": "Example Series",
                "english": "Example Series",
                "native": None,
                "episodes": 24,
                "format": "TV",
                "seasonYear": 2025,
            },
            {
                "id": SPECIAL_ID,
                "romaji": "Example Series Special",
                "english": "Example Series: Special",
                "native": None,
                "episodes": 12,
                "format": "ONA",
                "seasonYear": 2025,
            },
        ],
    )
    for candidate in ranked:
        print(_fmt(candidate))
    check("main TV series ranks first", bool(ranked) and ranked[0].anilist_id == MAIN_ID)
    by_id = {candidate.anilist_id: candidate for candidate in ranked}
    check(
        "exact-title TV outranks episode-count-matching special",
        by_id[MAIN_ID].score > by_id[SPECIAL_ID].score,
        f"main={by_id[MAIN_ID].score:.3f} special={by_id[SPECIAL_ID].score:.3f}",
    )
    margin = by_id[MAIN_ID].score - by_id[SPECIAL_ID].score
    check(
        "synthetic result clears confidence thresholds",
        by_id[MAIN_ID].score >= service.CONFIDENT_MIN_SCORE
        and margin >= service.CONFIDENT_MARGIN,
        f"score={by_id[MAIN_ID].score:.3f} margin={margin:.3f}",
    )
    _offset_checks()


# (numbers seen in one batch, aired ceiling, expected offset, label)
_OFFSET_CASES = [
    ([1, 2, 3, 13, 14, 15, 16, 17, 18, 19], 7, 12,
     "mixed relative+absolute in one entry folds (S1 was 12 eps)"),
    (list(range(1, 19)) + list(range(73, 91)), 18, 72,
     "long-running sequel folds (3 prior seasons = 72 eps)"),
    ([13, 14, 15, 16, 17, 18, 19], 7, 12,
     "absolute-only batch still folds"),
    (["01", "02", "13", "14"], 2, 12,
     "anitopy's string episode numbers are accepted"),
    ([1, 2, 3, 4, 5], 5, None,
     "all-relative batch has nothing to fold"),
    ([1, 2, 3], 0, None,
     "unknown ceiling declines (cannot tell relative from absolute)"),
    ([1, 2, 3, 8], 7, None,
     "number one past the ceiling is a just-aired episode, not absolute"),
    ([1, 2, 3, 4, 5, 6, 7, 8, 9], 7, None,
     "two just-aired episodes ahead of a lagging ceiling still decline"),
    ([1, 2, 3, 13, 14, 15, 99], 7, None,
     "one number that folds out of range rejects the whole inference"),
    ([1, 2, 25, 26], 12, 24,
     "third season folds by the two prior seasons"),
    ([], 7, None, "empty batch"),
    ([0, -3, None], 7, None, "junk numbers"),
]


def _offset_checks() -> None:
    """`infer_episode_offset` — absolute→relative folding from a whole batch."""
    for numbers, ceiling, expected, label in _OFFSET_CASES:
        got = service.infer_episode_offset(numbers, ceiling)
        check(f"offset: {label}", got == expected,
              f"ceiling={ceiling} -> {got} (expected {expected})")


def _live_checks() -> None:
    filename = os.getenv("MIMI_TEST_ANILIST_FILENAME", "").strip()
    raw_anilist = os.getenv("MIMI_TEST_ANILIST_ID", "").strip()
    raw_mal = os.getenv("MIMI_TEST_MAL_ID", "").strip()
    if not (filename and raw_anilist and raw_mal):
        print("\nSKIP: live AniList/Fribb checks (set MIMI_TEST_ANILIST_FILENAME, "
              "MIMI_TEST_ANILIST_ID, and MIMI_TEST_MAL_ID)")
        return
    try:
        expected_anilist = int(raw_anilist)
        expected_mal = int(raw_mal)
    except ValueError:
        check("live fixture ids are integers", False)
        return

    print("\nLIVE — configured AniList fixture")
    result = service.match_file(filename)
    check(
        "configured filename matches configured AniList id",
        bool(result.best) and result.best.anilist_id == expected_anilist,
        f"got {result.best.anilist_id if result.best else None}",
    )

    summary = service.refresh_idmap()
    check("Fribb id map contains entries", summary.get("entries", 0) > 0)
    mal_id = service.mal_id_for(expected_anilist)
    check("configured AniList id maps to configured MAL id", mal_id == expected_mal,
          f"got {mal_id}")
    check("configured MAL id maps back to configured AniList id",
          service.anilist_id_for(expected_mal) == expected_anilist)

    try:
        from fastapi.testclient import TestClient
        from app.main import app

        with TestClient(app) as client:
            response = client.get("/api/match/preview", params={"filename": filename})
            check("GET /api/match/preview -> 200", response.status_code == 200,
                  f"status={response.status_code}")
            if response.status_code == 200:
                best_id = (response.json().get("best") or {}).get("anilist_id")
                check("preview returns configured AniList id", best_id == expected_anilist,
                      f"got {best_id}")
    except Exception as exc:
        check("live route check", False, f"exception: {exc}")


def main() -> int:
    print("=" * 78)
    print("MATCH SELFTEST")
    print("=" * 78)
    _deterministic_checks()
    _live_checks()
    print("\n" + "=" * 78)
    print(f"MATCH SELFTEST: {_passed} passed, {_failed} failed")
    print("=" * 78)
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
