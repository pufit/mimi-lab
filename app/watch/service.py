"""One-click Play, relayed to the user-side Connector (notes/REMOTE_PLAYBACK_DESIGN.md).

The server no longer drives a co-located Migaku. `play_episode`/`play_moment`
resolve the episode's media to **URLs on this server** (with short-lived signed
tokens), then push a `{cmd:"play", …}` over the Connector's outbound WebSocket.
Migaku — inside the Connector's Chrome — fetches the bytes itself and plays +
tokenizes. No media passes through the Connector; nothing plays on the server.

This module also receives the Connector's playback telemetry — the closed loop:
watch → progress recorded → auto-marked watched at 90% → MAL push → (Connector
re-syncs known words at session end) → comprehension re-rank. Watching an
episode now updates everything with zero clicks.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from ..config import settings
from ..connector.service import registry
from ..db import connect, cursor
from ..media.streaming import resolve_episode_media
from ..security import make_media_token

log = logging.getLogger("mimi_lab.watch")

# an episode is "watched" once you've seen this share of it
WATCHED_AT = 0.90
# ignore telemetry until a video has a sane duration (player still loading)
MIN_DURATION_MS = 60_000

# ── rewatch session state (in-memory; resets on restart, which is fine) ──
# A rewatch is logged when an ALREADY-watched episode crosses WATCHED_AT —
# but only if this viewing session actually started early in the episode
# (min position seen <= REWATCH_MIN_START). Without that, Moments'
# "play from here" near the end would instantly log a phantom rewatch.
# Sessions are keyed per (device, episode): two Connector devices watching
# concurrently must not contaminate each other's min-position tracking.
REWATCH_MIN_START = 0.50
REWATCH_DEDUP_S = 3 * 3600  # one logged rewatch per episode per 3h window
_session_min_pos: dict[tuple[Optional[str], int], float] = {}  # (device_id, episode_id) -> min ratio
_last_rewatch_log: dict[int, float] = {}  # episode_id -> unix time of last log (global on purpose)


def _public_base(request_base: Optional[str]) -> str:
    """The base URL the Connector (and Migaku) reaches us at.

    Prefer the configured public URL (required for a remote Connector over TLS);
    fall back to the incoming request's base (fine on the same LAN/loopback).
    """
    return (settings.server_public_url or request_base or "").rstrip("/")


def _media_url(base: str, episode_id: int, scope: str) -> str:
    token = make_media_token(episode_id, scope)
    return f"{base}/api/media/episode/{episode_id}/{scope}?token={token}"


async def play_episode(
    episode_id: int,
    seek_ms: Optional[int] = None,
    request_base: Optional[str] = None,
    device_id: Optional[str] = None,
) -> dict:
    """Relay a play command for `episode_id` to a connected Connector.

    `device_id` targets a specific watching machine; None routes to the default
    device (the single connected one, else Migaku-ready / most recently used).

    Raises FileNotFoundError if the episode has no video on disk, or
    ConnectorOffline / ConnectorTimeout (from the registry) if no matching
    Connector is connected / it doesn't ack.
    """
    video, sub, sub2 = resolve_episode_media(episode_id)
    if not video or not os.path.exists(video):
        raise FileNotFoundError(f"episode {episode_id} has no playable video on disk")

    # Resume: no explicit seek + we have recorded progress in the resumable
    # window (>30s in, <90% through) → continue where you left off.
    if seek_ms is None:
        try:
            with connect() as cx:
                row = cx.execute(
                    "SELECT watch_progress_ms, duration_ms, watched FROM episodes WHERE id=?",
                    (episode_id,),
                ).fetchone()
            if row and not row["watched"] and row["watch_progress_ms"]:
                pos, dur = int(row["watch_progress_ms"]), row["duration_ms"]
                if pos > 30_000 and (not dur or pos / dur < WATCHED_AT):
                    seek_ms = pos
        except Exception:
            pass

    base = _public_base(request_base)
    if not base:
        raise RuntimeError(
            "no public base URL: set SERVER_PUBLIC_URL (the URL the Connector reaches this server at)"
        )

    cmd = {
        "cmd": "play",
        "episode_id": episode_id,
        "video_url": _media_url(base, episode_id, "video"),
        "sub_url": _media_url(base, episode_id, "subtitle") if sub else None,
        # English secondary/reference track (Migaku shows it when you enable
        # "secondary subtitles" in its settings). None when no English exists.
        "sub2_url": _media_url(base, episode_id, "subtitle2") if sub2 else None,
        "seek_ms": int(seek_ms) if seek_ms is not None else None,
    }
    # Resolve the target device up-front so the play, the session-state reset,
    # and the response all name the same machine (raises ConnectorOffline here
    # when nothing matches — same 409 the UI already understands).
    dev = registry.resolve_device(device_id)
    # a new play = a new viewing session for rewatch tracking (a stale min-pos
    # from a previous session could otherwise fake a full rewatch)
    _session_min_pos.pop((dev.device_id, episode_id), None)

    # Generous ack budget: the Connector streams the WHOLE episode into Migaku
    # before acking; large files over a LAN can legitimately take minutes.
    # Download
    # progress streams to the UI separately (SSE 'play-progress').
    ack = await registry.send_command(cmd, timeout=300.0, device_id=dev.device_id)
    return {
        "ok": True,
        "episode_id": episode_id,
        "relayed": True,
        "seek_ms": cmd["seek_ms"],
        "device_id": dev.device_id,
        "device_name": dev.device_name,
        "verify": ack.get("verify"),
    }


async def play_moment(
    line_id: int, request_base: Optional[str] = None, device_id: Optional[str] = None
) -> dict:
    cx = connect()
    try:
        row = cx.execute(
            "SELECT episode_id, start_ms FROM subtitle_lines WHERE id=?", (line_id,)
        ).fetchone()
    finally:
        cx.close()
    if not row:
        raise ValueError(f"subtitle line {line_id} not found")
    return await play_episode(
        row["episode_id"], seek_ms=row["start_ms"], request_base=request_base, device_id=device_id
    )


def connector_status() -> dict:
    """Status of the user-side Connector (mirrors GET /api/connector/status)."""
    return registry.status()


# ---------------------------------------------------------------------------
# playback telemetry (pushed by the Connector every ~15s while playing)
# ---------------------------------------------------------------------------
def record_telemetry(msg: dict, source: str = "connector") -> None:
    """Persist watch progress; auto-mark watched at WATCHED_AT; log rewatches.

    Called from the Connector WS loop (source='connector'; the registry stamps
    the sending device's `device_id` into msg) and the in-browser fallback
    player (source='browser', no device_id). First watch: flips episodes.watched
    (which also logs the watch_history row + pushes MAL). Rewatch: an
    already-watched episode crossing WATCHED_AT in a session that started
    early enough gets its own watch_history row — that's what makes rewatches
    count in Stats instead of vanishing into the boolean. Session min-position
    is tracked per (device, episode) so simultaneous devices don't contaminate
    each other.
    """
    import time as _time

    episode_id = msg.get("episode_id")
    pos = msg.get("position_ms")
    dur = msg.get("duration_ms")
    device_id = msg.get("device_id")
    if episode_id is None or pos is None:
        return
    episode_id, pos = int(episode_id), max(0, int(pos))
    session_key = (device_id, episode_id)

    with cursor() as cx:
        row = cx.execute(
            "SELECT watched, anilist_id, duration_ms FROM episodes WHERE id=?",
            (episode_id,),
        ).fetchone()
        if not row:
            return
        cx.execute(
            "UPDATE episodes SET watch_progress_ms=?, updated_at=datetime('now') WHERE id=?",
            (pos, episode_id),
        )
    duration = dur or row["duration_ms"]
    ratio = (pos / duration) if (duration and duration >= MIN_DURATION_MS) else None

    if ratio is not None:
        prev_min = _session_min_pos.get(session_key)
        _session_min_pos[session_key] = ratio if prev_min is None else min(prev_min, ratio)

        if not row["watched"] and ratio >= WATCHED_AT:
            _auto_mark_watched(episode_id, row["anilist_id"], source=source)
        elif (row["watched"] and ratio >= WATCHED_AT
              and _session_min_pos.get(session_key, 1.0) <= REWATCH_MIN_START
              and _time.time() - _last_rewatch_log.get(episode_id, 0) > REWATCH_DEDUP_S):
            _log_rewatch(episode_id, source)
            _last_rewatch_log[episode_id] = _time.time()

    # nudge the UI so the progress bar / continue-watching rail stays live
    try:
        from ..events import bus
        bus.publish("watch", {"episode_id": episode_id})
    except Exception:
        pass


def _log_rewatch(episode_id: int, source: str) -> None:
    log.info("episode %s rewatched (source=%s)", episode_id, source)
    try:
        with cursor() as cx:
            cx.execute(
                "INSERT INTO watch_history(episode_id, source, is_rewatch) VALUES(?,?,1)",
                (episode_id, source),
            )
    except Exception as e:
        log.warning("could not log rewatch for %s: %s", episode_id, e)
        return
    try:
        from ..events import service as events
        events.record(
            "watch", "Rewatch logged", "success",
            detail=f"Episode {episode_id} — counted in your immersion stats.",
            meta={"episode_id": episode_id},
        )
    except Exception:
        pass


def _auto_mark_watched(episode_id: int, anilist_id: Optional[int], source: str = "connector") -> None:
    log.info("episode %s crossed %.0f%% — auto-marking watched", episode_id, WATCHED_AT * 100)
    try:
        # catalog.set_watched also logs the watch_history row + enqueues the MAL push
        from ..catalog.service import set_watched
        set_watched(episode_id, True, source=source)
    except Exception as e:
        log.warning("auto-watched failed for %s: %s", episode_id, e)
        return
    try:
        from ..events import service as events
        events.record(
            "watch", "Marked watched automatically", "success",
            detail=f"Episode {episode_id} passed {int(WATCHED_AT * 100)}% — synced to MAL.",
            meta={"episode_id": episode_id, "anilist_id": anilist_id},
        )
    except Exception:
        pass


def record_session_end(msg: dict) -> None:
    """A viewing session ended (video finished / changed / player closed).

    The Connector re-syncs known words on its own right after sending this —
    words get mined DURING watching, so this is the moment the known-set moved.
    Also resets that device's rewatch session state for the episode
    (min-position tracking).
    """
    episode_id = msg.get("episode_id")
    device_id = msg.get("device_id")
    reason = msg.get("reason") or "ended"
    if episode_id is not None:
        _session_min_pos.pop((device_id, int(episode_id)), None)
    log.info(
        "viewing session ended for episode %s (%s)%s",
        episode_id, reason, f" on {device_id}" if device_id else "",
    )
