"""Self-test for the media module.  Run:  .venv/bin/python -m app.media.selftest

Verifies (per build spec) against lib/TestShow/TestShow - S01E01.mp4 (h264/aac 30s):
  * probe -> codec h264, audio aac, duration ~= 30000 ms, 1280x720
  * needs_transcode: h264 -> False, hevc -> True
  * process_file -> a temp mp4 that exists, is non-empty, and is ffprobe-valid (h264+aac)
  * organize (dry, into a temp Library) -> the Jellyfin-style path
  * cleanup of all temp outputs
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import traceback
from pathlib import Path

from ..config import settings
from . import service

ROOT = Path(__file__).resolve().parent.parent.parent
TEST_MP4 = ROOT / "lib" / "TestShow" / "TestShow - S01E01.mp4"

_passes: list[str] = []
_fails: list[str] = []


def _ok(name: str, cond: bool, detail: str = "") -> None:
    (_passes if cond else _fails).append(f"{name}: {detail}")
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}{' — ' + detail if detail else ''}")


def test_probe() -> dict:
    print("\n# probe TestShow")
    info = service.probe(TEST_MP4)
    print("  probe ->", info)
    _ok("video codec is h264", (info.get("codec") or "").lower() == "h264", info.get("codec"))
    _ok("audio codec is aac", (info.get("audio_codec") or "").lower() == "aac", info.get("audio_codec"))
    dur = info.get("duration_ms") or 0
    _ok("duration ~= 30000 ms", abs(dur - 30000) <= 500, f"{dur} ms")
    _ok("width == 1280", info.get("width") == 1280, str(info.get("width")))
    _ok("height == 720", info.get("height") == 720, str(info.get("height")))
    _ok("container is mp4-family",
        "mp4" in (info.get("container") or "").lower(), info.get("container"))
    return info


def test_needs_transcode() -> None:
    print("\n# needs_transcode decisions")
    _ok("h264 -> remux (False)", service.needs_transcode("h264") is False, "h264")
    _ok("hevc -> transcode (True)", service.needs_transcode("hevc") is True, "hevc")
    _ok("h265 -> transcode (True)", service.needs_transcode("h265") is True, "h265")


def test_process_file(tmp: Path) -> Path:
    print("\n# process_file (h264 -> lossless remux)")
    out = tmp / "processed.mp4"
    res = service.process_file(TEST_MP4, out)
    print("  process_file ->", res)
    _ok("path_taken == remux (h264 source)", res.get("path_taken") == "remux", res.get("path_taken"))
    _ok("output exists", out.exists(), str(out))
    size = out.stat().st_size if out.exists() else 0
    _ok("output is non-empty", size > 0, f"{size} bytes")
    # ffprobe-valid + browser-playable (h264 video + aac audio)
    try:
        out_info = service.probe(out)
        _ok("output is ffprobe-valid", bool(out_info.get("codec")), str(out_info.get("codec")))
        _ok("output video is h264", (out_info.get("codec") or "").lower() == "h264",
            out_info.get("codec"))
        _ok("output audio is aac", (out_info.get("audio_codec") or "").lower() == "aac",
            out_info.get("audio_codec"))
        _ok("output duration preserved (~30000 ms)",
            abs((out_info.get("duration_ms") or 0) - 30000) <= 1000,
            f"{out_info.get('duration_ms')} ms")
    except Exception as e:
        _ok("output is ffprobe-valid", False, f"probe raised {e!r}")
    return out


def test_organize(processed: Path, tmp: Path) -> None:
    print("\n# organize (dry, into a temp Library)")
    # Redirect the library dir to a temp location so we don't touch the real one.
    orig_lib = settings.library_dir
    temp_lib = tmp / "Library"
    settings.library_dir = temp_lib
    try:
        final = service.organize(processed, anilist_id=99000001, ep_number=1,
                                 title_romaji="TestShow")
        print("  organize ->", final)
        expected = temp_lib / "TestShow" / "Season 01" / "TestShow - S01E01.mp4"
        _ok("final path is Jellyfin-style", Path(final) == expected,
            f"{final}")
        _ok("final file exists at destination", Path(final).exists(), str(final))
        _ok("season dir zero-padded (Season 01)",
            (temp_lib / "TestShow" / "Season 01").is_dir(), "Season 01")
        _ok("filename zero-padded (S01E01)", "S01E01" in Path(final).name, Path(final).name)
    finally:
        settings.library_dir = orig_lib


def test_safe_title() -> None:
    print("\n# safe_title sanitization")
    s = service.safe_title('TestShow / Example: "test" <bad>|chars?')
    print("  safe_title ->", repr(s))
    _ok("strips filesystem-illegal chars",
        not any(c in s for c in '<>:"/\\|?*'), s)


def _scratch_db_ready() -> bool:
    """True when MIMI_LAB_DB points at a scratch database (schema created here).
    Never touch the live DB from a selftest (a selftest once wiped live creds)."""
    import os
    from ..db import init_db

    live = (ROOT / "data" / "mimi_lab.db").resolve()
    if not os.environ.get("MIMI_LAB_DB") or Path(settings.mimi_lab_db).resolve() == live:
        return False
    init_db()
    return True


def test_partial_wait(tmp: Path) -> None:
    """Scratch-DB: a download whose files are still 'name.part' is not failed —
    the import waits and re-checks; a genuinely empty pack fails VISIBLY.

    Transmission renames an in-progress file only when every byte is in, and a
    torrent can read complete a beat before that (a pack declared done at 99.9%
    had all three wanted files still .part). The batch importer used to return
    quietly on 'no video files' and the pack was never imported."""
    print("\n# import waits on .part files; empty packs fail visibly (scratch DB)")
    d = tmp / "pack"
    d.mkdir()
    (d / "Example Series - 01.mkv.part").write_bytes(b"x")
    _ok("_has_partial_files: folder holding a .part", service._has_partial_files(d))
    _ok("_has_partial_files: the file path itself still .part",
        service._has_partial_files(d / "Example Series - 01.mkv"))
    empty = tmp / "empty"
    empty.mkdir()
    _ok("_has_partial_files: nothing in progress", not service._has_partial_files(empty))

    if not _scratch_db_ready():
        print("  [SKIP] set MIMI_LAB_DB to a scratch path to run the DB-backed checks")
        return
    from ..db import cursor

    with cursor() as cx:
        cx.execute("DELETE FROM downloads")
        cx.execute("DELETE FROM jobs")
        cx.execute("DELETE FROM events")
        cx.execute("INSERT OR REPLACE INTO titles(anilist_id, romaji, english, total_episodes) "
                   "VALUES(999, 'Example Series', 'Example Series', 3)")
        cx.execute("INSERT INTO downloads(id,kind,state,save_path,anilist_id,wanted_eps) "
                   "VALUES(7,'batch','completed',?,999,'[1, 2]')", (str(d),))
        cx.execute("INSERT INTO downloads(id,kind,state,save_path,anilist_id,wanted_eps) "
                   "VALUES(8,'batch','completed',?,999,'[1, 2]')", (str(empty),))
        cx.execute("INSERT INTO downloads(id,kind,state,save_path,anilist_id) "
                   "VALUES(9,'single','completed',?,999)", (str(d / "Example Series - 01.mkv"),))
        cx.execute("INSERT INTO downloads(id,kind,state,save_path,anilist_id) "
                   "VALUES(10,'single','completed',?,999)", (str(tmp / "nothing.mkv"),))
    try:
        res = service._postprocess_batch(7, str(d), 999, 0)
        with cursor() as cx:
            job = cx.execute("SELECT payload_json FROM jobs WHERE type='postprocess'").fetchone()
        _ok("pack with only .part files: waits + re-checks",
            res.get("ok") and res.get("waiting") and job is not None
            and '"pp_attempt": 1' in (job["payload_json"] if job else ""), f"{res} job={dict(job) if job else None}")

        res2 = service._postprocess_batch(8, str(empty), 999, 0)
        with cursor() as cx:
            st = cx.execute("SELECT state FROM downloads WHERE id=8").fetchone()["state"]
            ev = cx.execute("SELECT title FROM events WHERE title LIKE 'Season pack: nothing%'").fetchone()
        _ok("empty pack: fails visibly (pp_failed + warning event)",
            res2.get("error") and st == "pp_failed" and ev is not None, f"state={st} event={ev and ev['title']}")

        res3 = service.postprocess({"download_id": 9})
        _ok("single episode still .part: waits + re-checks", res3.get("ok") and res3.get("waiting"), f"{res3}")

        raised = None
        try:
            service.postprocess({"download_id": 10})
        except Exception as e:
            raised = e
        with cursor() as cx:
            st10 = cx.execute("SELECT state FROM downloads WHERE id=10").fetchone()["state"]
        _ok("single with nothing on disk: fails (pp_failed) as before",
            isinstance(raised, RuntimeError) and st10 == "pp_failed", f"{raised!r} state={st10}")

        with cursor() as cx:
            cx.execute("INSERT INTO downloads(id,kind,state,save_path,qbt_hash) VALUES(11,'single','completed',?, 'hh')",
                       (str(d / "x.mkv"),))
            cx.execute("INSERT INTO downloads(id,kind,state,save_path,qbt_hash) VALUES(12,'single','completed',?, 'hh')",
                       (str(d / "x.mkv"),))
        others = service._other_rows_needing_source(11, "hh", d / "x.mkv")
        _ok("source kept while another row's import still needs it", others == [12], f"{others}")
        with cursor() as cx:
            cx.execute("UPDATE downloads SET state='postprocessed' WHERE id=12")
        _ok("…and pruned once that import is done",
            service._other_rows_needing_source(11, "hh", d / "x.mkv") == [])
    finally:
        with cursor() as cx:
            cx.execute("DELETE FROM downloads")
            cx.execute("DELETE FROM jobs")
            cx.execute("DELETE FROM events")
            cx.execute("DELETE FROM titles WHERE anilist_id=999")


def main() -> int:
    print("=" * 64)
    print("MEDIA SELFTEST")
    print("=" * 64)
    if not TEST_MP4.exists():
        print(f"  [FAIL] test asset missing: {TEST_MP4}")
        return 1

    tmp = Path(tempfile.mkdtemp(prefix="migaku_media_selftest_"))
    try:
        test_probe()
        test_needs_transcode()
        test_safe_title()
        test_partial_wait(tmp)
        processed = test_process_file(tmp)
        test_organize(processed, tmp)
    except Exception as e:
        print(f"  [FAIL] unexpected exception: {e!r}")
        traceback.print_exc()
        _fails.append(f"unexpected: {e!r}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        print(f"\n  (cleaned up temp dir {tmp})")

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
