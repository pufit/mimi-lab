"""Clips — one small mp4 + poster per card under `<clips_dir>/srs/<card_id>/` (§6).

One cue per clip by default (amendments §A.5): the target line ± padding,
clamped against the *time-adjacent* dialogue neighbours (never `idx±1` — 2,276
consecutive cue pairs overlap in time and 31 are out of order). A moment whose
meaning depends on neighbouring lines (`extend_json`, role `evidence`) covers
those too, up to `EXTENDED_MAX_CLIP_MS`; the poster is always the frame at the
TARGET line start (§6.2 "Evidence lines"). Sources are 119 h264 + 8 AV1
10-bit files, so a stream copy is impossible: every clip is re-encoded at
≤ 720p, `libx264 -preset veryfast -crf 23 -tune animation`, ~0.7 MB per card
(amendments §D, disk is tight).

Everything is version-guarded (§6.6): the payload carries `clip_version`, the
scratch directory is named after it, and the commit only happens when the row
still has that version — a job finishing after a swap or an undo is a no-op.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .constants import (
    BLEED_MS,
    CLIP_BYTES_WARN,
    CLIP_HEIGHT,
    CLIP_LEAD_MS,
    CLIP_TAIL_MS,
    DISK_LOW_BYTES,
    EXTENDED_MAX_CLIP_MS,
    MAX_CLIP_MS,
    MIN_LINE_MS,
    STALE_CLIP_MIN,
)

log = logging.getLogger("mimi_lab.srs.clips")

MIN_CLIP_BYTES = 10 * 1024
MIN_POSTER_BYTES = 1
DURATION_TOLERANCE_S = 0.25
AUDIT_ORPHAN_DAYS = 2


class DiskLow(RuntimeError):
    """Free space below `DISK_LOW_BYTES` — the clip job raises and is retried."""


class ClipError(RuntimeError):
    """ffmpeg failed / produced an unusable file (§5.13)."""


@dataclass
class ClipBuild:
    """Result of one clip extraction (§6.3/§6.4).

    `status` is what was written to `srs_cards.clip_status`, except `stale`,
    which means the card's `clip_version` moved on while this build ran: nothing
    was written and the scratch dir was removed (§6.6).
    """
    card_id: int
    version: int = 1
    status: str = "pending"          # pending | ready | failed | no_source | missing | stale
    start_ms: Optional[int] = None
    end_ms: Optional[int] = None
    bytes: Optional[int] = None
    video_path: Optional[str] = None
    poster_path: Optional[str] = None
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# paths & binaries
# ---------------------------------------------------------------------------

def _bin(name: str) -> str:
    return shutil.which(name) or f"/opt/homebrew/bin/{name}"


def card_dir(card_id: int) -> Path:
    from ..config import settings
    return Path(settings.clips_dir) / "srs" / str(int(card_id))


def _tmp_dir(card_id: int, version: int) -> Path:
    d = card_dir(card_id)
    return d.parent / f"{d.name}.v{int(version)}.tmp"


def _prev_dir(card_id: int) -> Path:
    d = card_dir(card_id)
    return d.parent / f"{d.name}.prev"


def _run(cmd: list[str], timeout: int = 90) -> tuple[int, str]:
    """`learn.service._run` semantics: never raises, returns `(rc, stderr tail)`."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
        return p.returncode, (p.stderr or "")[-2000:]
    except subprocess.TimeoutExpired:
        return 124, "timeout"
    except FileNotFoundError as e:
        return 127, str(e)


# ---------------------------------------------------------------------------
# window (§6.2)
# ---------------------------------------------------------------------------

@dataclass
class WindowPlan:
    """The window one clip covers (§6.2), with the extend lines it kept.

    `truncated` = at least one evidence line was dropped because the span
    exceeded `EXTENDED_MAX_CLIP_MS`; it is recorded as `extend_truncated` in
    `meta.json` (the card's `extend_json` then holds only the kept lines).
    """
    start_ms: int
    end_ms: int
    extend: list[dict]
    truncated: bool = False
    poster_ms: int = 0


def _raw_window(ls: int, le: int, extend: list[dict], prev: Optional[dict],
                nxt: Optional[dict]) -> tuple[int, int]:
    first = min([ls] + [int(x.get("start_ms") or 0) for x in extend])
    last = max([max(le, ls + MIN_LINE_MS)] + [int(x.get("end_ms") or 0) for x in extend])
    start = max(0, first - CLIP_LEAD_MS)
    end = last + CLIP_TAIL_MS
    if prev:
        pe = int(prev.get("end_ms") or 0)
        if start < pe - BLEED_MS:
            start = min(first - 100, max(0, pe - BLEED_MS))
    if nxt:
        ns = int(nxt.get("start_ms") or 0)
        if end > ns + BLEED_MS:
            end = max(last + 100, ns + BLEED_MS)
    return max(0, start), end


def plan_window(line: dict, extend: Optional[list[dict]], prev: Optional[dict],
                nxt: Optional[dict], duration_ms: Optional[int]) -> WindowPlan:
    """The clip window for one MOMENT (§6.2): the target cue plus every `extend`
    line (evidence before/after + the continuation cue), `CLIP_LEAD_MS`/
    `CLIP_TAIL_MS` padding, clamped against the time-adjacent dialogue
    neighbours that are NOT part of the moment, `MIN_LINE_MS` floor, the episode
    duration and the ceiling — `MAX_CLIP_MS` for a plain single cue,
    `EXTENDED_MAX_CLIP_MS` once the moment carries extend lines.

    When the span exceeds the ceiling the farthest evidence line is dropped and
    the window recomputed (`truncated=True`); continuation lines are part of the
    sentence and are never dropped.
    """
    from .snapshot import sort_extend

    ls = int(line.get("start_ms") or 0)
    le = int(line.get("end_ms") or 0)
    kept = sort_extend([x for x in (extend or []) if isinstance(x, dict)])
    truncated = False
    while True:
        cap = EXTENDED_MAX_CLIP_MS if kept else MAX_CLIP_MS
        start, end = _raw_window(ls, le, kept, prev, nxt)
        if end - start <= cap:
            break
        droppable = [i for i, x in enumerate(kept)
                     if (x.get("role") or "continuation") == "evidence"]
        if not droppable:
            break
        far = max(droppable, key=lambda i: abs(int(kept[i].get("start_ms") or 0) - ls))
        kept.pop(far)
        truncated = True

    cap = EXTENDED_MAX_CLIP_MS if kept else MAX_CLIP_MS
    end = min(end, start + cap)
    if duration_ms:
        end = min(end, int(duration_ms))
    if end <= start:
        end = start + MIN_LINE_MS
    # the poster is the frame at the TARGET line start (with evidence lines the
    # clip starts on another speaker, whose frame says nothing about the card)
    poster = min(max(int(ls), int(start)), max(int(start), int(end) - 100))
    return WindowPlan(start_ms=int(start), end_ms=int(end), extend=kept,
                      truncated=truncated, poster_ms=int(poster))


def window(line: dict, prev: Optional[dict], next: Optional[dict],  # noqa: A002
           duration_ms: Optional[int],
           extend: Optional[list[dict]] = None) -> tuple[int, int]:
    """`(start_ms, end_ms)` of `plan_window` — the §6.2 window, extend-aware."""
    p = plan_window(line, extend, prev, next, duration_ms)
    return p.start_ms, p.end_ms


# ---------------------------------------------------------------------------
# ffprobe helpers
# ---------------------------------------------------------------------------

def probe_audio_index(video: str) -> int:
    """Absolute stream index of the Japanese audio track, else the first audio
    stream, else 0 ("unsure" — the caller maps `0:a:0?`) (§6.3)."""
    rc, out = _probe([
        "-v", "error", "-select_streams", "a",
        "-show_entries", "stream=index:stream_tags=language", "-of", "json", video,
    ])
    if rc != 0 or not out:
        return 0
    try:
        streams = (json.loads(out) or {}).get("streams") or []
    except Exception:
        return 0
    first = 0
    for i, s in enumerate(streams):
        idx = int(s.get("index") or 0)
        if i == 0:
            first = idx
        lang = ((s.get("tags") or {}).get("language") or "").lower()
        if lang in ("jpn", "ja", "jap"):
            return idx
    return first


def _probe(args: list[str], timeout: int = 30) -> tuple[int, str]:
    try:
        p = subprocess.run([_bin("ffprobe"), *args], capture_output=True, text=True,
                           timeout=timeout, check=False)
        return p.returncode, p.stdout or ""
    except subprocess.TimeoutExpired:
        return 124, ""
    except FileNotFoundError:
        return 127, ""


def _probe_duration(path: Path) -> Optional[float]:
    rc, out = _probe(["-v", "error", "-show_entries", "format=duration",
                      "-of", "csv=p=0", str(path)])
    if rc != 0:
        return None
    try:
        return float((out or "").strip())
    except ValueError:
        return None


def _ffmpeg_version() -> str:
    rc, out = _probe(["-version"])
    if rc != 0 or not out:
        try:
            p = subprocess.run([_bin("ffmpeg"), "-version"], capture_output=True,
                               text=True, timeout=10, check=False)
            out = p.stdout or ""
        except Exception:
            return ""
    head = (out or "").splitlines()[0] if out else ""
    parts = head.split()
    return parts[2] if len(parts) > 2 else head[:40]


def source_available(episode_id: Optional[int]) -> bool:
    """True when the episode's video file exists on disk right now."""
    if not episode_id:
        return False
    from ..db import connect
    cx = connect()
    try:
        r = cx.execute("SELECT video_path FROM episodes WHERE id=?", (episode_id,)).fetchone()
    finally:
        cx.close()
    return bool(r and r["video_path"] and Path(r["video_path"]).exists())


# ---------------------------------------------------------------------------
# build (§6.3)
# ---------------------------------------------------------------------------

def _moment_span(card: dict, extend: list[dict]) -> tuple[int, int, set]:
    """`(first_start_ms, last_end_ms, included_line_ids)` of everything the
    moment covers — the target cue plus its extend lines (§6.2)."""
    ls = int(card.get("start_ms") or 0)
    le = int(card.get("end_ms") or 0)
    first = min([ls] + [int(x.get("start_ms") or 0) for x in extend])
    last = max([le] + [int(x.get("end_ms") or 0) for x in extend])
    ids = {int(x["line_id"]) for x in extend if x.get("line_id") is not None}
    if card.get("line_id") is not None:
        ids.add(int(card["line_id"]))
    return first, last, ids


def _neighbours(cx, card: dict,
                extend: Optional[list[dict]] = None) -> tuple[Optional[dict], Optional[dict]]:
    """Time-adjacent dialogue neighbours OUTSIDE the moment; falls back to the
    card's `context_json` snapshot when the line row is gone (deleted corpus) —
    §6.2. With evidence lines the moment spans several cues, so the clamps must
    be taken against the nearest dialogue line before/after that whole span, not
    around the target cue (whose neighbour may be an evidence line itself)."""
    from .snapshot import dialogue_neighbours

    ext = [x for x in (extend or []) if isinstance(x, dict)]
    first, last, included = _moment_span(card, ext)
    line_id = card.get("line_id")
    if line_id:
        row = cx.execute(
            "SELECT id, episode_id, idx, start_ms, end_ms, text FROM subtitle_lines WHERE id=?",
            (line_id,)).fetchone()
        if row:
            span = {"line_id": row["id"], "episode_id": row["episode_id"], "idx": row["idx"],
                    "start_ms": min(first, int(row["start_ms"] or 0)),
                    "end_ms": max(last, int(row["end_ms"] or 0)), "text": row["text"]}
            # n=4: the nearest candidates may be evidence lines or the other
            # half of a two-line cue (same timings — on screen WITH the moment,
            # so never a clamp; 2026-09-03)
            before, after = dialogue_neighbours(cx, span, n=4)
            prev = next((c for c in reversed(before)
                         if int(c.get("line_id") or 0) not in included
                         and int(c.get("end_ms") or 0) <= first + BLEED_MS), None)
            nxt = next((c for c in after
                        if int(c.get("line_id") or 0) not in included
                        and int(c.get("start_ms") or 0) >= last - BLEED_MS), None)
            return prev, nxt
    ctx = card.get("context_json")
    if ctx:
        try:
            lines = json.loads(ctx) or []
        except Exception:
            lines = []
        prev = None
        nxt = None
        for c in lines:
            if c.get("is_target"):
                continue
            if c.get("line_id") is not None and int(c["line_id"]) in included:
                continue
            if int(c.get("end_ms") or 0) <= first:
                if prev is None or int(c.get("end_ms") or 0) > int(prev.get("end_ms") or 0):
                    prev = c
            elif int(c.get("start_ms") or 0) >= last:
                if nxt is None or int(c.get("start_ms") or 0) < int(nxt.get("start_ms") or 0):
                    nxt = c
        return prev, nxt
    return None, None


def _free_bytes() -> int:
    from ..config import settings
    d = Path(settings.clips_dir)
    d.mkdir(parents=True, exist_ok=True)
    return shutil.disk_usage(str(d)).free


def build(card: dict, *, keep_prev: bool = False) -> ClipBuild:
    """Cut the clip + poster into `<dir>.v<version>.tmp/` and commit it only when
    `srs_cards.clip_version` still matches (§6.6). `keep_prev=True` rotates the
    previous directory to `<dir>.prev/` so `restore_prev()` can put it back.

    Raises `DiskLow` when free space is below `DISK_LOW_BYTES` and `ClipError`
    when ffmpeg fails or writes an unusable file; a missing source video is
    reported as `status='no_source'` without raising (§9.1).
    """
    from ..db import connect, cursor, kv_get, kv_set

    card_id = int(card["id"])
    version = int(card.get("clip_version") or 1)
    out = ClipBuild(card_id=card_id, version=version)

    cx = connect()
    try:
        ep = None
        if card.get("episode_id"):
            ep = cx.execute(
                "SELECT id, video_path, duration_ms, codec FROM episodes WHERE id=?",
                (card["episode_id"],)).fetchone()
        video = (ep["video_path"] if ep else None) or ""
        if not video or not Path(video).exists():
            out.status = "no_source"
            return out
        if _free_bytes() < DISK_LOW_BYTES:
            raise DiskLow(f"less than {DISK_LOW_BYTES // 2**30} GiB free — clip deferred")

        extend = [x for x in (_json_or_none(card.get("extend_json")) or []) if isinstance(x, dict)]
        prev, nxt = _neighbours(cx, card, extend)
        line = {"start_ms": int(card.get("start_ms") or 0), "end_ms": int(card.get("end_ms") or 0)}
        plan = plan_window(line, extend, prev, nxt, ep["duration_ms"] if ep else None)
        start_ms, end_ms = plan.start_ms, plan.end_ms
        out.start_ms, out.end_ms = start_ms, end_ms

        tmp = _tmp_dir(card_id, version)
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True, exist_ok=True)

        start_s = start_ms / 1000.0
        dur_s = max(0.2, (end_ms - start_ms) / 1000.0)
        # the poster is the frame at the TARGET line start (§6.2): with evidence
        # lines the clip opens on another speaker.
        poster_s = plan.poster_ms / 1000.0
        audio_idx = probe_audio_index(video)
        audio_map = f"0:{audio_idx}" if audio_idx else "0:a:0?"

        clip_path = tmp / "clip.mp4"
        poster_path = tmp / "poster.jpg"
        clip_args = [
            "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-ss", f"{start_s:.3f}", "-t", f"{dur_s:.3f}", "-i", video,
            "-map", "0:v:0", "-map", audio_map, "-sn", "-dn",
            "-map_metadata", "-1", "-map_chapters", "-1",
            "-vf", f"scale=-2:'min({CLIP_HEIGHT},ih)':flags=bicubic,format=yuv420p",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-tune", "animation",
            "-profile:v", "high", "-level", "4.1",
            "-c:a", "aac", "-b:a", "128k", "-ac", "2", "-ar", "48000",
            "-af", "aresample=async=1:first_pts=0",
            "-movflags", "+faststart", "-avoid_negative_ts", "make_zero",
            str(clip_path),
        ]
        rc, err = _run([_bin("ffmpeg"), *clip_args])
        if rc != 0 or not clip_path.exists() or clip_path.stat().st_size < MIN_CLIP_BYTES:
            shutil.rmtree(tmp, ignore_errors=True)
            raise ClipError(f"ffmpeg clip rc={rc}: {err[-400:]}")

        poster_args = [
            "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-ss", f"{poster_s:.3f}", "-i", video, "-frames:v", "1",
            "-vf", f"scale=-2:'min({CLIP_HEIGHT},ih)'", "-q:v", "4", str(poster_path),
        ]
        prc, perr = _run([_bin("ffmpeg"), *poster_args])
        if prc != 0 or not poster_path.exists() or poster_path.stat().st_size <= MIN_POSTER_BYTES:
            shutil.rmtree(tmp, ignore_errors=True)
            raise ClipError(f"ffmpeg poster rc={prc}: {perr[-400:]}")

        actual = _probe_duration(clip_path)
        if actual is not None and abs(actual - dur_s) > DURATION_TOLERANCE_S:
            log.warning("clip %s duration %.3f vs requested %.3f", card_id, actual, dur_s)

        meta = {
            "card_id": card_id,
            "lemma": card.get("lemma"),
            "reading": card.get("reading"),
            "meaning_short": card.get("meaning_short"),
            "line_id": card.get("line_id"),
            "episode_id": card.get("episode_id"),
            "anilist_id": card.get("anilist_id"),
            "show": card.get("show_title"),
            "ep_number": card.get("ep_number"),
            "source_video": video,
            "codec": ep["codec"] if ep else None,
            "line": {
                "start_ms": int(card.get("start_ms") or 0),
                "end_ms": int(card.get("end_ms") or 0),
                "text": card.get("text") or "",
                "translation": card.get("translation"),
                "translation_source": card.get("translation_source"),
                "target_surface": card.get("target_surface") or "",
            },
            "window": {
                "start_ms": start_ms, "end_ms": end_ms,
                "poster_ms": plan.poster_ms,
                "prev": ({"line_id": prev.get("line_id"), "end_ms": prev.get("end_ms")}
                         if prev else None),
                "next": ({"line_id": nxt.get("line_id"), "start_ms": nxt.get("start_ms")}
                         if nxt else None),
                "extend_truncated": bool(plan.truncated),
            },
            # every extra line the moment covers (evidence + continuation), in
            # playback order; `extend_truncated` above says whether the cap
            # dropped one (§6.2 "Evidence lines")
            "extend": plan.extend,
            "extend_truncated": bool(plan.truncated),
            "context": _json_or_none(card.get("context_json")),
            "ffmpeg": _ffmpeg_version(),
            "ffmpeg_args": clip_args,
            "clip_version": version,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        (tmp / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1))

        total = sum(p.stat().st_size for p in tmp.iterdir() if p.is_file())
        out.bytes = int(total)

        # --- version-guarded commit -----------------------------------------
        with cursor() as wx:
            n = wx.execute(
                "UPDATE srs_cards SET clip_status='ready', clip_bytes=?, clip_start_ms=?, "
                "clip_end_ms=?, clip_error=NULL, updated_at=datetime('now') "
                "WHERE id=? AND clip_version=?",
                (int(total), start_ms, end_ms, card_id, version)).rowcount
        if n != 1:
            shutil.rmtree(tmp, ignore_errors=True)
            out.status = "stale"
            return out

        dest = card_dir(card_id)
        dest.parent.mkdir(parents=True, exist_ok=True)
        old_bytes = 0
        if dest.exists():
            old_bytes = sum(p.stat().st_size for p in dest.iterdir() if p.is_file())
            prev_d = _prev_dir(card_id)
            if keep_prev:
                # rotate: this build's own predecessor becomes the kept copy
                if prev_d.exists():
                    shutil.rmtree(prev_d, ignore_errors=True)
                os.replace(dest, prev_d)
            else:
                # A `.prev` left by a demotion moment swap belongs to the *undo*
                # path, not to us: the §9.1 `srs_clip {card_id, v}` payload carries
                # no keep_prev flag, so the re-cut after a swap always arrives here.
                # Leave it alone (§6.4 "kept until the new clip is ready"); the
                # weekly audit reaps `.prev` dirs older than 2 days.
                shutil.rmtree(dest, ignore_errors=True)
        os.replace(tmp, dest)

        try:
            cur = int(kv_get("srs.clips.bytes") or 0)
            kv_set("srs.clips.bytes", str(max(0, cur - old_bytes + int(total))))
        except Exception as e:
            log.debug("clip byte accounting failed: %s", e)

        out.status = "ready"
        out.video_path = str(dest / "clip.mp4")
        out.poster_path = str(dest / "poster.jpg")
        return out
    finally:
        cx.close()


def _json_or_none(raw):
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# URLs, restore, remove (§6.4/§6.6)
# ---------------------------------------------------------------------------

def urls(card: dict) -> dict:
    """`{"video_url", "poster_url"}` under `/clips/srs/<id>/…?v=<clip_version>`,
    or `None`s when the status is not `ready` or a file is missing (§6.4)."""
    out = {"video_url": None, "poster_url": None}
    try:
        card_id = int(card.get("id"))
    except (TypeError, ValueError):
        return out
    if (card.get("clip_status") or "") != "ready":
        return out
    d = card_dir(card_id)
    clip = d / "clip.mp4"
    poster = d / "poster.jpg"
    if not clip.exists() or not poster.exists():
        return out
    v = int(card.get("clip_version") or 1)
    out["video_url"] = f"/clips/srs/{card_id}/clip.mp4?v={v}"
    out["poster_url"] = f"/clips/srs/{card_id}/poster.jpg?v={v}"
    return out


def restore_prev(card_id: int) -> bool:
    """Put the kept previous clip back (undo of a demotion swap). True when a
    `.prev` directory existed."""
    prev = _prev_dir(card_id)
    if not prev.exists():
        return False
    dest = card_dir(card_id)
    trash = dest.parent / f"{dest.name}.undo.tmp"
    shutil.rmtree(trash, ignore_errors=True)
    if dest.exists():
        os.replace(dest, trash)
    os.replace(prev, dest)
    shutil.rmtree(trash, ignore_errors=True)
    return True


def remove(card_id: int) -> None:
    """Delete `<clips_dir>/srs/<card_id>/` and its scratch/`.prev` siblings."""
    d = card_dir(card_id)
    shutil.rmtree(d, ignore_errors=True)
    parent = d.parent
    if not parent.exists():
        return
    for p in parent.iterdir():
        if p.name.startswith(f"{d.name}.") and p.is_dir():
            shutil.rmtree(p, ignore_errors=True)


# ---------------------------------------------------------------------------
# stale pending re-enqueue (§6.6) — called by service.periodic_tick
# ---------------------------------------------------------------------------

def _clip_job_live(cx, card_id: int, version: int) -> bool:
    payload = json.dumps({"card_id": int(card_id), "v": int(version)}, sort_keys=True)
    r = cx.execute(
        "SELECT 1 FROM jobs WHERE type='srs_clip' AND payload_json=? "
        "AND state IN ('queued','running') LIMIT 1", (payload,)).fetchone()
    return r is not None


def requeue_stale_pending(cx) -> int:
    """Re-enqueue `srs_clip` for cards stuck in `pending` for more than
    `STALE_CLIP_MIN` minutes with no live job; returns how many (§6.6)."""
    from ..jobs.service import enqueue

    rows = cx.execute(
        "SELECT id, clip_version FROM srs_cards WHERE clip_status='pending' "
        "AND clip_requested_at IS NOT NULL "
        f"AND clip_requested_at <= datetime('now', '-{int(STALE_CLIP_MIN)} minutes')"
    ).fetchall()
    n = 0
    for r in rows:
        v = int(r["clip_version"] or 1)
        if _clip_job_live(cx, r["id"], v):
            continue
        enqueue("srs_clip", {"card_id": int(r["id"]), "v": v}, priority=30)
        n += 1
    if n:
        log.info("re-enqueued %d stale pending clips", n)
    return n


# ---------------------------------------------------------------------------
# jobs (§9.1)
# ---------------------------------------------------------------------------

_CARD_COLS = (
    "id, lemma, reading, meaning_short, line_id, episode_id, anilist_id, show_title, ep_number, "
    "start_ms, end_ms, text, translation, translation_source, target_surface, context_json, "
    "extend_json, clip_status, clip_version"
)


def _publish(card_id: int) -> None:
    try:
        from ..events import bus
        bus.publish("srs", {"what": "clip", "card_id": int(card_id)})
    except Exception as e:
        log.debug("bus publish failed: %s", e)


def _job_clip(payload: dict) -> None:
    """`srs_clip {card_id, v}` — exits silently when `v != clip_version`. Raises
    on ffmpeg failure / a tiny output / low disk; a missing source video is
    recorded as `no_source` and does NOT raise (§9.1)."""
    from ..db import connect, cursor

    card_id = int(payload.get("card_id") or 0)
    want_v = int(payload.get("v") or 0)
    if not card_id:
        raise ValueError("srs_clip payload without card_id")

    cx = connect()
    try:
        row = cx.execute(f"SELECT {_CARD_COLS} FROM srs_cards WHERE id=?", (card_id,)).fetchone()
    finally:
        cx.close()
    if row is None:
        log.info("srs_clip: card %s is gone — nothing to cut", card_id)
        return
    card = dict(row)
    version = int(card.get("clip_version") or 1)
    if want_v and want_v != version:
        log.info("srs_clip: card %s payload v=%s != clip_version %s — superseded",
                 card_id, want_v, version)
        return

    try:
        res = build(card, keep_prev=False)
    except DiskLow as e:
        with cursor() as wx:
            wx.execute("UPDATE srs_cards SET clip_error=?, updated_at=datetime('now') "
                       "WHERE id=? AND clip_version=?", (str(e)[:400], card_id, version))
        raise
    except ClipError as e:
        with cursor() as wx:
            n = wx.execute(
                "UPDATE srs_cards SET clip_status='failed', clip_error=?, "
                "updated_at=datetime('now') WHERE id=? AND clip_version=?",
                (str(e)[:400], card_id, version)).rowcount
        if n == 1:
            _publish(card_id)
        raise

    if res.status == "no_source":
        with cursor() as wx:
            n = wx.execute(
                "UPDATE srs_cards SET clip_status='no_source', clip_error=NULL, "
                "updated_at=datetime('now') WHERE id=? AND clip_version=?",
                (card_id, version)).rowcount
        if n == 1:
            _publish(card_id)
        return
    if res.status == "ready":
        _publish(card_id)


def _job_clip_audit(payload: dict) -> None:
    """`srs_clip_audit {}` — weekly (§6.6): verify files, re-enqueue what is
    missing or has a source again, prune orphan dirs and orphan moments, refresh
    `kv srs.clips.bytes`. Best-effort: logs, never raises."""
    from ..config import settings
    from ..db import connect, cursor, kv_set
    from ..jobs.service import enqueue

    try:
        root = Path(settings.clips_dir) / "srs"
        root.mkdir(parents=True, exist_ok=True)
        cx = connect()
        try:
            cards = cx.execute(
                "SELECT id, episode_id, clip_status, clip_version FROM srs_cards"
            ).fetchall()
            ep_paths = {r["id"]: (r["video_path"] or "")
                        for r in cx.execute("SELECT id, video_path FROM episodes")}
        finally:
            cx.close()

        alive = {int(r["id"]) for r in cards}
        missing: list[tuple[int, int]] = []
        back: list[tuple[int, int]] = []
        for r in cards:
            cid, ver = int(r["id"]), int(r["clip_version"] or 1)
            status = r["clip_status"] or ""
            d = card_dir(cid)
            if status == "ready":
                if not (d / "clip.mp4").exists() or not (d / "poster.jpg").exists():
                    missing.append((cid, ver))
            elif status == "no_source":
                p = ep_paths.get(r["episode_id"] or -1) or ""
                if p and Path(p).exists():
                    back.append((cid, ver))

        with cursor() as wx:
            for cid, ver in missing:
                wx.execute("UPDATE srs_cards SET clip_status='missing', "
                           "updated_at=datetime('now') WHERE id=? AND clip_version=?", (cid, ver))
            # orphan moments of deleted episodes
            wx.execute("DELETE FROM srs_moments WHERE episode_id NOT IN "
                       "(SELECT id FROM episodes)")

        for cid, ver in missing + back:
            src_ok = True
            if (cid, ver) in missing:
                cx2 = connect()
                try:
                    r2 = cx2.execute(
                        "SELECT e.video_path FROM srs_cards c LEFT JOIN episodes e "
                        "ON e.id = c.episode_id WHERE c.id=?", (cid,)).fetchone()
                finally:
                    cx2.close()
                src_ok = bool(r2 and r2["video_path"] and Path(r2["video_path"]).exists())
            if src_ok:
                enqueue("srs_clip", {"card_id": cid, "v": ver}, priority=30)

        # orphan directories (backup-restore leftovers) and stale scratch dirs
        cutoff = time.time() - AUDIT_ORPHAN_DAYS * 86400
        total = 0
        for p in root.iterdir():
            if not p.is_dir():
                continue
            name = p.name
            stale_kind = name.endswith(".tmp") or name.endswith(".prev") or ".v" in name
            base = name.split(".", 1)[0]
            try:
                cid = int(base)
            except ValueError:
                cid = -1
            old = p.stat().st_mtime < cutoff
            if stale_kind and old:
                shutil.rmtree(p, ignore_errors=True)
                continue
            if not stale_kind and cid not in alive and old:
                shutil.rmtree(p, ignore_errors=True)
                continue
            if not stale_kind and cid in alive:
                total += sum(f.stat().st_size for f in p.iterdir() if f.is_file())
        kv_set("srs.clips.bytes", str(int(total)))
        if total > CLIP_BYTES_WARN:
            try:
                from ..events import service as events
                events.record("srs", f"SRS clips use {total / 2**30:.1f} GB", kind="warning")
            except Exception as e:
                log.debug("clip size event failed: %s", e)
        log.info("clip audit: %d missing, %d re-armed, %.1f MB on disk",
                 len(missing), len(back), total / 2**20)
    except Exception as e:                       # best-effort by contract (§9.1)
        log.warning("srs_clip_audit failed: %s", e)


def register_jobs() -> None:
    """Register the real clip handlers: `srs_clip` and `srs_clip_audit` (§9.1)."""
    from ..jobs.service import register
    register("srs_clip", _job_clip)
    register("srs_clip_audit", _job_clip_audit)
