"""Self-test for the acquire module.  Run:  .venv/bin/python -m app.acquire.selftest

Deterministic release classification always runs. Live nyaa checks run only
when their documented environment fixtures are set:

  MIMI_TEST_NYAA_QUERY
  MIMI_TEST_NYAA_BATCH_ANILIST_ID
  MIMI_TEST_NYAA_BATCH_TITLE
  MIMI_TEST_NYAA_BATCH_ALT_TITLE
  MIMI_TEST_NYAA_BATCH_EPISODES

Verifies (per build spec):
  * configured nyaa LIVE search returns >=1 result with a magnet/torrent link
    and seeders. Prints the top 3 titles.
  * Transmission-unavailable paths degrade without throwing (client stubbed out).
  * NyaaResult field mapping (size/seeders/trusted) is sane.
"""
from __future__ import annotations

import sys
import traceback
import os

from . import service

_passes: list[str] = []
_fails: list[str] = []


def _ok(name: str, cond: bool, detail: str = "") -> None:
    (_passes if cond else _fails).append(f"{name}: {detail}")
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}{' — ' + detail if detail else ''}")


def test_nyaa_live() -> None:
    query = os.getenv("MIMI_TEST_NYAA_QUERY", "").strip()
    if not query:
        print("\n# SKIP nyaa LIVE search (set MIMI_TEST_NYAA_QUERY)")
        return
    print("\n# nyaa LIVE search (configured fixture)")
    try:
        results = service.nyaa_search(query)
    except Exception as e:
        _ok("nyaa_search does not raise", False, f"raised {e!r}")
        traceback.print_exc()
        return

    if not results:
        # treat unreachable network as a soft note, not a hard fail of the module
        print("  [NOTE] nyaa returned 0 results — host may be unreachable from this network")
        _ok("nyaa reachable", False, "0 results (network?)")
        return

    _ok("nyaa returns >=1 result", len(results) >= 1, f"{len(results)} results")

    top = results[:3]
    print("  top 3 titles:")
    for i, r in enumerate(top, 1):
        link = r.magnet or r.torrent_url or "<no link>"
        print(f"    {i}. {r.title}")
        print(f"       seeders={r.seeders} size={r.size} trusted={r.trusted} "
              f"link={link[:60]}{'…' if len(link) > 60 else ''}")

    first = results[0]
    _ok("top result has magnet or torrent link",
        bool(first.magnet or first.torrent_url),
        f"magnet={'yes' if first.magnet else 'no'} torrent={'yes' if first.torrent_url else 'no'}")
    _ok("at least one result has seeders",
        any(r.seeders is not None for r in results),
        f"top seeders={first.seeders}")
    _ok("results carry a size string", any(r.size for r in results), f"top size={first.size}")
    _ok("magnet is a valid magnet URI",
        (first.magnet or "").startswith("magnet:?xt=urn:btih:") if first.magnet else True,
        first.magnet[:50] + "…" if first.magnet else "(torrent-only)")


def test_qbt_unavailable() -> None:
    print("\n# Transmission-unavailable graceful degradation (stubbed)")
    original = service._trans_client
    service._trans_client = lambda: None
    try:
        avail = service.qbt_available()
        _ok("qbt_available() returns False without throwing", avail is False, f"returned {avail}")

        st = service.torrent_state("0" * 40)
        _ok("torrent_state degrades gracefully", st.get("found") is False,
            f"{st.get('reason', st)}")

        lst = service.list_torrents()
        _ok("list_torrents degrades to []", lst == [], f"got {len(lst)} items")

        add = service.add_torrent("magnet:?xt=urn:btih:" + "a" * 40)
        _ok("add_torrent degrades gracefully", add.get("ok") is False,
            f"{add.get('reason')}")
    except Exception as e:
        _ok("unavailable-daemon paths do not throw", False, f"raised {e!r}")
        traceback.print_exc()
    finally:
        service._trans_client = original


def test_batch_classification() -> None:
    """Deterministic (no network): batch vs single-episode classification."""
    print("\n# batch classification (_looks_like_batch)")
    import anitopy

    cases = [
        # (title, expected_is_batch)
        ("[ReleaseGroup] Example Series - 06 (1080p)", False),
        ("[ReleaseGroup] Example Series 4th Season - 06 [1080p]", False),
        ("[ReleaseGroup] Example Series - S04E11 (WEB 1080p)", False),
        # compact season tag on a SINGLE episode must NOT read as a "2-10" range
        ("[ReleaseGroup] Example Series S2 - 10 (1080p).mkv", False),
        ("[ReleaseGroup] Example Series (Season 01 + Season 02 + Directors Cut) [BD]", True),
        ("[ReleaseGroup] Example Series [BD]", True),
        ("[ReleaseGroup] Example Series S01 (BD 1080p x264)", True),
        ("[ReleaseGroup] Example Series S1+S2+OVAs [BD]", True),
        ("[ReleaseGroup] Example Series (01-25) [1080p][Batch]", True),
    ]
    for title, expected in cases:
        got = service._looks_like_batch(title, anitopy.parse(title) or {})
        _ok(f"classify {'batch' if expected else 'single'}: {title[:42]}", got == expected,
            f"expected={expected} got={got}")


def test_batch_live() -> None:
    """LIVE: a back-catalog show surfaces healthy season packs (the whole point —
    individual old episodes are dead, but BD batches are well-seeded)."""
    values = {
        "id": os.getenv("MIMI_TEST_NYAA_BATCH_ANILIST_ID", "").strip(),
        "title": os.getenv("MIMI_TEST_NYAA_BATCH_TITLE", "").strip(),
        "alt": os.getenv("MIMI_TEST_NYAA_BATCH_ALT_TITLE", "").strip(),
        "episodes": os.getenv("MIMI_TEST_NYAA_BATCH_EPISODES", "").strip(),
    }
    if not all(values.values()):
        print("\n# SKIP batch discovery LIVE (set all MIMI_TEST_NYAA_BATCH_* fixtures)")
        return
    try:
        anilist_id = int(values["id"])
        episode_count = int(values["episodes"])
    except ValueError:
        _ok("batch live fixture ids/count are integers", False)
        return
    print("\n# batch discovery LIVE (configured fixture)")
    try:
        cands = service._batch_release_candidates(
            anilist_id, values["title"], values["alt"], episode_count,
        )
    except Exception as e:
        _ok("_batch_release_candidates does not raise", False, f"raised {e!r}")
        traceback.print_exc()
        return

    if not cands:
        print("  [NOTE] 0 batch candidates — nyaa may be unreachable from this network")
        _ok("nyaa reachable for batch search", False, "0 results (network?)")
        return

    _ok("finds >=1 season pack", len(cands) >= 1, f"{len(cands)} packs")
    top = cands[0][0]
    print("  top 3 packs:")
    for r, _p, (span, count) in cands[:3]:
        print(f"    {r.seeders or 0:>4} seeders · {span or '?'} · {r.size or '?'} · {r.title[:56]}")
    _ok("top pack is well-seeded (>10)", (top.seeders or 0) > 10, f"top seeders={top.seeders}")
    _ok("top pack has a magnet/torrent link", bool(top.magnet or top.torrent_url),
        "magnet" if top.magnet else ("torrent" if top.torrent_url else "none"))


def main() -> int:
    print("=" * 64)
    print("ACQUIRE SELFTEST")
    print("=" * 64)
    test_nyaa_live()
    test_batch_classification()
    test_batch_live()
    test_qbt_unavailable()

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
