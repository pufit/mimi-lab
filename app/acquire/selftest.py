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


def test_title_season_ordinal() -> None:
    """Deterministic (no network): a title's season must be read from ALL its
    names, not one. AniList's romaji and english disagree in both directions,
    and reading only one silently yields 1 for a sequel — which makes every
    correctly S02-tagged file in a season pack get rejected on import."""
    print("\n# title season ordinal (title_season_ordinal)")
    cases = [
        # (romaji, english, expected_season)
        # english states it, romaji is the Japanese arc name
        ("EXAMPLE: Kami no Shiken-hen",
         "EXAMPLE: MAGIC WORDS Season 2", 2),
        # english states it, romaji's bare trailing digit does not parse
        ("Example Academia 7", "Example Academia 7th Season", 7),
        ("Example Tensei III: Honki Dasu",
         "Example Tensei: Reincarnation Season 3", 3),
        # the reverse: romaji states it, english is the bare digit
        ("Example Tenshi-sama 2nd Season", "Example Tenshi-sama 2", 2),
        ("Example Level Up: Season 2 - Arise from the Example",
         "Example Level Up Season 2: Arise from the Example", 2),
        # an arc subtitle with digits after the marker must not hide the season
        (_S4_ROMAJI, _S4_ENGLISH, 4),
        # genuine season 1 stays 1
        ("EXAMPLE", "EXAMPLE: MAGIC WORDS", 1),
        # missing names degrade to 1 rather than raising
        (None, None, 1),
        ("Example Series", None, 1),
    ]
    for romaji, english, expected in cases:
        got = service.title_season_ordinal(romaji, english)
        _ok(f"season {expected}: {(romaji or english or '<none>')[:44]}",
            got == expected, f"expected={expected} got={got}")


# A (fictional) sequel whose AniList names carry an arc subtitle after the season
# marker, with the real-world shape: a base ending in a one-letter word, a
# '4th Season' marker, a subtitle with digits romanised two ways. The release
# names below follow the naming habits of the real groups — the offline corpus.
_S4_ROMAJI = "Example Gakuen Monogatari e 4th Season 2-bu Ichi Shou"
_S4_ENGLISH = "Example Gakuen Monogatari e 4th Season: 2-bu 1 Shou"
_S4_BASE = "Example Gakuen Monogatari e"
_S4_NYAA = [  # (title, seeders, trusted)
    ("[SubsPlease] Example Gakuen Monogatari e S4 - 01 (1080p) [78E7377B].mkv", 172, True),
    ("[SubsPlease] Example Gakuen Monogatari e S4 - 01 (720p) [19A250B5].mkv", 26, True),
    ("[ASW] Example Gakuen Monogatari e S4 - 01 [1080p HEVC x265 10Bit][AAC]", 39, False),
    ("[Erai-raws] Example Gakuen Monogatari e 4th Season: 2-bu 1 Shou - 01 "
     "[1080p CR WEB-DL AVC AAC][MultiSub][54A22331]", 32, True),
    ("[Erai-raws] Example Gakuen Monogatari e 4th Season: 2-bu 1 Shou - 02 "
     "[1080p CR WEB-DL AVC AAC][MultiSub][54A22332]", 30, True),
    ("[SubsPlease] Example Gakuen Monogatari e S4 - 10 (480p) [01FFE6F4].mkv", 10, True),
    ("[SubsPlease] Example Gakuen Monogatari e S4 (01-16) (1080p) [Batch]", 74, True),
    ("[SubsPlease] Example Gakuen Monogatari e S3 (01-13) (1080p) [Batch]", 35, True),
    ("[Judas] Example Gakuen Monogatari e (Example School Story) (Season 04) "
     "[1080p][HEVC x265 10bit][Dual-Audio][Multi-Subs] (Batch)", 112, False),
    ("[DB] Example Gakuen Monogatari e | Example School Story (Season 1-3) "
     "[Dual Audio 10bit BD1080p][HEVC-x265] (Batch)", 32, False),
    ("[Yameii] Example School Story - S04E16 [English Dub] [CR WEB-DL 1080p H264 AAC] [6B710057] "
     "(Example Gakuen Monogatari e 4th Season: 2 Bu 1 Shou | Part Two, Chapter One)",
     28, False),
    ("[SubsPlease] Example Gakuen Monogatari e - 01 (1080p) [AAAAAAAA].mkv", 5, True),  # season 1
]


def _fake_nyaa(corpus):
    """Stand-in for nyaa_search with nyaa's semantics: every query word must occur
    as a whole word (a "quoted phrase" as a phrase), case-insensitive."""
    import re

    def _terms(query: str) -> list[str]:
        return [t.strip('"') for t in re.findall(r'"[^"]+"|\S+', query)]

    def search(query: str, category: str = "1_2", trusted: bool = True):
        out = []
        for i, (title, seeders, is_trusted) in enumerate(corpus):
            if trusted and not is_trusted:
                continue
            if all(re.search(r"(?<!\w)" + re.escape(t) + r"(?!\w)", title, re.I) for t in _terms(query)):
                out.append(service.NyaaResult(
                    nyaa_id=str(i), title=title, magnet=f"magnet:?xt=urn:btih:{i:040d}",
                    seeders=seeders, trusted=is_trusted,
                ))
        return out

    return search


def test_sequel_search_offline() -> None:
    """Deterministic (nyaa stubbed): a sequel whose AniList name carries an arc
    subtitle ('… 4th Season 2-bu Ichi Shou') must still find its
    releases. Groups drop the subtitle (SubsPlease 'S4 - 01'), spell it differently
    (Erai-raws '1 Shou') and spell the season differently ('S4' / '4th Season'),
    so querying the exact name found nothing on every episode; and the subtitle's
    digits made anitopy drop the season from the names that DID come back."""
    print("\n# sequel with an arc subtitle: name ladder + season gate (offline)")
    import anitopy

    names = service._search_names(_S4_ROMAJI, _S4_ENGLISH)
    _ok("exact names first (as query text — the english name's ':' becomes a space)",
        names.exact == [_S4_ROMAJI, _S4_ENGLISH.replace(":", "")], f"{names.exact}")
    want = [f"{_S4_BASE} S4", f'{_S4_BASE} "4th Season"', f'{_S4_BASE} "Season 4"']
    _ok("derived: base + S4 / \"4th Season\" / \"Season 4\"", names.derived == want, f"{names.derived}")
    _ok("bare base as last resort", names.bare == [_S4_BASE], f"{names.bare}")
    plain = service._search_names("Example Series", "Example Series")
    _ok("season-1 title: only its exact name (unchanged behaviour)",
        plain.exact == ["Example Series"] and not plain.derived and not plain.bare, f"{plain}")
    part = service._search_names("Example Series 2nd Season Part 2", None)
    _ok("'Part 2' kept in every derived query",
        part.derived and all(q.endswith(" Part 2") for q in part.derived + part.bare), f"{part.derived}")
    plain_sequel = service._search_names("Example Series 2nd Season", None)
    _ok("derived spelling equal to the exact name is not repeated",
        plain_sequel.derived == ["Example Series S2", 'Example Series "Season 2"'], f"{plain_sequel.derived}")
    # nyaa reads ':' '/' '!' '[]' and a leading '-' as query syntax → zero results
    syntax = service._search_names("Re:Example kara Hajimeru Monogatari 3rd Season",
                                   "Re:EXAMPLE -Starting Life in Another Example- Season 3")
    _ok("query text: syntax characters become spaces",
        syntax.exact == ["Re Example kara Hajimeru Monogatari 3rd Season",
                         "Re EXAMPLE Starting Life in Another Example Season 3"], f"{syntax.exact}")
    _ok("query text: in-word hyphens survive ('2-bu', 'Example-san')",
        service._query_text("Example-san 2-bu") == "Example-san 2-bu")
    brackets = service._search_names("[Example no Ko] 2nd Season", "Example/Zero! 2nd Season")
    _ok("query text: brackets / slash / bang stripped, phrases kept",
        brackets.derived == ["Example no Ko S2", 'Example no Ko "Season 2"', "Example Zero S2", 'Example Zero "Season 2"'],
        f"{brackets.derived}")

    noise = service._release_noise_patterns(_S4_ROMAJI, _S4_ENGLISH)
    _ok("noise patterns: one per distinct subtitle spelling", len(noise) == 2, f"{len(noise)}")
    for raw, want_ep in ((_S4_NYAA[3][0], 1), (_S4_NYAA[10][0], 16), (_S4_NYAA[8][0], None)):
        p = anitopy.parse(service._clean_release_title(raw, noise)) or {}
        _ok(f"normalised parse → S4 E{want_ep}: {raw[:38]}",
            service._parsed_season(p) == 4 and service._parsed_ep(p) == want_ep,
            f"season={service._parsed_season(p)} ep={service._parsed_ep(p)}")
    untouched = "[SubsPlease] Example Series S2 Part 2 - 01 (1080p).mkv"
    _ok("'Part 2' is never stripped",
        service._clean_release_title(untouched, service._release_noise_patterns("Example Series 2nd Season Part 2"))
        == untouched)

    original = service.nyaa_search
    service.nyaa_search = _fake_nyaa(_S4_NYAA)
    try:
        cands = service._episode_release_candidates(None, _S4_ROMAJI, 1, alt=_S4_ENGLISH)
        titles = [r.title for r, _p in cands]
        _ok("ep 1: SubsPlease 'S4 - 01' found (name carries no subtitle)",
            any("S4 - 01 (1080p)" in t for t in titles), f"{len(titles)} candidates")
        _ok("ep 1: ASW 'S4 - 01' found", any(t.startswith("[ASW]") for t in titles))
        _ok("ep 1: Erai-raws '… 1 Shou - 01' found (subtitle spelled differently)",
            any("Erai-raws" in t and " - 01 " in t for t in titles))
        leaks = [t for t in titles if " - 02 " in t or " S3 " in t or "S04E16" in t or "[AAAAAAAA]" in t]
        _ok("ep 1: no other episode / season leaks", not leaks, f"{leaks}")
        _ok("ep 1: best pick = trusted 1080p with most seeders (SubsPlease)",
            bool(titles) and titles[0].startswith("[SubsPlease]") and "S4 - 01 (1080p)" in titles[0],
            titles[0][:60] if titles else "none")

        packs = service._batch_release_candidates(None, _S4_ROMAJI, _S4_ENGLISH, 16)
        ptitles = [r.title for r, _p, _s in packs]
        _ok("packs: SubsPlease S4 batch + Judas '(Season 04)' found",
            any("S4 (01-16) (1080p)" in t for t in ptitles) and any(t.startswith("[Judas]") for t in ptitles),
            f"{[t[:40] for t in ptitles]}")
        _ok("packs: the S3 batch and the 'Season 1-3' pack are rejected",
            not any(" S3 " in t or "Season 1-3" in t for t in ptitles))

        # the SAME misparse in the other direction: the season-1 page must not offer
        # the S4 release whose subtitle hid its season from anitopy
        s1 = [r.title for r, _p in service._episode_release_candidates(None, _S4_BASE, 1)]
        _ok("season-1 page: its own '- 01' found", any("[AAAAAAAA]" in t for t in s1), f"{len(s1)} candidates")
        _ok("season-1 page: the S4 '… 1 Shou - 01' release is rejected",
            not any("Shou" in t or " S4 " in t for t in s1), f"{[t[:50] for t in s1]}")
        s1_packs = [r.title for r, _p, _s in service._batch_release_candidates(None, _S4_BASE, None, 12)]
        _ok("season-1 packs: multi-season 'Season 1-3' kept, S3/S4 packs rejected",
            any("Season 1-3" in t for t in s1_packs) and not any(" S3 " in t or " S4 " in t or "Season 04" in t
                                                                for t in s1_packs),
            f"{[t[:40] for t in s1_packs]}")
        span = service._episode_span(_S4_NYAA[9][0], {}, 12)
        _ok("'(Season 1-3)' is labelled as a season span, not episodes 01–03", span == ("S1–S3", None), f"{span}")
    finally:
        service.nyaa_search = original

    # absolute numbering: 'Example Series - 15' IS season-2 episode 3 when the
    # title's offset is 12 — and only a query for '15' can surface it.
    from ..match import service as match_service
    corpus = [
        ("[SubsPlease] Example Series - 15 (1080p) [AAAAAAAA].mkv", 50, True),   # S2 ep 3, absolute
        ("[SubsPlease] Example Series - 03 (1080p) [BBBBBBBB].mkv", 40, True),   # S1 ep 3
        ("[Other] Example Series S2 - 03 (1080p) [CCCCCCCC].mkv", 4, False),     # S2 ep 3, relative
    ]
    original_offsets = match_service.episode_offsets_for
    service.nyaa_search = _fake_nyaa(corpus)
    match_service.episode_offsets_for = lambda _aid: [12]
    try:
        got = [r.title for r, _p in service._episode_release_candidates(999, "Example Series 2nd Season", 3)]
        _ok("absolute-numbered '- 15' found for S2 ep 3", any("[AAAAAAAA]" in t for t in got), f"{got}")
        _ok("relative 'S2 - 03' still found", any("[CCCCCCCC]" in t for t in got))
        _ok("season-1 '- 03' rejected", not any("[BBBBBBBB]" in t for t in got))
    finally:
        service.nyaa_search = original
        match_service.episode_offsets_for = original_offsets


def _scratch_db_ready() -> bool:
    """True when MIMI_LAB_DB points at a scratch database (schema created here).
    Never touch the live DB from a selftest (a selftest once wiped live creds)."""
    import os
    from pathlib import Path
    from ..config import settings
    from ..db import init_db

    root = Path(__file__).resolve().parent.parent.parent
    live = (root / "data" / "mimi_lab.db").resolve()
    if not os.environ.get("MIMI_LAB_DB") or Path(settings.mimi_lab_db).resolve() == live:
        return False
    init_db()
    return True


def test_download_row_guards() -> None:
    """Scratch-DB: two rows must never fight over one torrent, and 99.9% is not done.

    The same release added twice (a second click while the first was importing)
    made two rows share ONE Transmission torrent: cancelling the duplicate deleted
    the data under the running import, and the two imports raced (the first pruned
    the source under the second). And a season pack declared complete at 0.999
    still had every file as 'name.part' — nothing was imported."""
    print("\n# download rows: duplicate / shared-torrent guards + completion (scratch DB)")
    if not _scratch_db_ready():
        print("  [SKIP] set MIMI_LAB_DB to a scratch path to run the DB-backed checks")
        return
    from ..db import cursor

    calls: list = []
    orig = (service.qbt_available, service.remove_torrent, service.torrent_state)
    service.qbt_available = lambda: False  # add_download: no Transmission push
    service.remove_torrent = lambda h, delete_data=True: calls.append((h, delete_data)) or {"ok": True}
    try:
        with cursor() as cx:
            cx.execute("DELETE FROM downloads")
            cx.execute("DELETE FROM jobs")
            cx.execute("INSERT INTO downloads(id,nyaa_id,title_guess,magnet,qbt_hash,state,save_path) "
                       "VALUES(1,'X','rel','magnet:?xt=urn:btih:aaaa','h1','completed','/inbox/rel.mkv')")
        rel = service.NyaaResult(nyaa_id="X", title="rel", magnet="magnet:?xt=urn:btih:aaaa")
        dl = service.add_download(rel)
        _ok("re-download while the import is in flight reuses the row", dl.id == 1, f"got id {dl.id}")
        with cursor() as cx:
            cx.execute("UPDATE downloads SET updated_at=datetime('now','-3 hours') WHERE id=1")
        dl2 = service.add_download(rel)
        _ok("a row stranded in 'completed' for 3 h no longer blocks", dl2.id != 1, f"got id {dl2.id}")

        with cursor() as cx:
            cx.execute("UPDATE downloads SET updated_at=datetime('now') WHERE id=1")
            cx.execute("UPDATE downloads SET qbt_hash='h1', state='downloading' WHERE id=?", (dl2.id,))
        r = service.cancel_download(dl2.id)
        _ok("cancelling the duplicate leaves the shared torrent (and its data) alone",
            r["ok"] and not r["torrent_removed"] and "shared" in (r.get("reason") or "") and calls == [],
            f"{r} calls={calls}")

        with cursor() as cx:
            cx.execute("INSERT INTO downloads(id,qbt_hash,state) VALUES(3,'h1','downloading')")
        rr = service.release_torrent(1)
        _ok("release after import keeps a torrent another row still needs",
            rr["ok"] and "shared" in (rr.get("reason") or "") and calls == [], f"{rr}")
        with cursor() as cx:
            cx.execute("UPDATE downloads SET state='cancelled' WHERE id=3")
        service.release_torrent(1)
        _ok("release removes the torrent once unshared (data kept)", calls == [("h1", False)], f"{calls}")

        calls.clear()
        with cursor() as cx:
            cx.execute("INSERT INTO downloads(id,qbt_hash,state) VALUES(4,'h4','downloading')")
        r4 = service.cancel_download(4)
        _ok("cancelling a lone download removes its torrent WITH data",
            r4["torrent_removed"] and calls == [("h4", True)], f"{calls}")

        # completion: 99.9% is still downloading (files are 'name.part'); 100% is done
        service.qbt_available = lambda: True
        states = {"h5": {"found": True, "state": "downloading", "progress": 0.9995,
                         "save_path": "/inbox", "name": "n", "content_path": "/inbox/n"}}
        service.torrent_state = lambda h: states.get(h, {"found": False, "state": "unknown", "progress": 0.0})
        with cursor() as cx:
            cx.execute("INSERT INTO downloads(id,qbt_hash,state,save_path) VALUES(5,'h5','downloading','/inbox')")
        service.poll_downloads()
        with cursor() as cx:
            st = cx.execute("SELECT state FROM downloads WHERE id=5").fetchone()["state"]
            jobs = cx.execute("SELECT COUNT(*) FROM jobs WHERE type='postprocess'").fetchone()[0]
        _ok("99.95% done is NOT complete (no postprocess yet)", st == "downloading" and jobs == 0,
            f"state={st} jobs={jobs}")
        states["h5"]["progress"] = 1.0
        service.poll_downloads()
        with cursor() as cx:
            st = cx.execute("SELECT state FROM downloads WHERE id=5").fetchone()["state"]
            jobs = cx.execute("SELECT COUNT(*) FROM jobs WHERE type='postprocess'").fetchone()[0]
        _ok("100% is complete → postprocess enqueued", st == "completed" and jobs == 1,
            f"state={st} jobs={jobs}")
    finally:
        service.qbt_available, service.remove_torrent, service.torrent_state = orig
        with cursor() as cx:
            cx.execute("DELETE FROM downloads")
            cx.execute("DELETE FROM jobs")


def test_multiseason_span() -> None:
    """Deterministic: the season range a multi-season pack states."""
    print("\n# multi-season span (_multiseason_span)")
    cases = [
        ("[DB] Example Series (Season 1-3) [BD1080p] (Batch)", (1, 3)),
        ("[G] Example Series S1+S2+OVAs [BD]", (1, 2)),
        ("[G] Example Series (Season 01 + Season 02 + Directors Cut) [BD]", (1, 2)),
        ("[G] Example Series 1+2 Season [BD]", (1, 2)),
        ("[G] Example Series Complete Series [BD]", None),
        ("[G] Example Series (01-25) [1080p][Batch]", None),
        ("[G] Example Series S2 - 10 (1080p).mkv", None),
    ]
    for title, expected in cases:
        got = service._multiseason_span(title)
        _ok(f"span {expected}: {title[:44]}", got == expected, f"expected={expected} got={got}")


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
    test_title_season_ordinal()
    test_sequel_search_offline()
    test_multiseason_span()
    test_download_row_guards()
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
