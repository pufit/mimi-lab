"""Media post-processing: probe -> remux/transcode -> organize -> upsert episode.

See notes/ARCHITECTURE.md §5.1 (acquisition→library) and §7 (codec reality), and
notes/DISCOVERY.md §7 for the exact ffmpeg commands (used verbatim below).

The Migaku Player is browser-based: it cannot decode HEVC/h265 or 10-bit h264, and
may choke on FLAC/Opus audio. So we always emit a browser-playable 8-bit-h264 MP4:
  * h264 source -> lossless remux (copy video + copy AAC-LC audio)  (~seconds)
  * HEVC/10-bit -> 8-bit h264 transcode (libx264 CRF by default; h264_videotoolbox
                   selectable as a hardware fallback) + copy/encode audio
  * audio: a browser-playable AAC-LC track is copied; only FLAC/Opus/AC3/HE-AAC is
    re-encoded to AAC.

Runs NATIVE (not Docker) for fast local I/O + the optional VideoToolbox path.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Optional

from ..config import settings

log = logging.getLogger("mimi_lab.media")

# video extensions we know how to process
VIDEO_EXTS = {".mkv", ".mp4", ".webm", ".ogg", ".m4v", ".avi", ".mov", ".ts"}

# codecs the browser cannot decode -> must transcode to h264
_TRANSCODE_CODECS = {"hevc", "h265", "vp9_unsupported_marker_never"}

# subprocess timeouts — a wedged ffmpeg/ffprobe otherwise blocks a job worker
# forever (stalling subtitles, comprehension, the whole pipeline). On expiry
# subprocess.run kills the child and raises TimeoutExpired -> the job fails
# and retries instead of hanging.
_PROBE_TIMEOUT = 120        # ffprobe metadata reads
_TRANSCODE_TIMEOUT = 7200   # a full remux/transcode (2h ceiling: even a long movie
                            # transcodes well under this on Apple silicon)
_SUBEXTRACT_TIMEOUT = 600   # embedded-subtitle stream copy

# Only one CPU-heavy transcode at a time. There are multiple job workers, and
# job priority only orders *claims* — without this gate, 3 queued postprocess
# jobs meant 3 parallel libx264 encodes fighting for the same cores (worse
# aggregate throughput + starving interactive work of CPU). Remuxes (stream
# copy, I/O-bound seconds) are not gated.
_TRANSCODE_GATE = threading.BoundedSemaphore(1)


# --------------------------------------------------------------------------- #
# events + prune helpers (pipeline automation)
# --------------------------------------------------------------------------- #
def _record_event(category, title, kind="info", detail=None, meta=None) -> None:
    """Record a pipeline event; never raises (lazy import to avoid cycles)."""
    try:
        from ..events import service as events
        events.record(category, title, kind, detail=detail, meta=meta)
    except Exception as e:  # pragma: no cover
        log.debug("event record (%s) failed: %s", category, e)


def _maybe_prune_original(source: Path, final_path: Path) -> None:
    """After a successful organize, delete the source file IFF
    settings.prune_originals is on AND the source lives under settings.inbox_dir.

    Never deletes anything inside the Library. Realpath-guarded; best-effort.
    """
    if not settings.prune_originals:
        return
    try:
        import os

        src = Path(source)
        if not src.exists() or not src.is_file():
            return
        src_real = os.path.realpath(str(src))
        inbox_real = os.path.realpath(str(settings.inbox_dir))
        library_real = os.path.realpath(str(settings.library_dir))
        final_real = os.path.realpath(str(final_path))

        # never delete the file we just organized into the library
        if src_real == final_real:
            return
        # must live strictly under inbox_dir
        if not src_real.startswith(inbox_real + os.sep):
            return
        # belt-and-suspenders: must NOT live under the library
        if src_real.startswith(library_real + os.sep) or src_real == library_real:
            return
        src.unlink()
        log.info("pruned original source file: %s", src_real)
    except Exception as e:  # pragma: no cover
        log.warning("prune original failed: %s", e)


# --------------------------------------------------------------------------- #
# probe
# --------------------------------------------------------------------------- #
def probe(path: str | Path) -> dict:
    """ffprobe a media file.

    Returns {codec, audio_codec, container, duration_ms, width, height}.
    Raises FileNotFoundError if the path is missing; RuntimeError on ffprobe error.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"media not found: {p}")

    cmd = [
        "ffprobe", "-v", "error", "-print_format", "json",
        "-show_streams", "-show_format", str(p),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=_PROBE_TIMEOUT)
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe failed for {p}: {proc.stderr.strip()}")

    data = json.loads(proc.stdout or "{}")
    streams = data.get("streams", [])
    fmt = data.get("format", {})

    vstream = next((s for s in streams if s.get("codec_type") == "video"), None)
    astream = next((s for s in streams if s.get("codec_type") == "audio"), None)

    # duration: prefer format.duration, fall back to the video stream
    duration_ms = None
    for src in (fmt.get("duration"), vstream.get("duration") if vstream else None):
        if src:
            try:
                duration_ms = int(round(float(src) * 1000))
                break
            except (TypeError, ValueError):
                continue

    return {
        "codec": (vstream or {}).get("codec_name"),
        "audio_codec": (astream or {}).get("codec_name"),
        "container": fmt.get("format_name"),
        "duration_ms": duration_ms,
        "width": (vstream or {}).get("width"),
        "height": (vstream or {}).get("height"),
    }


# --------------------------------------------------------------------------- #
# transcode decision
# --------------------------------------------------------------------------- #
def needs_transcode(codec: Optional[str]) -> bool:
    """True if the video codec must be transcoded (vs. a lossless remux).

    HEVC/h265 (and h264 High 10-bit, which browsers can't decode) -> transcode.
    Plain h264 -> remux (copy). Unknown codecs are transcoded to be safe.
    """
    if not codec:
        return False
    c = codec.strip().lower()
    if c in ("h264", "avc", "avc1"):
        return False
    if c in ("hevc", "h265"):
        return True
    # anything else the browser may not handle (vp9 ok, but mpeg4/av1/etc. -> transcode)
    return c not in ("vp8", "vp9", "av1")


def _is_high10(codec: Optional[str], probe_info: Optional[dict] = None) -> bool:
    """h264 High 10 (10-bit) is not browser-decodable -> needs transcode.

    Detected from the ffprobe profile if available.
    """
    if probe_info and (probe_info.get("profile") or "").lower().startswith("high 10"):
        return True
    return False


# --------------------------------------------------------------------------- #
# process_file: remux (h264) or transcode (HEVC) -> playable mp4
# --------------------------------------------------------------------------- #
def _ffprobe_video_profile(path: str | Path) -> Optional[str]:
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=profile", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=_PROBE_TIMEOUT,
        )
        return (proc.stdout or "").strip() or None
    except Exception:
        return None


def _japanese_audio_map(src: str | Path) -> str:
    """ffmpeg -map value selecting the ORIGINAL JAPANESE audio track.

    Dual-audio anime releases frequently list the English dub first, so a blind
    '0:a:0' grabs English. Find the Japanese track by language tag (jpn/ja) or
    title; fall back to the first non-English track, then to the first audio
    stream. Returns e.g. '0:a:1?'. The media pipeline prefers Japanese audio.
    """
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a",
             "-show_entries", "stream_tags=language,title", "-of", "json", str(src)],
            capture_output=True, text=True, timeout=_PROBE_TIMEOUT,
        )
        streams = json.loads(proc.stdout or "{}").get("streams", [])
    except Exception:
        return "0:a:0?"
    if len(streams) <= 1:
        return "0:a:0?"
    jpn = eng = None
    for i, s in enumerate(streams):  # i = audio-relative index (select_streams a is ordered)
        tags = {k.lower(): str(v).lower() for k, v in (s.get("tags") or {}).items()}
        lang, title = tags.get("language", ""), tags.get("title", "")
        if jpn is None and (lang in ("jpn", "ja", "jp") or "japanese" in title or "日本" in title):
            jpn = i
        if eng is None and (lang in ("eng", "en", "english") or "english" in title):
            eng = i
    if jpn is not None:
        return f"0:a:{jpn}?"
    if eng is not None:  # untagged but multi-track -> first track that isn't English
        for i in range(len(streams)):
            if i != eng:
                return f"0:a:{i}?"
    return "0:a:0?"


# --------------------------------------------------------------------------- #
# codec args: a true-lossless remux when possible, a quality transcode otherwise
# --------------------------------------------------------------------------- #
def _parse_bitrate(spec: str) -> int:
    """'10M' / '8000k' / '12000000' -> bits per second (defaults to 10M on junk)."""
    s = (spec or "").strip().lower()
    try:
        if s.endswith("m"):
            return int(float(s[:-1]) * 1_000_000)
        if s.endswith("k"):
            return int(float(s[:-1]) * 1_000)
        return int(float(s))
    except ValueError:
        return 10_000_000


def _target_video_bitrate(info: dict) -> str:
    """Resolution-aware video bitrate (bps, as a string) for the h264_videotoolbox
    fallback (libx264 uses CRF instead and ignores this).

    Anchored at settings.transcode_video_bitrate_1080p for 1080p and scaled by
    height for smaller frames, with a 2M floor. The old hard-coded 4M starved
    1080p sources (blocking/banding); ~10M @ 1080p is visually near-transparent.
    """
    anchor = _parse_bitrate(settings.transcode_video_bitrate_1080p)
    height = info.get("height") or 1080
    try:
        scaled = int(anchor * min(1.0, float(height) / 1080.0))
    except (TypeError, ValueError):
        scaled = anchor
    return str(max(2_000_000, scaled))


def _selected_audio_codec(src: str | Path, amap: str) -> tuple[Optional[str], Optional[str]]:
    """(codec_name, profile) of the audio track that `amap` selects (e.g. '0:a:1?')."""
    m = re.search(r"a:(\d+)", amap or "")
    idx = m.group(1) if m else "0"
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", f"a:{idx}",
             "-show_entries", "stream=codec_name,profile", "-of", "json", str(src)],
            capture_output=True, text=True, timeout=_PROBE_TIMEOUT,
        )
        streams = json.loads(proc.stdout or "{}").get("streams", [])
    except Exception:
        return None, None
    if not streams:
        return None, None
    s = streams[0]
    return (s.get("codec_name") or "").lower() or None, s.get("profile")


def _audio_args(src: str | Path, amap: str) -> list[str]:
    """Audio output args for the selected track.

    A browser-playable **AAC-LC** track is copied **losslessly** — no generational
    quality loss and no encoder-delay/edit-list churn. Anything the browser can't
    natively decode (FLAC / Opus / AC3 / HE-AAC) is re-encoded to AAC with async
    resampling so a gappy source can't drift the audio against the video.
    """
    codec, profile = _selected_audio_codec(src, amap)
    if codec == "aac" and (not profile or profile.strip().upper().startswith("LC")):
        return ["-c:a", "copy"]
    return ["-c:a", "aac", "-b:a", settings.audio_bitrate,
            "-af", "aresample=async=1:first_pts=0"]


def _video_args(transcode: bool, info: dict) -> list[str]:
    """Video output args.

    h264 (8-bit) -> lossless `copy`. Everything else -> 8-bit h264 (browser-safe),
    normalized to **CFR** so browser players schedule frames cleanly.

    Default encoder is **libx264** at a content-adaptive CRF (faster + better
    quality/bit than the throughput-capped VideoToolbox encoder on Apple silicon).
    `-pix_fmt yuv420p` is mandatory: a 10-bit BD source would otherwise produce
    High-10 h264 that browsers cannot decode. h264_videotoolbox is a config-
    selectable hardware fallback (fixed resolution-scaled bitrate).
    """
    if not transcode:
        return ["-c:v", "copy"]
    if settings.transcode_encoder == "h264_videotoolbox":
        return ["-c:v", "h264_videotoolbox", "-b:v", _target_video_bitrate(info),
                "-pix_fmt", "yuv420p", "-fps_mode", "cfr"]
    return ["-c:v", "libx264", "-preset", settings.transcode_preset,
            "-crf", str(settings.transcode_crf), "-pix_fmt", "yuv420p",
            "-fps_mode", "cfr"]


def process_file(src: str | Path, dst: str | Path) -> dict:
    """Produce a browser-playable MP4 at `dst` from `src`.

    h264 (8-bit)  -> lossless remux: copy video + copy AAC-LC audio (+faststart)
    HEVC / High10 -> 8-bit h264 transcode (libx264 CRF by default, CFR)
                     + copy/encode audio (+faststart)

    Audio is copied losslessly when it is already browser-playable AAC-LC, and
    only re-encoded to AAC for codecs the browser can't decode (FLAC/Opus/AC3).

    Returns {dst, path_taken: 'remux'|'transcode', codec, audio_codec}.
    Raises FileNotFoundError / RuntimeError on failure.
    """
    src = Path(src)
    dst = Path(dst)
    if not src.exists():
        raise FileNotFoundError(f"source not found: {src}")
    dst.parent.mkdir(parents=True, exist_ok=True)

    info = probe(src)
    codec = (info.get("codec") or "").lower()
    profile = _ffprobe_video_profile(src)
    high10 = (profile or "").lower().startswith("high 10")

    transcode = needs_transcode(codec) or high10
    path_taken = "transcode" if transcode else "remux"

    amap = _japanese_audio_map(src)  # Japanese audio only
    cmd = (
        ["ffmpeg", "-y", "-i", str(src), "-map", "0:v:0", "-map", amap]
        + _video_args(transcode, info)   # copy (h264) | VideoToolbox transcode (CFR)
        + _audio_args(src, amap)         # copy AAC-LC | re-encode non-AAC to AAC
        + ["-movflags", "+faststart", str(dst)]
    )

    log.info("process_file (%s): %s -> %s", path_taken, src.name, dst.name)
    if transcode:
        with _TRANSCODE_GATE:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=_TRANSCODE_TIMEOUT)
    else:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=_TRANSCODE_TIMEOUT)
    if proc.returncode != 0 or not dst.exists() or dst.stat().st_size == 0:
        # If a copy remux failed (e.g. odd source), retry as a transcode once.
        if not transcode:
            log.warning("remux failed, retrying as transcode: %s", proc.stderr.strip()[-500:])
            return _force_transcode(src, dst)
        raise RuntimeError(f"ffmpeg failed for {src}: {proc.stderr.strip()[-800:]}")

    out = probe(dst)
    return {
        "dst": str(dst),
        "path_taken": path_taken,
        "codec": out.get("codec"),
        "audio_codec": out.get("audio_codec"),
    }


def _force_transcode(src: Path, dst: Path) -> dict:
    """Fallback when a lossless remux fails on an odd source: force BOTH a
    VideoToolbox video transcode and an AAC audio re-encode (the most tolerant
    path), regardless of the source codecs."""
    info = probe(src)
    amap = _japanese_audio_map(src)  # Japanese audio only
    cmd = (
        ["ffmpeg", "-y", "-i", str(src), "-map", "0:v:0", "-map", amap]
        + _video_args(True, info)
        + ["-c:a", "aac", "-b:a", settings.audio_bitrate,
           "-af", "aresample=async=1:first_pts=0"]
        + ["-movflags", "+faststart", str(dst)]
    )
    with _TRANSCODE_GATE:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=_TRANSCODE_TIMEOUT)
    if proc.returncode != 0 or not dst.exists() or dst.stat().st_size == 0:
        raise RuntimeError(f"forced transcode failed for {src}: {proc.stderr.strip()[-800:]}")
    out = probe(dst)
    return {
        "dst": str(dst),
        "path_taken": "transcode",
        "codec": out.get("codec"),
        "audio_codec": out.get("audio_codec"),
    }


# --------------------------------------------------------------------------- #
# organize: Jellyfin / TRaSH naming into the library
# --------------------------------------------------------------------------- #
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_title(title: str) -> str:
    """Filesystem-safe title (Jellyfin/TRaSH style). Strips illegal chars, trims."""
    if not title:
        return "Unknown"
    s = _ILLEGAL.sub("", title)
    s = s.replace("　", " ").strip().rstrip(". ")  # no trailing dot/space
    s = re.sub(r"\s+", " ", s)
    return s or "Unknown"


def organize(
    src_processed: str | Path,
    anilist_id: Optional[int],
    ep_number: Optional[int],
    title_romaji: Optional[str],
    season: int = 1,
) -> Path:
    """Move/rename a processed mp4 into the library, Jellyfin-style.

        <library_dir>/<SafeTitle>/Season 01/<SafeTitle> - S01Exx.mp4

    Season and episode are zero-padded to 2 digits. Returns the final path.
    """
    src = Path(src_processed)
    if not src.exists():
        raise FileNotFoundError(f"processed file not found: {src}")

    title = safe_title(title_romaji or (f"AniList {anilist_id}" if anilist_id else "Unknown"))
    ep = int(ep_number) if ep_number is not None else 1
    season = int(season) if season else 1

    show_dir = Path(settings.library_dir) / title
    season_dir = show_dir / f"Season {season:02d}"
    season_dir.mkdir(parents=True, exist_ok=True)

    filename = f"{title} - S{season:02d}E{ep:02d}.mp4"
    dst = season_dir / filename

    # if source IS already the destination, nothing to do
    if src.resolve() == dst.resolve():
        return dst

    if dst.exists():
        # Never silently destroy an existing library file — a mis-mapped season
        # pack can overwrite good episodes. Keep ONE backup generation
        # (.replaced.mp4); a retention sweep prunes old ones.
        bak = dst.with_suffix(".replaced.mp4")
        try:
            if bak.exists():
                bak.unlink()
            dst.rename(bak)
            log.warning("organize: %s existed — kept backup at %s", dst.name, bak.name)
        except Exception as e:
            log.warning("organize: could not back up existing %s (%s); overwriting", dst, e)
            dst.unlink()
    shutil.move(str(src), str(dst))
    log.info("organized -> %s", dst)
    return dst


def finalize_import(
    src_processed: str | Path,
    anilist_id: int,
    ep_number: Optional[int],
    title_romaji: Optional[str] = None,
    season: int = 1,
    download_id: Optional[int] = None,
) -> dict:
    """Move an already-processed .mp4 into the Library and run the rest of the
    pipeline (episode upsert + subtitle fetch).

    Shared by the automatic postprocess path and the **manual match-confirm**
    flow. Returns {episode_id, final_path}.
    """
    final_path = organize(src_processed, anilist_id, ep_number, title_romaji, season)
    try:
        info = probe(final_path)
    except Exception as e:  # pragma: no cover - never block import on a reprobe
        log.debug("finalize_import: reprobe of %s failed: %s", final_path, e)
        info = {}
    episode_id = _upsert_episode(anilist_id, ep_number or 1, final_path, info)

    label = f"{title_romaji or anilist_id}"
    if ep_number is not None:
        label = f"{label} - E{int(ep_number):02d}"
    _record_event(
        "match", f"Added to Library: {label}", "success", detail=str(final_path),
        meta={"anilist_id": anilist_id, "ep_number": ep_number},
    )

    if download_id is not None:
        try:
            from ..db import cursor
            with cursor() as cx:
                cx.execute(
                    "UPDATE downloads SET state='postprocessed', linked_episode_id=?, "
                    "anilist_id=?, ep_number=?, updated_at=datetime('now') WHERE id=?",
                    (episode_id, anilist_id, ep_number, download_id),
                )
        except Exception as e:
            log.warning("finalize_import: could not link download %s: %s", download_id, e)

    if episode_id is not None:
        try:
            from ..jobs.service import enqueue
            enqueue("subtitle_fetch", {"episode_id": episode_id})
        except Exception as e:
            log.warning("finalize_import: could not enqueue subtitle_fetch: %s", e)

    return {"episode_id": episode_id, "final_path": str(final_path)}


# --------------------------------------------------------------------------- #
# embedded English softsub extraction (the secondary/reference track)
# --------------------------------------------------------------------------- #
#
# The transcode maps only video + Japanese audio (subtitle streams are dropped),
# and the original release is pruned after organize — so the embedded English
# softsub most WEB releases ship is our richest English
# source, but it can only be grabbed HERE, before the prune. We write it next to
# the organized MP4 as <base>.en.srt (Migaku smart-pairs by name; the media
# resolver also looks there). English is a reference track only — never tokenized.

# subtitle codecs we can convert to .srt (text-based). Image subs (PGS/DVD/DVB)
# would need OCR — skip them.
_TEXT_SUB_CODECS = {"subrip", "srt", "ass", "ssa", "mov_text", "webvtt", "text", "vtt"}


def _english_subtitle_stream(src: str | Path) -> Optional[int]:
    """Subtitle-relative index (for `-map 0:s:<i>`) of the best English text track
    in `src`, or None. Prefers a full dialogue track: skips forced / signs&songs
    and image-based (PGS/DVD/DVB) subtitles; lightly de-prioritises SDH."""
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "s", "-show_entries",
             "stream=codec_name:stream_tags=language,title:stream_disposition=forced,hearing_impaired",
             "-of", "json", str(src)],
            capture_output=True, text=True, timeout=_PROBE_TIMEOUT,
        )
        streams = json.loads(proc.stdout or "{}").get("streams", [])
    except Exception:
        return None
    best: Optional[tuple] = None  # (score, subtitle_index)
    for sidx, s in enumerate(streams):  # select_streams=s -> sidx is the s:<i> index
        codec = (s.get("codec_name") or "").lower()
        if codec not in _TEXT_SUB_CODECS:
            continue
        tags = {k.lower(): str(v).lower() for k, v in (s.get("tags") or {}).items()}
        disp = s.get("disposition") or {}
        lang, title = tags.get("language", ""), tags.get("title", "")
        if lang not in ("eng", "en", "english") and "english" not in title:
            continue
        forced = bool(disp.get("forced")) or "forced" in title or "sign" in title
        sdh = bool(disp.get("hearing_impaired")) or "sdh" in title or "hearing" in title
        text_native = 0 if codec in ("ass", "ssa", "subrip", "srt") else 1
        score = (1 if forced else 0, 1 if sdh else 0, text_native, sidx)  # lower sorts first
        if best is None or score < best[0]:
            best = (score, sidx)
    return best[1] if best else None


def _strip_srt_markup(path: Path) -> None:
    """Strip ffmpeg's ASS→SRT styling (<font…>, <i>/<b>, leftover {…}) from an .srt
    in place. SRT structure (indices, '-->' timestamps) has no such markup, so a
    blanket tag strip is safe. Best-effort — never raises."""
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
        cleaned = re.sub(r"\{[^}]*\}", "", re.sub(r"<[^>]+>", "", raw))
        if cleaned != raw:
            path.write_text(cleaned, encoding="utf-8")
    except Exception as e:  # pragma: no cover
        log.debug("srt markup strip skipped for %s: %s", path, e)


def embedded_sub_artifact(final_path: str | Path) -> Path:
    """Where the raw embedded-English extraction artifact for a video lives:
    ``Library/_subs/embedded/<video-stem>.en.srt``.

    Deliberately NOT next to the video — ``<base>.en.srt`` beside the video is
    the SERVED track (Migaku name-pairs it), which the english resolver's
    mt/AnimeTosho fallbacks legitimately rewrite. Keeping the extraction at a
    path only this module ever writes means (a) a generated track can never
    clobber or pose as the release's own subs, and (b) the artifact survives
    for later english runs even when a fallback won this one. Provenance is
    the path itself; structure is still gate-validated on every acceptance."""
    p = Path(final_path)
    return settings.library_dir / "_subs" / "embedded" / (p.with_suffix("").name + ".en.srt")


def extract_embedded_english(src: str | Path, final_path: str | Path) -> Optional[Path]:
    """Extract the embedded English subtitle from the original release to the
    ``embedded_sub_artifact`` path (the english resolver validates + promotes it
    onto the served `<final_base>.en.srt`). Must run BEFORE the original is
    pruned. Best-effort: returns the written path, or None (no English track /
    image subs / mismatched track / error)."""
    try:
        src = Path(src)
        final_path = Path(final_path)
        if not src.exists():
            return None
        sidx = _english_subtitle_stream(src)
        if sidx is None:
            return None
        dest = embedded_sub_artifact(final_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        cmd = ["ffmpeg", "-y", "-i", str(src), "-map", f"0:s:{sidx}", "-c:s", "srt", str(dest)]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=_SUBEXTRACT_TIMEOUT)
        if proc.returncode == 0 and dest.exists() and dest.stat().st_size > 0:
            _strip_srt_markup(dest)  # drop ffmpeg's <font…>/<i> ASS→SRT styling
            # Sanity: the embedded track must plausibly fit THIS episode's video
            # AND be structurally sound (not self-overlapping). Some source
            # releases mux a mismatched / wrong-episode / double-timed subtitle
            # track (for example, an overlong track on a normal-length video).
            # Such a sidecar is a bogus "ground
            # truth" that would drag alignment out of sync and show wrong English,
            # so discard it and let the English pipeline fall back to a
            # release-keyed source (AnimeTosho) instead. Shared gate — the same
            # validation every other subtitle acceptance point uses.
            from ..subs.service import sub_display_sane
            ok, why = sub_display_sane(dest, final_path)
            if not ok:
                log.warning("embedded English for %s rejected (%s) — discarding",
                            final_path.name, why)
                dest.unlink(missing_ok=True)
                return None
            log.info("extracted embedded English sub -> %s", dest.name)
            return dest
        try:
            dest.unlink(missing_ok=True)
        except Exception:
            pass
        log.info("no usable embedded English sub in %s (ffmpeg rc=%s)", src.name, proc.returncode)
        return None
    except Exception as e:  # pragma: no cover
        log.warning("extract_embedded_english failed: %s", e)
        return None


# --------------------------------------------------------------------------- #
# postprocess job handler
# --------------------------------------------------------------------------- #
def _pick_video_file(path: str | Path) -> Optional[Path]:
    """Resolve a save_path (file or dir) to the primary video file."""
    p = Path(path)
    if p.is_file() and p.suffix.lower() in VIDEO_EXTS:
        return p
    if p.is_dir():
        vids = [
            f for f in p.rglob("*")
            if f.is_file() and f.suffix.lower() in VIDEO_EXTS
            and not f.name.lower().endswith(".sample.mkv")
        ]
        if vids:
            return max(vids, key=lambda f: f.stat().st_size)  # largest = the episode
    return None


def _parse_filename(name: str) -> dict:
    """anitopy parse of a release filename. Best-effort; returns {} on failure."""
    try:
        import anitopy
        parsed = anitopy.parse(name) or {}
    except Exception as e:
        log.warning("anitopy parse failed for %r: %s", name, e)
        return {}
    # Fallback: anitopy can't tokenize 'S01E01-Title […].mkv' (episode token glued
    # to the episode title, e.g. EMBER packs) — pull SxxExx out ourselves.
    if not parsed.get("episode_number"):
        m = re.search(r"\bS(\d{1,2})[ ._-]?E(\d{1,4})\b", name, re.I)
        if m:
            parsed["anime_season"] = m.group(1)
            parsed["episode_number"] = m.group(2)
    return parsed


def _known_title_for_download(download_id: Optional[int]):
    """Title already pinned on a download row: (anilist_id, romaji|None, ep_number|None).

    A download started from a show/episode page records its chosen AniList id +
    episode on the `downloads` row before the torrent starts (see
    acquire.download_episode — the per-episode "fast download" — and download_batch),
    so the match is already known and must not be re-guessed from the filename.
    Returns None when there's no download_id or no id pinned on the row.
    """
    if download_id is None:
        return None
    try:
        from ..db import cursor
        with cursor() as cx:
            row = cx.execute(
                "SELECT anilist_id, ep_number FROM downloads WHERE id=?", (download_id,)
            ).fetchone()
            if not row or row["anilist_id"] is None:
                return None
            anilist_id = int(row["anilist_id"])
            ep_number = row["ep_number"] if "ep_number" in row.keys() else None
            trow = cx.execute(
                "SELECT romaji FROM titles WHERE anilist_id=?", (anilist_id,)
            ).fetchone()
            romaji = trow["romaji"] if trow else None
        return anilist_id, romaji, ep_number
    except Exception as e:
        log.debug("known-title lookup for download %s failed: %s", download_id, e)
        return None


def _resolve_title(parsed: dict, download_id: Optional[int], *, trust_row: bool = True):
    """Resolve {anilist_id, ep_number, title_romaji} for a finished download.

    Resolution order:
      1. (trust_row) If the download row already has an anilist_id, use it. A
         download started from a show page — the per-episode "fast download" or
         "Download season" — pins the exact title + episode up front, so there is
         nothing to guess. Batch per-file imports pass trust_row=False because they
         deliberately resolve a stray file independently of the pack's title.
      2. Auto-match the release filename via app.match.match_file(); link it only
         when the matcher is *confident*.
      3. Otherwise enqueue to the match_queue for manual confirmation, carrying the
         ranked candidates as suggestions.

    Returns (anilist_id|None, ep_number|None, title_romaji|None, queued: bool).
    """
    anime_title = parsed.get("anime_title")
    ep_raw = parsed.get("episode_number")
    if isinstance(ep_raw, list):  # anitopy yields a list for multi-episode files
        ep_raw = ep_raw[0] if ep_raw else None
    try:
        ep_number = int(ep_raw) if ep_raw is not None else None
    except (TypeError, ValueError):
        ep_number = None

    title_romaji = anime_title

    # (1) Trust the id pinned on the download row (fast download from a show page).
    if trust_row:
        known = _known_title_for_download(download_id)
        if known is not None:
            known_id, known_romaji, known_ep = known
            return (
                known_id,
                known_ep if known_ep is not None else ep_number,
                known_romaji or title_romaji,
                False,
            )

    # (2) Auto-match from the release filename. match_file() is the match module's
    #     only matcher entrypoint — call it directly so a wrong name fails loudly
    #     instead of silently sending everything to the manual queue.
    anilist_id = None
    res = None
    try:
        from ..match import service as match_service  # lazy: built in parallel
    except Exception as e:
        match_service = None
        log.info("match.service unavailable (%s) -> will queue for manual match", e)

    if match_service is not None:
        try:
            res = match_service.match_file(parsed.get("file_name") or anime_title or "")
            # Only auto-link a *confident* match. A low-confidence best guess is left
            # for manual confirmation rather than silently mislabeling the file.
            if res is not None and res.confident and res.best is not None:
                anilist_id = res.best.anilist_id
                title_romaji = res.best.romaji or title_romaji
        except Exception as e:
            log.warning("match_file failed for %r: %s", anime_title, e)

    # (3) Degrade: enqueue for manual confirmation with the ranked candidates.
    queued = False
    if anilist_id is None:
        filename = parsed.get("file_name") or anime_title or "unknown"
        candidates = list(res.candidates) if res is not None else []
        try:
            if match_service is not None:
                match_service.enqueue_unmatched(
                    filename, parsed, candidates, download_id=download_id
                )
            else:
                from ..db import cursor
                with cursor() as cx:
                    cx.execute(
                        "INSERT INTO match_queue(filename,parsed_json,ep_number,state,download_id) "
                        "VALUES(?,?,?,?,?)",
                        (
                            filename,
                            json.dumps(parsed, ensure_ascii=False),
                            ep_number,
                            "pending",
                            download_id,
                        ),
                    )
            queued = True
            log.info("queued for manual match: %s", anime_title)
        except Exception as e:
            log.warning("could not enqueue match_queue: %s", e)

    return anilist_id, ep_number, title_romaji, queued


def _upsert_episode(anilist_id: int, ep_number: int, final_path: Path, info: dict) -> Optional[int]:
    """Upsert the episode row via app.catalog (lazy import), else write directly.

    Returns episode_id or None.
    """
    # Preferred: go through catalog.service so its invariants hold.
    try:
        from ..catalog import service as catalog_service  # lazy: built in parallel
        for fn_name in ("upsert_episode", "set_episode_video", "register_episode_file"):
            fn = getattr(catalog_service, fn_name, None)
            if fn is None:
                continue
            try:
                res = fn(
                    anilist_id=anilist_id,
                    ep_number=ep_number,
                    video_path=str(final_path),
                    codec=info.get("codec"),
                    container="mp4",
                    duration_ms=info.get("duration_ms"),
                )
                if isinstance(res, int):
                    return res
                ep_id = getattr(res, "id", None) or (res.get("id") if isinstance(res, dict) else None)
                if ep_id:
                    return ep_id
            except TypeError:
                continue  # signature mismatch, try the next candidate
        log.info("catalog.service present but no compatible upsert fn; writing episode directly")
    except Exception as e:
        log.info("catalog.service unavailable (%s); writing episode directly", e)

    # Fallback: direct DB upsert (schema is the shared contract).
    return _upsert_episode_direct(anilist_id, ep_number, final_path, info)


def _upsert_episode_direct(anilist_id: int, ep_number: int, final_path: Path, info: dict) -> Optional[int]:
    from ..db import cursor
    try:
        with cursor() as cx:
            # ensure a titles row exists (FK), without clobbering catalog's data
            cx.execute(
                "INSERT OR IGNORE INTO titles(anilist_id) VALUES(?)", (anilist_id,)
            )
            cx.execute(
                "INSERT INTO episodes(anilist_id,ep_number,video_path,codec,container,duration_ms) "
                "VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(anilist_id,ep_number) DO UPDATE SET "
                "video_path=excluded.video_path, codec=excluded.codec, "
                "container=excluded.container, duration_ms=excluded.duration_ms, "
                "updated_at=datetime('now')",
                (
                    anilist_id, ep_number, str(final_path),
                    info.get("codec"), "mp4", info.get("duration_ms"),
                ),
            )
            row = cx.execute(
                "SELECT id FROM episodes WHERE anilist_id=? AND ep_number=?",
                (anilist_id, ep_number),
            ).fetchone()
            return row["id"] if row else None
    except Exception as e:
        log.warning("direct episode upsert failed: %s", e)
        return None


# --------------------------------------------------------------------------- #
# batch / season-pack import (many episodes from one torrent)
# --------------------------------------------------------------------------- #
def _list_video_files(path: str | Path) -> list[Path]:
    """All real episode video files under a path (file or dir), samples excluded.

    When real episodes (> ~50 MB) exist, tiny files (samples/thumbs) are dropped.
    """
    p = Path(path)
    if p.is_file() and p.suffix.lower() in VIDEO_EXTS:
        return [p]
    if not p.is_dir():
        return []
    vids = [
        f for f in p.rglob("*")
        if f.is_file() and f.suffix.lower() in VIDEO_EXTS
        and "sample" not in f.name.lower()
    ]
    real = [f for f in vids if f.stat().st_size > 50 * 1024 * 1024]
    return sorted(real or vids, key=lambda f: f.name.lower())


def _set_batch_counts(download_id: Optional[int], *, total_files=None, done_files=None) -> None:
    """Update a batch download's import counters (for the UI). Best-effort."""
    if download_id is None:
        return
    sets, params = [], []
    if total_files is not None:
        sets.append("total_files=?"); params.append(total_files)
    if done_files is not None:
        sets.append("done_files=?"); params.append(done_files)
    if not sets:
        return
    try:
        from ..db import cursor
        with cursor() as cx:
            cx.execute(
                f"UPDATE downloads SET {', '.join(sets)}, updated_at=datetime('now') WHERE id=?",
                (*params, download_id),
            )
    except Exception as e:  # pragma: no cover
        log.debug("batch: set counts failed: %s", e)


def _probe_process_organize(video: Path, anilist_id: int, ep_number: Optional[int],
                            title_romaji: Optional[str]) -> tuple[Path, dict]:
    """probe -> temp mp4 (remux/transcode) -> organize into the Library.
    Returns (final_path, probe_info). Raises on failure (caller handles per-file)."""
    info = probe(video)
    tmp_dir = Path(tempfile.mkdtemp(prefix="migaku_pp_"))
    tmp_out = tmp_dir / (video.stem + ".mp4")
    try:
        proc_res = process_file(video, tmp_out)
        info["codec"] = proc_res.get("codec") or info.get("codec")
        final_path = organize(tmp_out, anilist_id, ep_number, title_romaji)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    return final_path, info


def _import_batch_file(video: Path, anilist_id: int, title_romaji: Optional[str],
                       total: Optional[int], download_id: Optional[int],
                       wanted: Optional[set] = None, target_season: int = 1) -> dict:
    """Import one file from a season pack.

    Season-aware: a file explicitly tagged a DIFFERENT season than this title (a
    multi-season pack's "… Season 02 - 20" viewed from the S1 page) is skipped — it
    is NOT forced into this title by episode number (which would overwrite the real
    episode). Otherwise parse the episode number, place it within this title's
    season (folding absolute numbering via the Fribb offset); a file that doesn't
    fit is routed through the per-file matcher or skipped (extras/NCOP/NCED).
    `wanted`, when set, restricts the import to those (season-relative) episode
    numbers. Returns {ok, ep_number, episode_id} or {ok: False, reason}.
    """
    # a Director's Cut / recap re-numbers the season — never import it as the main
    # broadcast (it would overwrite real episodes). Check the file + its immediate
    # folder only — the pack's root folder is often named for its whole contents
    # (e.g. "… Director's Cut + OVAs + Movies") and must not match every file.
    if re.search(r"director'?s?\s*cut|\brecap\b|\bdigest\b", f"{video.parent.name} / {video.name}", re.I):
        return {"ok": False, "reason": "non-canonical variant (Director's Cut/recap)"}

    parsed = _parse_filename(video.name)
    raw_ep = parsed.get("episode_number")
    if isinstance(raw_ep, list):
        raw_ep = raw_ep[0] if raw_ep else None
    if raw_ep is None:
        return {"ok": False, "reason": "no episode number (extra/NCOP/NCED?)"}

    # season-aware guard: don't import another season's file into this title
    fs = parsed.get("anime_season")
    if isinstance(fs, list):
        fs = fs[0] if fs else None
    try:
        fs = int(fs) if fs is not None else None
    except (TypeError, ValueError):
        fs = None
    if fs is not None and fs != target_season:
        return {"ok": False, "reason": f"different season (S{fs}); not this title"}

    target_id = anilist_id
    title_for_org = title_romaji
    ep_number: Optional[int] = None

    try:
        from ..match import service as match_service
        rel = match_service.normalize_episode_number(anilist_id, raw_ep, total)
    except Exception:
        rel = None

    if rel is not None and (not total or 1 <= rel <= total):
        ep_number = rel  # fits this season
    else:
        # doesn't fit -> resolve the file on its own (multi-season pack / wrong show).
        # trust_row=False: ignore the pack's pinned id, match this file by its name.
        a2, e2, t2, _queued = _resolve_title(parsed, download_id, trust_row=False)
        if a2 is None or e2 is None:
            return {"ok": False, "reason": "could not place file in this season"}
        target_id, ep_number, title_for_org = a2, e2, (t2 or title_for_org)
        try:
            from ..db import connect as _connect
            from ..match import service as _m
            with _connect() as _cx:
                _tr = _cx.execute(
                    "SELECT total_episodes, romaji FROM titles WHERE anilist_id=?", (target_id,)
                ).fetchone()
            _rel2 = _m.normalize_episode_number(
                target_id, ep_number, (_tr["total_episodes"] if _tr else None)
            )
            if _rel2 is not None:
                ep_number = _rel2
            if _tr and _tr["romaji"]:
                title_for_org = _tr["romaji"]
        except Exception:
            pass

    # honour an explicit episode selection for THIS title (a deselected episode
    # that slipped through before file-selection applied is not imported)
    if wanted is not None and target_id == anilist_id and (ep_number is None or ep_number not in wanted):
        return {"ok": False, "reason": f"ep {ep_number} not in selection"}

    final_path, info = _probe_process_organize(video, target_id, ep_number, title_for_org)
    extract_embedded_english(video, final_path)  # before prune — needs the original
    _maybe_prune_original(video, final_path)
    episode_id = _upsert_episode(target_id, ep_number or 1, final_path, info)

    if episode_id is not None:
        try:
            from ..jobs.service import enqueue
            enqueue("subtitle_fetch", {"episode_id": episode_id})
        except Exception as e:
            log.warning("batch: could not enqueue subtitle_fetch for ep %s: %s", episode_id, e)

    _record_event(
        "match", f"Added to Library: {title_for_org or target_id} - E{int(ep_number or 1):02d}",
        "success", detail=str(final_path),
        meta={"anilist_id": target_id, "ep_number": ep_number, "kind": "batch"},
    )
    return {"ok": True, "ep_number": ep_number, "episode_id": episode_id}


def _maybe_prune_batch_dir(save_path: str | Path) -> None:
    """Remove a fully-imported batch dir from the inbox (inbox-only, realpath-
    guarded). Leaves the dir if any episode video remains (import incomplete)."""
    if not settings.prune_originals:
        return
    try:
        import os
        p = Path(save_path)
        if not p.is_dir():
            return
        real = os.path.realpath(str(p))
        inbox = os.path.realpath(str(settings.inbox_dir))
        library = os.path.realpath(str(settings.library_dir))
        if not real.startswith(inbox + os.sep):
            return
        if real == library or real.startswith(library + os.sep):
            return
        if any(f.is_file() and f.suffix.lower() in VIDEO_EXTS for f in p.rglob("*")):
            return  # real videos still present -> don't discard
        shutil.rmtree(p, ignore_errors=True)
        log.info("pruned batch inbox dir: %s", real)
    except Exception as e:  # pragma: no cover
        log.warning("prune batch dir failed: %s", e)


# straggler handling: a wanted episode that hasn't finished downloading yet
# (Transmission names in-progress files '.part', which the importer skips). Keep
# the torrent + retry the import a few times before giving up, so a slow/last file
# isn't permanently stranded.
_PP_MAX_RETRY = 20
_PP_RETRY_DELAY = 120  # seconds between straggler-import retries (~40 min total)
# nothing whole on disk yet (every file still '.part'): re-check a few times, then fail
_PP_EMPTY_MAX_RETRY = 5
_PP_SINGLE_RETRY_DELAY = 60  # a single episode is one file — re-check sooner


def _imported_episode_set(anilist_id: int, wanted: Optional[set] = None) -> set:
    """Episode numbers for `anilist_id` that already have a video on disk
    (optionally intersected with the `wanted` selection)."""
    from ..db import connect
    try:
        with connect() as cx:
            rows = cx.execute(
                "SELECT ep_number FROM episodes WHERE anilist_id=? AND video_path IS NOT NULL",
                (anilist_id,),
            ).fetchall()
        have = {r["ep_number"] for r in rows}
    except Exception:
        return set()
    return (have & set(wanted)) if wanted else have


def _postprocess_batch(download_id: Optional[int], save_path: str, anilist_id: int,
                       pp_attempt: int = 0) -> dict:
    """Import every episode in a finished season pack into `anilist_id`.

    Processes files sequentially (one ffmpeg at a time), updating the batch's
    done/total counters so the UI shows live progress. Only releases the torrent
    (keeps data) + marks 'postprocessed' once every WANTED episode is imported; if
    some are still downloading it keeps the torrent and re-imports shortly (so a
    last/slow file isn't stranded). Never raises.
    """
    summary: dict = {"ok": False, "kind": "batch", "download_id": download_id,
                     "anilist_id": anilist_id}

    title_romaji = None
    total = None
    wanted: Optional[set] = None
    target_season = 1
    try:
        from ..db import cursor
        with cursor() as cx:
            t = cx.execute(
                "SELECT romaji, english, total_episodes FROM titles WHERE anilist_id=?",
                (anilist_id,),
            ).fetchone()
            d = cx.execute(
                "SELECT wanted_eps FROM downloads WHERE id=?", (download_id,)
            ).fetchone() if download_id is not None else None
        if t:
            title_romaji = t["romaji"] or t["english"]
            total = t["total_episodes"]
            try:
                from ..acquire.service import title_season_ordinal
                target_season = title_season_ordinal(t["romaji"], t["english"])
            except Exception:
                target_season = 1
        if d and d["wanted_eps"]:
            try:
                wanted = {int(e) for e in json.loads(d["wanted_eps"])}
            except Exception:
                wanted = None
    except Exception as e:
        log.warning("batch: could not read title %s: %s", anilist_id, e)

    videos = _list_video_files(save_path)
    summary["files_found"] = len(videos)
    if not videos:
        # Nothing whole yet — a torrent can read complete while its files are
        # still 'name.part' (Transmission renames each only when every byte is in;
        # the endgame's last pieces can take minutes). Wait and re-check; if
        # nothing is even in progress, or the retries run out, fail VISIBLY —
        # this used to return quietly and the pack was never imported.
        if download_id is not None and _has_partial_files(save_path) and pp_attempt < _PP_EMPTY_MAX_RETRY:
            try:
                from ..jobs.service import enqueue
                enqueue("postprocess", {"download_id": download_id, "pp_attempt": pp_attempt + 1},
                        delay_seconds=_PP_RETRY_DELAY)
            except Exception as e:
                log.warning("batch: could not re-enqueue postprocess for %s: %s", download_id, e)
            log.info("batch %s: no finished video under %s yet — retry %d/%d in %ds",
                     download_id, save_path, pp_attempt + 1, _PP_EMPTY_MAX_RETRY, _PP_RETRY_DELAY)
            summary.update(ok=True, waiting=True, retry=pp_attempt + 1)
            return summary
        summary["error"] = f"no video files under {save_path}"
        log.warning("postprocess_batch: %s", summary["error"])
        if download_id is not None:
            try:
                from ..db import cursor
                with cursor() as cx:
                    cx.execute(
                        "UPDATE downloads SET state='pp_failed', updated_at=datetime('now') WHERE id=?",
                        (download_id,),
                    )
            except Exception as e:
                log.warning("batch: could not mark %s pp_failed: %s", download_id, e)
        _record_event(
            "download", "Season pack: nothing to import", "warning",
            detail=f"{title_romaji or anilist_id} — no finished video file under the pack folder",
            meta={"download_id": download_id, "anilist_id": anilist_id, "kind": "batch"},
        )
        return summary

    # progress is cumulative across straggler-retries: total = wanted count,
    # done = wanted episodes already in the library.
    expected = len(wanted) if wanted else (total or len(videos))
    base = len(_imported_episode_set(anilist_id, wanted))
    _set_batch_counts(download_id, total_files=expected, done_files=base)

    imported: list[dict] = []
    skipped: list[dict] = []
    for video in videos:
        try:
            res = _import_batch_file(video, anilist_id, title_romaji, total, download_id,
                                     wanted, target_season)
        except Exception as e:
            log.warning("batch: import failed for %s: %s", video.name, e)
            res = {"ok": False, "reason": str(e)}
        if res.get("ok"):
            imported.append({"ep_number": res.get("ep_number"), "episode_id": res.get("episode_id")})
            _set_batch_counts(download_id, done_files=base + len(imported))
        else:
            skipped.append({"file": video.name, "reason": res.get("reason")})

    # which WANTED episodes are still missing? (a file that hadn't finished
    # downloading yet is named '.part' by Transmission and was skipped above)
    have = _imported_episode_set(anilist_id, wanted)
    if wanted:
        missing = sorted(set(wanted) - have)
    elif total:
        missing = sorted(set(range(1, int(total) + 1)) - have)
    else:
        missing = []

    # still waiting on episodes and retries left -> KEEP the torrent + data and
    # re-import shortly (releasing/pruning now would strand the unfinished files)
    if download_id is not None and missing and pp_attempt < _PP_MAX_RETRY:
        try:
            from ..jobs.service import enqueue
            enqueue("postprocess", {"download_id": download_id, "pp_attempt": pp_attempt + 1},
                    delay_seconds=_PP_RETRY_DELAY)
        except Exception as e:
            log.warning("batch: could not re-enqueue postprocess for %s: %s", download_id, e)
        log.info("batch %s: %d/%d imported, waiting on eps %s (retry %d/%d)",
                 download_id, len(have), expected, missing, pp_attempt + 1, _PP_MAX_RETRY)
        summary.update(ok=True, imported=len(imported), waiting_on=missing, retry=pp_attempt + 1)
        return summary

    # finalize: every wanted episode imported (or retries exhausted) -> stop seeding,
    # unregister the torrent (keep data), prune the inbox.
    if download_id is not None:
        try:
            from ..acquire import service as acquire
            acquire.release_torrent(download_id)
        except Exception as e:
            log.warning("batch release_torrent failed for %s: %s", download_id, e)
        try:
            from ..db import cursor
            with cursor() as cx:
                cx.execute(
                    "UPDATE downloads SET state='postprocessed', updated_at=datetime('now') WHERE id=?",
                    (download_id,),
                )
        except Exception as e:
            log.warning("batch: could not mark postprocessed: %s", e)
    _maybe_prune_batch_dir(save_path)

    summary.update(ok=True, imported=len(imported), skipped=len(skipped),
                   episodes=imported, skipped_files=skipped[:10], missing=missing)
    if missing:
        _record_event(
            "download", f"Season pack: couldn't download {len(missing)} episode(s)", "warning",
            detail=f"{title_romaji or anilist_id} — missing {missing} (no seeders / stalled)",
            meta={"download_id": download_id, "anilist_id": anilist_id, "kind": "batch", "missing": missing},
        )
    else:
        _record_event(
            "download", f"Season pack imported: {len(have)} episode(s)", "success",
            detail=f"{title_romaji or anilist_id} — {len(have)}/{expected} episodes",
            meta={"download_id": download_id, "anilist_id": anilist_id, "kind": "batch"},
        )
    log.info("postprocess_batch done: %s", summary)
    return summary


def _has_partial_files(path: str | Path) -> bool:
    """True when Transmission is still writing here: the path itself is a
    'name.part' in progress, or the folder holds one."""
    try:
        p = Path(path)
        if Path(str(p) + ".part").exists():
            return True
        if p.is_dir():
            return any(f.is_file() and f.name.lower().endswith(".part") for f in p.rglob("*"))
    except Exception as e:  # pragma: no cover
        log.debug("partial-file check failed for %s: %s", path, e)
    return False


def _other_rows_needing_source(download_id: Optional[int], qbt_hash: Optional[str],
                               source: Optional[Path]) -> list[int]:
    """Other download rows whose import still needs this source file: same
    torrent, or the same save_path. Never raises."""
    if download_id is None:
        return []
    try:
        from ..acquire.service import other_rows_needing_torrent
        ids = set(other_rows_needing_torrent(qbt_hash, download_id))
        if source is not None:
            from ..db import cursor
            with cursor() as cx:
                rows = cx.execute(
                    "SELECT id FROM downloads WHERE id<>? AND save_path=? "
                    "AND state IN ('queued','downloading','completed','pp_failed')",
                    (download_id, str(source)),
                ).fetchall()
            ids.update(r["id"] for r in rows)
        return sorted(ids)
    except Exception as e:  # pragma: no cover
        log.debug("shared-source check failed for %s: %s", download_id, e)
        return []


def _pp_error(download_id, msg: str) -> None:
    """Mark the download row failed (so the UI shows it) and RAISE.

    Raising lets the queue retry and, on terminal failure, surface an error
    event and a retryable job.
    """
    if download_id is not None:
        try:
            from ..db import cursor
            with cursor() as cx:
                cx.execute(
                    "UPDATE downloads SET state='pp_failed', updated_at=datetime('now') WHERE id=?",
                    (download_id,),
                )
        except Exception as e:
            log.warning("could not mark download %s pp_failed: %s", download_id, e)
    raise RuntimeError(msg)


def postprocess(payload: dict) -> dict:
    """Job handler: full media pipeline for a finished download (or a raw path).

    payload: {download_id: int}  or  {path: str}

    Steps: resolve source video -> anitopy parse -> match (lazy) -> probe ->
    process_file(temp) -> organize(Library) -> upsert episode (lazy catalog) ->
    enqueue('subtitle_fetch', {episode_id}).

    Cross-module imports are wrapped defensively (modules build in parallel).
    Returns a summary dict on success; RAISES on pipeline failure (probe /
    transcode / organize) so the job queue's retry + error surfacing applies.
    """
    summary: dict = {"ok": False}
    download_id = payload.get("download_id")
    raw_path = payload.get("path")

    # 1. resolve the source video file + the release filename to parse
    save_path = raw_path
    dl_kind = "single"
    dl_anilist = None
    dl_hash = None
    if download_id is not None and not save_path:
        try:
            from ..db import cursor
            with cursor() as cx:
                row = cx.execute(
                    "SELECT save_path, title_guess, kind, anilist_id, qbt_hash "
                    "FROM downloads WHERE id=?",
                    (download_id,),
                ).fetchone()
            if row:
                save_path = row["save_path"]
                summary["title_guess"] = row["title_guess"]
                dl_kind = (row["kind"] if "kind" in row.keys() else None) or "single"
                dl_anilist = row["anilist_id"] if "anilist_id" in row.keys() else None
                dl_hash = row["qbt_hash"] if "qbt_hash" in row.keys() else None
        except Exception as e:
            log.warning("could not read download %s: %s", download_id, e)

    if not save_path:
        _pp_error(download_id, "postprocess: no path / save_path to process")

    pp_attempt = int(payload.get("pp_attempt", 0) or 0)

    # batch / season pack -> import every video file into the title (own pipeline)
    if dl_kind == "batch" and dl_anilist is not None:
        return _postprocess_batch(download_id, save_path, dl_anilist, pp_attempt)

    video = _pick_video_file(save_path)
    if video is None:
        # Not whole yet? Transmission writes an in-progress file as 'name.part'
        # and renames it only when every byte is in; a torrent can read complete
        # a beat before that. Wait and re-check instead of failing the download.
        if download_id is not None and _has_partial_files(save_path) and pp_attempt < _PP_EMPTY_MAX_RETRY:
            try:
                from ..jobs.service import enqueue
                enqueue("postprocess", {"download_id": download_id, "pp_attempt": pp_attempt + 1},
                        delay_seconds=_PP_SINGLE_RETRY_DELAY)
            except Exception as e:
                log.warning("postprocess %s: could not re-enqueue: %s", download_id, e)
            log.info("postprocess %s: file still downloading (.part) — retry %d/%d in %ds",
                     download_id, pp_attempt + 1, _PP_EMPTY_MAX_RETRY, _PP_SINGLE_RETRY_DELAY)
            summary.update(ok=True, waiting=True, retry=pp_attempt + 1)
            return summary
        _pp_error(download_id, f"postprocess: no video file under {save_path}")

    parse_name = video.name
    summary["source"] = str(video)

    # 2. parse + resolve title
    parsed = _parse_filename(parse_name)
    parsed.setdefault("file_name", parse_name)
    anilist_id, ep_number, title_romaji, queued = _resolve_title(parsed, download_id)
    # Fold absolute (cross-season) episode numbers back to season-relative so a
    # sequel's absolute-numbered release doesn't land at a phantom episode number
    # (mirrors the subtitle pipeline; see match.normalize_episode_number).
    if anilist_id is not None and ep_number is not None:
        try:
            from ..match import service as _match
            from ..db import connect as _connect
            with _connect() as _cx:
                _tr = _cx.execute(
                    "SELECT total_episodes FROM titles WHERE anilist_id=?", (anilist_id,)
                ).fetchone()
            _total = (_tr["total_episodes"] if _tr else None) or None
            _rel = _match.normalize_episode_number(anilist_id, ep_number, _total)
            if _rel is not None:
                ep_number = _rel
        except Exception as e:  # never block postprocess on normalization
            log.debug("ep-number normalize skipped for %s: %s", anilist_id, e)
    summary.update(
        anilist_id=anilist_id, ep_number=ep_number,
        title_romaji=title_romaji, queued_for_match=queued,
    )

    # 3. probe + 4. process into a temp mp4
    try:
        info = probe(video)
    except Exception as e:
        _pp_error(download_id, f"probe failed for {video.name}: {e}")

    tmp_dir = Path(tempfile.mkdtemp(prefix="migaku_pp_"))
    tmp_out = tmp_dir / (video.stem + ".mp4")
    try:
        proc_res = process_file(video, tmp_out)
        summary["path_taken"] = proc_res["path_taken"]
        info["codec"] = proc_res.get("codec") or info.get("codec")
    except Exception as e:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        _pp_error(download_id, f"process_file failed for {video.name}: {e}")

    # 5. organize into the library (needs at least an ep number; default to 1)
    if anilist_id is None:
        # we couldn't resolve -> keep the processed file in inbox, leave in match_queue
        staged = Path(settings.inbox_dir) / "processed"
        staged.mkdir(parents=True, exist_ok=True)
        final_path = staged / tmp_out.name
        try:
            if final_path.exists():
                final_path.unlink()
            shutil.move(str(tmp_out), str(final_path))
        except Exception as e:
            log.warning("could not stage unmatched processed file: %s", e)
            final_path = tmp_out
        summary["final_path"] = str(final_path)
        summary["ok"] = True
        summary["note"] = "unmatched: staged, awaiting manual match"
        shutil.rmtree(tmp_dir, ignore_errors=True)
        # Record the staged path onto the pending match_queue row so a later
        # confirm() can move THIS processed file into the Library (it's renamed
        # to .mp4 and lives outside library_dir, so confirm can't re-derive it).
        try:
            from ..db import cursor
            with cursor() as cx:
                cx.execute(
                    "UPDATE match_queue SET processed_path=? WHERE id=("
                    "  SELECT id FROM match_queue WHERE state='pending' AND filename=?"
                    "  ORDER BY id DESC LIMIT 1)",
                    (str(final_path), video.name),
                )
        except Exception as e:
            log.warning("could not record processed_path for %s: %s", video.name, e)
        _record_event(
            "match", f"Needs confirmation: {video.name}", "warning",
            detail="Could not confidently match this release; queued for manual confirmation.",
            meta={"download_id": download_id, "filename": video.name},
        )
        return summary

    try:
        final_path = organize(tmp_out, anilist_id, ep_number, title_romaji)
        summary["final_path"] = str(final_path)
    except Exception as e:
        _pp_error(download_id, f"organize failed for {video.name}: {e}")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    # organized into Library -> record a match event
    _ep_label = f"{title_romaji or anilist_id}"
    if ep_number is not None:
        _ep_label = f"{_ep_label} - E{int(ep_number):02d}"
    _record_event(
        "match", f"Added to Library: {_ep_label}", "success",
        detail=str(final_path),
        meta={"anilist_id": anilist_id, "ep_number": ep_number},
    )

    # 5a. release the torrent now that the episode is safely in the Library:
    # stop seeding + unregister from Transmission BEFORE any prune, so pruning the
    # inbox source file can't strand a still-registered torrent. (Approach C; the
    # seed-ratio policy already stopped seeding the instant the download finished.)
    if download_id is not None:
        try:
            from ..acquire import service as acquire
            rel = acquire.release_torrent(download_id)
            if not rel.get("ok"):
                log.info("release_torrent(%s): %s", download_id, rel.get("reason"))
        except Exception as e:  # never block postprocess on torrent cleanup
            log.warning("release_torrent failed for %s: %s", download_id, e)

    # 5b. extract the embedded English softsub (the secondary track) BEFORE pruning
    # — it's the only moment we still have the original release on disk.
    extract_embedded_english(video, final_path)

    # 5c. prune the original source file if configured (inbox only — never Library)
    # — unless another download row still relies on this very file (the same
    # release added twice: its import is queued behind ours on the same source).
    others = _other_rows_needing_source(download_id, dl_hash, video)
    if others:
        log.info("postprocess %s: source kept — download %s still needs it", download_id, others[0])
    else:
        _maybe_prune_original(video, final_path)

    # 6. upsert episode (lazy catalog), then link the download
    episode_id = _upsert_episode(anilist_id, ep_number or 1, final_path, info)
    summary["episode_id"] = episode_id

    if download_id is not None:
        try:
            from ..db import cursor
            with cursor() as cx:
                cx.execute(
                    "UPDATE downloads SET state='postprocessed', linked_episode_id=?, "
                    "anilist_id=?, ep_number=?, updated_at=datetime('now') WHERE id=?",
                    (episode_id, anilist_id, ep_number, download_id),
                )
        except Exception as e:
            log.warning("could not link download %s: %s", download_id, e)

    # 7. kick off subtitle pipeline
    if episode_id is not None:
        try:
            from ..jobs.service import enqueue
            enqueue("subtitle_fetch", {"episode_id": episode_id})
            summary["enqueued_subtitle_fetch"] = True
        except Exception as e:
            log.warning("could not enqueue subtitle_fetch: %s", e)

    summary["ok"] = True
    log.info("postprocess done: %s", summary)
    return summary


def _register_jobs() -> None:
    """Register the postprocess handler (idempotent)."""
    try:
        from ..jobs.service import register
    except Exception as e:  # pragma: no cover
        log.warning("jobs.service unavailable, postprocess not registered: %s", e)
        return
    register("postprocess", postprocess)


_register_jobs()
