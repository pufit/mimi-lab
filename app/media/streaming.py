"""On-demand media streaming for the Connector topology
(notes/REMOTE_PLAYBACK_DESIGN.md §3/§5).

Episodes live on the server; Migaku (inside the user's Connector) `fetch()`es the
bytes from here when you press Play. These endpoints therefore need real **HTTP
Range** support — both for the P1 full-download blob (resumable / progress) and
for the P3 streaming `<video src=range-URL>` spike.

Shared resolver `resolve_episode_media()` is the single source of truth for
"which files back this episode" (used by the media routes AND the watch relay).
"""
from __future__ import annotations

import logging
import mimetypes
import os
from pathlib import Path
from typing import Iterator, Optional

from fastapi import Request
from fastapi.responses import Response, StreamingResponse

from ..db import connect

CHUNK = 1024 * 1024  # 1 MiB — Chrome backs large blobs on disk; this keeps RAM flat

log = logging.getLogger("mimi_lab.media.streaming")

# (path, mtime_ns) -> display-sane? Serve-time defense-in-depth for the OPTIONAL
# secondary track: acceptance-time gates can be bypassed by anything that writes
# the sidecar file out-of-band (for example, leaving a doubled `.en.srt` at the
# served path). Cached per file
# version so the structural check runs once, not per request.
_SANE_CACHE: dict[tuple[str, int], bool] = {}


def _secondary_track_ok(path: str, video: Optional[str]) -> bool:
    try:
        key = (path, os.stat(path).st_mtime_ns)
    except OSError:
        return False
    hit = _SANE_CACHE.get(key)
    if hit is None:
        from ..subs.service import sub_display_sane
        try:
            hit, why = sub_display_sane(path, video)
        except Exception:
            hit, why = True, None  # can't disprove -> don't block playback
        if not hit:
            log.warning("not serving secondary subtitle %s: %s", path, why)
        if len(_SANE_CACHE) > 4096:  # bounded; entries are tiny
            _SANE_CACHE.clear()
        _SANE_CACHE[key] = hit
    return hit


def resolve_episode_media(
    episode_id: int,
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Resolve an episode to (video_path, sub_primary, sub_secondary) on disk.

    * primary  = the Japanese study subtitle (lang != 'en'),
    * secondary = the English reference/translation track (lang = 'en').

    Each prefers the best aligned/newest `subtitles` row, then a sidecar next to
    the video. Any of the three may be None. The secondary track additionally
    passes a (cached) display-sanity gate — English is optional, so serving no
    track beats serving a structurally-broken one (stacked wrong lines).
    """
    cx = connect()
    try:
        ep = cx.execute(
            "SELECT id, video_path FROM episodes WHERE id=?", (episode_id,)
        ).fetchone()
        if not ep:
            return None, None, None
        video = ep["video_path"]

        def _best(lang_clause: str) -> Optional[str]:
            row = cx.execute(
                "SELECT path FROM subtitles WHERE episode_id=? AND path IS NOT NULL "
                f"AND {lang_clause} ORDER BY aligned DESC, version DESC, id DESC LIMIT 1",
                (episode_id,),
            ).fetchone()
            if row and row["path"] and os.path.exists(row["path"]):
                return row["path"]
            return None

        def _sidecar(exts: tuple[str, ...]) -> Optional[str]:
            if not video:
                return None
            base = os.path.splitext(video)[0]
            for ext in exts:
                if os.path.exists(base + ext):
                    return base + ext
            return None

        sub = _best("COALESCE(lang,'ja') <> 'en'") or _sidecar((".ja.srt", ".srt", ".ja.ass"))
        sub2 = _best("lang = 'en'") or _sidecar((".en.srt", ".en.ass"))
        if sub2 and not _secondary_track_ok(sub2, video):
            sub2 = None
        return video, sub, sub2
    finally:
        cx.close()


def _guess_type(path: str, default: str) -> str:
    mt, _ = mimetypes.guess_type(path)
    return mt or default


def _parse_range(range_header: Optional[str], size: int):
    """Parse a single-range `Range: bytes=…` header.

    Returns (start, end) inclusive, the string 'unsatisfiable', or None (no/again
    unparseable range → serve the whole file).
    """
    if not range_header:
        return None
    units, _, spec = range_header.partition("=")
    if units.strip().lower() != "bytes":
        return None
    spec = spec.split(",")[0].strip()  # honour only the first range
    start_s, _, end_s = spec.partition("-")
    try:
        if start_s == "":
            # suffix range: last N bytes
            n = int(end_s)
            if n <= 0:
                return None
            start, end = max(0, size - n), size - 1
        else:
            start = int(start_s)
            end = int(end_s) if end_s else size - 1
    except ValueError:
        return None
    if start >= size or start > end:
        return "unsatisfiable"
    return start, min(end, size - 1)


def range_response(
    request: Request,
    path: str | Path,
    *,
    default_type: str = "application/octet-stream",
    download_name: Optional[str] = None,
) -> Response:
    """Serve a file with HTTP Range support (200 full, 206 partial, 416 bad range).

    Handles HEAD (headers only). Sets Accept-Ranges / Content-Range / Content-Length
    and an inline Content-Disposition (Migaku reads the body via fetch→File)."""
    p = Path(path)
    size = p.stat().st_size
    media_type = _guess_type(str(p), default_type)

    headers = {
        "accept-ranges": "bytes",
        # caching is unhelpful for one-shot media injects and complicates range
        "cache-control": "no-store",
    }
    if download_name:
        headers["content-disposition"] = f'inline; filename="{download_name}"'

    rng = _parse_range(request.headers.get("range"), size)

    if rng == "unsatisfiable":
        headers["content-range"] = f"bytes */{size}"
        return Response(status_code=416, headers=headers, media_type=media_type)

    if rng is None:
        start, end, status = 0, size - 1, 200
    else:
        start, end = rng
        status = 206
        headers["content-range"] = f"bytes {start}-{end}/{size}"

    length = end - start + 1
    headers["content-length"] = str(length)

    if request.method == "HEAD":
        return Response(status_code=status, headers=headers, media_type=media_type)

    def body() -> Iterator[bytes]:
        with open(p, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(CHUNK, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    return StreamingResponse(body(), status_code=status, headers=headers, media_type=media_type)
