"""Subtitle pipeline: jimaku fetch -> clean -> align -> ingest into the corpus.

Flow (fetch_for_episode):
  episode -> jimaku search(anilist_id) -> entries/{id}/files?episode=N
  -> rank candidates (avoid re-cut/Director's-Cut traps; prefer .srt + matching
     release group/quality)
  -> download (Authorization header)
  -> SELECT + ALIGN (self-verifying): if a trusted, video-timed reference exists
     (the embedded English softsub), align each top candidate to it and keep the
     one whose cue-onsets best match — this both picks the right file and corrects
     framerate/PAL drift. Without a reference, apply the robust alass->video
     default and mark it unverified (audio VAD is too weak for anime to trust).
  -> write <Title>.ja.srt next to the mp4 (Migaku smart-pairs by filename)
  -> clean_ja_caption() strips broadcast-caption artifacts (（speaker）/《…》/→/♪/
     furigana/full-width spacing), rewriting the .srt + populating the corpus
  -> parse (pysubs2) -> subtitle_lines -> tokenize -> line_lemmas + subtitle_fts
     + per-line furigana.

JA is often fetched for comprehension BEFORE a video/English exists (no alignment
possible then). `align_episode` is the re-entrant alignment authority: it (re)runs
whenever new data arrives (video extracted, English fetched) and only changes
timing when a trusted reference lets it verify the result. It stores an
`align_score` confidence so low-confidence episodes are flagged, not shipped
silently.

Registered `jobs` handlers: 'subtitle_fetch', 'subtitle_align',
'subtitle_align_sweep', 'late_sub_sweep', 'title_subtitle_fetch'.
"""
from __future__ import annotations

import logging
import math
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

import httpx
import pysubs2

from ..config import settings
from ..db import connect, cursor
from ..learn import service as learn_service

log = logging.getLogger("mimi_lab.subs")

JIMAKU_BASE = "https://jimaku.cc/api"


# --------------------------------------------------------------------------
# jimaku client
# --------------------------------------------------------------------------


def _jimaku_headers() -> dict:
    # Token from settings only — never hardcode / never log it.
    return {"Authorization": settings.jimaku_token, "Accept": "application/json"}


# Global throttle: jimaku rate-limits by IP. The job worker is single-threaded,
# but we still serialise + space out calls and honour 429 Retry-After, so a bulk
# analyze of hundreds of titles paces itself instead of blowing the limit and
# erroring out (which is exactly what happened on the first run).
_JIMAKU_MIN_INTERVAL = 1.5
_JIMAKU_LOCK = threading.Lock()
_JIMAKU_LAST = [0.0]


def _throttled_get(url: str, params: Optional[dict] = None, *, timeout: float = 30,
                   follow_redirects: bool = False) -> httpx.Response:
    """GET with a global jimaku pace + 429/Retry-After backoff. Shared by the API
    calls AND the file downloads, since both hit jimaku's IP rate limit. Raises
    only on persistent failure, so a transient 429 no longer fails the job."""
    last_exc: Optional[Exception] = None
    for attempt in range(5):
        with _JIMAKU_LOCK:
            gap = _JIMAKU_MIN_INTERVAL - (time.monotonic() - _JIMAKU_LAST[0])
            if gap > 0:
                time.sleep(gap)
            _JIMAKU_LAST[0] = time.monotonic()
        try:
            r = httpx.get(url, params=params, headers=_jimaku_headers(),
                          timeout=timeout, follow_redirects=follow_redirects)
        except Exception as e:  # transient network
            last_exc = e
            time.sleep(2.0 * (attempt + 1))
            continue
        if r.status_code == 429:
            try:
                ra = float(r.headers.get("Retry-After", 0) or 0)
            except ValueError:
                ra = 0.0
            sleep_s = min(max(ra, _JIMAKU_MIN_INTERVAL * (attempt + 2)), 30.0)
            log.info("jimaku 429 — backing off %.1fs (attempt %d/5)", sleep_s, attempt + 1)
            time.sleep(sleep_s)
            continue
        r.raise_for_status()
        return r
    if last_exc:
        raise last_exc
    raise RuntimeError("jimaku rate-limited (429) after retries")


def _jimaku_get(path: str, params: dict, timeout: float = 30) -> httpx.Response:
    return _throttled_get(f"{JIMAKU_BASE}{path}", params, timeout=timeout)


def jimaku_search(anilist_id: int) -> list[dict]:
    """GET /entries/search?anilist_id=<id>&anime=true -> list of entries."""
    data = _jimaku_get("/entries/search", {"anilist_id": anilist_id, "anime": "true"}).json()
    return data if isinstance(data, list) else data.get("entries", [])


def jimaku_files(entry_id: int, episode: Optional[int] = None) -> list[dict]:
    """GET /entries/{id}/files?episode=<n> -> list of file descriptors."""
    params = {}
    if episode is not None:
        params["episode"] = episode
    data = _jimaku_get(f"/entries/{entry_id}/files", params).json()
    return data if isinstance(data, list) else data.get("files", [])


_GROUP_RE = re.compile(r"\[([^\]]+)\]")
_QUALITY_RE = re.compile(r"(\d{3,4})p", re.I)
_KANA_RE = re.compile(r"[぀-ヿ]")  # hiragana + katakana


def _has_japanese(text: str) -> bool:
    """A line counts as Japanese dialogue if it contains any kana. Filters out
    non-Japanese fansub credit / karaoke lines (Chinese-only, romaji, English)
    that would otherwise pollute the lemma index + comprehension denominator."""
    return bool(_KANA_RE.search(text or ""))


def _file_name(f: dict) -> str:
    return f.get("name") or f.get("filename") or ""


# Tokens that mark a subtitle (or video) as a *re-edited cut* — 新編集版
# "New Edited Version" / Director's Cut, recap/compilation films, or
# "combined episodes". Their timeline doesn't fit a normal broadcast/BD episode
# at all, so pairing one with a normal release desyncs far worse than an OP
# splice (and no aligner can rescue it). Sink them unless the *video* is the
# same cut.
_CUT_MISMATCH_RE = re.compile(
    r"新編集版|新编集版|総集編|director'?s?[ ._-]*cut|\bdircut\b|"
    r"combined[ ._-]*episodes?|\brecap\b",
    re.I,
)
# Source/medium tokens for soft affinity (BD-for-BD, web-for-web…).
_SOURCE_RE = re.compile(r"blu-?ray|bdrip|\bbd\b|web-?rip|web-?dl|\bweb\b|\bhdtv\b|\btv\b", re.I)


def _is_recut(name: str) -> bool:
    return bool(_CUT_MISMATCH_RE.search(name or ""))


def rank_files(files: list[dict], prefer: Optional[str] = None,
               video_is_recut: Optional[bool] = None) -> list[dict]:
    """Rank jimaku subtitle files best-first. Priority order:

    1. **Cut match** — a re-edited-cut sub (新編集版 / Director's Cut / recap /
       combined) only matches when the *video* is that same cut; otherwise it
       sinks to the bottom (its timeline can't be aligned to a normal episode).
    2. **Extension** — .srt > .ass/.ssa > other (both play; .srt ingests cleaner).
    3. **Release-group** token shared with `prefer` (e.g. the source filename).
    4. **Resolution** then **source/medium** affinity (1080p, BD, WEB…).
    5. Filename — stable deterministic tiebreak.

    Robust by design: a wrong pick still gets re-aligned to the video audio by
    `align()`, so this only has to dodge the *cut* trap and break ties sensibly.
    """
    if not files:
        return []

    prefer = prefer or ""
    if video_is_recut is None:
        video_is_recut = _is_recut(prefer)
    want_groups = {g.lower() for g in _GROUP_RE.findall(prefer)}
    want_quality = {q.lower() for q in _QUALITY_RE.findall(prefer)}
    want_source = {s.lower() for s in _SOURCE_RE.findall(prefer)}

    def ext_rank(name: str) -> int:
        low = name.lower()
        if low.endswith(".srt"):
            return 0
        if low.endswith(".ass") or low.endswith(".ssa"):
            return 1
        return 2

    def score(f: dict) -> tuple:
        name = _file_name(f)
        low = name.lower()
        cut_mismatch = 1 if (_is_recut(name) != bool(video_is_recut)) else 0
        group_match = any(g in low for g in want_groups) if want_groups else False
        quality_match = any(q in low for q in want_quality) if want_quality else False
        source_match = any(s in low for s in want_source) if want_source else False
        # lower tuple sorts first
        return (
            cut_mismatch,              # 1. never pair a re-cut with a normal release
            ext_rank(name),            # 2. srt first
            0 if group_match else 1,   # 3. same release group
            0 if quality_match else 1, # 4a. same resolution
            0 if source_match else 1,  # 4b. same source/medium (BD/WEB)
            name,                      # 5. deterministic
        )

    return sorted(files, key=score)


def pick_best_file(files: list[dict], prefer: Optional[str] = None,
                   video_is_recut: Optional[bool] = None) -> Optional[dict]:
    """Best single subtitle file (see `rank_files`). Kept for callers/tests."""
    ranked = rank_files(files, prefer=prefer, video_is_recut=video_is_recut)
    return ranked[0] if ranked else None


def _file_url(f: dict) -> Optional[str]:
    return f.get("url") or f.get("download_url")


def download_subtitle(file: dict, dest_path: Path) -> Path:
    """Download a jimaku file (its `url`) to dest_path."""
    url = _file_url(file)
    if not url:
        raise RuntimeError(f"jimaku file has no url: {_file_name(file)!r}")
    dest_path = Path(dest_path)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    # throttled like the API calls — jimaku rate-limits downloads too.
    r = _throttled_get(url, timeout=120, follow_redirects=True)
    dest_path.write_bytes(r.content)
    return dest_path


# --------------------------------------------------------------------------
# Alignment — alass (split-aware) primary, ffsubsync (global) fallback
# --------------------------------------------------------------------------
#
# The hard case: the jimaku sub is timed to a release whose
# OP/ED differs from our video, so the timeline has a *step* — in sync up to the
# OP, then ~90s off for the rest of the episode. ffsubsync only models one
# global offset + framerate, so it cannot fix a mid-episode splice (it lands on
# a garbage compromise that fits nowhere). **alass** models splits (commercial
# breaks / OP / different cuts) and shifts each block independently — exactly
# this case. So alass runs first; ffsubsync is the fallback when it's absent.
#
# Whatever an aligner produces is then sanity-gated (most cues must land within
# the video runtime) before we accept it, so a bogus alignment can never replace
# a merely-offset original.


def settings_root() -> Path:
    # project root = two levels up from this file's package
    return Path(__file__).resolve().parent.parent.parent


def _alass_cmd() -> Optional[list[str]]:
    """Locate the alass CLI (Homebrew installs it as `alass-cli`)."""
    for name in ("alass-cli", "alass"):
        exe = shutil.which(name)
        if exe:
            return [exe]
    for cand in ("/opt/homebrew/bin/alass-cli", "/usr/local/bin/alass-cli"):
        if Path(cand).exists():
            return [cand]
    return None


def _ffsubsync_cmd() -> list[str]:
    exe = settings_root() / ".venv" / "bin" / "ffsubsync"
    if exe.exists():
        return [str(exe)]
    return [sys.executable, "-m", "ffsubsync"]


_DURATION_CACHE: dict[str, Optional[int]] = {}


def _video_duration_ms(video_path: Path) -> Optional[int]:
    """Video runtime in ms via ffprobe (cached). None if it can't be read."""
    key = str(video_path)
    if key in _DURATION_CACHE:
        return _DURATION_CACHE[key]
    ms: Optional[int] = None
    try:
        p = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nokey=1:noprint_wrappers=1", str(video_path)],
            capture_output=True, text=True, timeout=30, check=False,
        )
        out = (p.stdout or "").strip()
        if out:
            ms = int(float(out) * 1000)
    except Exception:
        ms = None
    _DURATION_CACHE[key] = ms
    return ms


# Share of cues that must land within the video runtime for an alignment to be
# accepted. Tolerant of a small mis-placed OP/ED/preview tail (alass can
# over-shift a tiny trailing block) while still rejecting gross breakage.
_ALIGN_MIN_IN_RANGE = 0.85


def _in_range_fraction(sub_path: Path, duration_ms: Optional[int]) -> Optional[float]:
    """Fraction of cues whose start lands within [-2s, duration+2s]. None when we
    can't measure (no duration) — the caller then accepts any produced output."""
    if not duration_ms:
        return None
    try:
        subs = _load_subs(sub_path)
    except Exception:
        return None
    ev = [e for e in subs if not e.is_comment and (e.text or "").strip()]
    if not ev:
        return 0.0
    lo, hi = -2000, duration_ms + 2000
    in_range = sum(1 for e in ev if lo <= int(e.start) <= hi)
    return in_range / len(ev)


def _run_aligner(cmd: list[str], out_path: Path) -> bool:
    """Run an aligner subprocess; True iff it produced a non-empty output file."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=600, check=False)
    except Exception as e:  # FileNotFound, Timeout, …
        log.warning("align: %s failed to run (%s)", Path(cmd[0]).name, e)
        return False
    if p.returncode == 0 and out_path.exists() and out_path.stat().st_size > 0:
        return True
    log.info("align: %s rc=%s (no usable output)", Path(cmd[0]).name, p.returncode)
    return False


# --------------------------------------------------------------------------
# Objective alignment scoring — reference-free verification
# --------------------------------------------------------------------------
#
# A correctly-aligned JA sub places its cues where the dialogue actually is. We
# score a candidate against the best available ground truth, preferring the
# cleaner signal, so alignment is *verified*, never assumed:
#
#   * vs a trusted, video-timed reference subtitle (the embedded English —
#     muxed into the release, frame-accurate by construction): fraction of the
#     reference's *dialogue* cue-onsets (hygiene-filtered — see the reference
#     hygiene block below) that have a JA cue-onset within TOL. Timestamps
#     only (cheap) and highly discriminative (empirically ~0.38 broken → ~0.70
#     aligned for anime).
#   * else vs the video's own speech (webrtcvad): fraction of JA cue time that
#     lands on detected speech. Weaker for anime (BGM/music reads as speech) but
#     reference-free — the fallback until a reference exists.
#
# The aligner keeps whichever candidate scores highest and never regresses below
# the input, and it stores the score as a confidence so low-confidence episodes
# can be resurfaced when better data (video / English) arrives.

_ONSET_TOL_MS = 750
_VAD_FRAME_S = 0.03
_AUDIO_VAD_CACHE: dict = {}


def _cue_starts(path: Path) -> list[int]:
    try:
        return sorted(int(e.start) for e in _load_subs(path)
                      if not e.is_comment and (e.text or "").strip())
    except Exception:
        return []


# --- Reference hygiene ------------------------------------------------------
#
# Embedded "English" tracks are often typeset fansub scripts (ASS flattened to
# SRT): the dialogue is buried under karaoke frames (rolling sub-200ms cues,
# syllable by syllable) and vector-drawing commands ("m 12 34 l ..."), which
# concentrate in OP/ED storms where a dialogue sub correctly shows *nothing*.
# Used raw, such a track poisons everything downstream: onset agreement becomes
# ANTI-correlated with correctness — a perfectly-timed JA sub scores ~0.05
# (>90% of ref onsets are karaoke frames with no dialogue counterpart) while a
# sub shoved onto an OP/ED storm scores ~0.30 — so aligners "win" by piling
# dialogue cues onto karaoke clusters. (Re:Zero S3E01, 2026-07-21: a correct
# jimaku sub was progressively mangled across re-align passes while the score
# "improved" 0.30 -> 0.57.)
#
# Therefore every consumer of a reference — the onset scorer, alass-en, and the
# trust gate — sees only its *dialogue-like* cues: drawing commands and
# single-character glyphs dropped, rolling same-text runs merged,
# sub-_REF_MIN_CUE_MS leftovers dropped, and onset *storms* removed (typeset
# credit animations emit hundreds of cues per second — e.g. one cue per falling
# glyph, each a healthy 500ms long, so only density gives them away; dialogue
# never exceeds a few onsets per second). A reference reduced below
# _REF_MIN_DIALOGUE_CUES cannot anchor an episode and is not trusted at all
# (align_episode then waits, as with no reference).

_REF_MIN_CUE_MS = 300          # real dialogue cues are ≥300ms; karaoke frames ~40-100ms
_REF_MIN_DIALOGUE_CUES = 50    # fewer can't anchor a whole episode
_REF_MERGE_GAP_MS = 500        # same-text cues this close = one rolling emission
_REF_MAX_ROLL_FRAGMENTS = 3    # dialogue re-emits ≤3×; karaoke lines roll as dozens of frames
_REF_STORM_ONSETS_PER_S = 6    # more onsets in one second = typesetting, not speech
_REF_JUNK_ZONE_PER_S = 10      # ≥ this many *dropped* cues in a second = typeset zone
_REF_JUNK_ZONE_PAD_S = 2       # zone influence extends this far around the hot second
_DRAWING_RE = re.compile(
    r"^(?:\{[^}]*\}\s*)*m\s+-?\d+(?:\.\d+)?\s+-?\d+(?:\.\d+)?\s+[lbms]", re.IGNORECASE)


def _dialogue_events(path: Path) -> list[tuple[int, int, str]]:
    """(start_ms, end_ms, text) of a track's dialogue-like cues — see the
    reference-hygiene block above. Empty list when unparsable.

    Beyond the per-cue filters, two structural passes matter:
      * rolling-fragment cap — a lyric line rolled out as dozens of sub-100ms
        frames merges back into one healthy-looking cue, but the fragment count
        survives the merge and gives it away;
      * junk-zone exclusion — OP/ED karaoke blocks emit song-line *translations*
        (romaji + English) that pass every per-cue test, yet they always sit
        inside a region saturated with cues the other filters dropped (drawing
        frames, glyph storms). Seconds dense in dropped junk mark typeset
        zones; survivors starting there are lyrics, not dialogue. Without this,
        a cut difference around an OP gets "corrected" by alass onto the lyric
        anchors — dialogue rolling during the opening (Re:Zero S3E02).
    """
    try:
        subs = _load_subs(path)
    except Exception:
        return []
    junk_per_s: dict[int, int] = {}

    def _junk(start_ms: int, weight: int = 1) -> None:
        junk_per_s[start_ms // 1000] = junk_per_s.get(start_ms // 1000, 0) + weight

    ev: list[list] = []
    for e in subs:
        if e.is_comment:
            continue
        text = (e.text or "").strip()
        if not text:
            continue
        if len(text) < 2 or _DRAWING_RE.match(text):
            _junk(int(e.start))
            continue
        ev.append([int(e.start), int(e.end), text])
    ev.sort()
    merged: list[list] = []  # [start, end, text, fragments]
    for s, en, t in ev:
        if merged and merged[-1][2] == t and s - merged[-1][1] <= _REF_MERGE_GAP_MS:
            merged[-1][1] = max(merged[-1][1], en)  # rolling re-emission of the same line
            merged[-1][3] += 1
        else:
            merged.append([s, en, t, 1])
    kept: list[tuple[int, int, str]] = []
    for s, en, t, n in merged:
        if en - s >= _REF_MIN_CUE_MS and n <= _REF_MAX_ROLL_FRAGMENTS:
            kept.append((s, en, t))
        else:
            _junk(s, n)
    # onset-storm removal: drop every cue starting in a second whose onset count
    # is impossible for dialogue (typeset OP/ED animations, per-glyph credits).
    per_s: dict[int, int] = {}
    for s, _en, _t in kept:
        per_s[s // 1000] = per_s.get(s // 1000, 0) + 1
    survivors: list[tuple[int, int, str]] = []
    for s, en, t in kept:
        if per_s[s // 1000] <= _REF_STORM_ONSETS_PER_S:
            survivors.append((s, en, t))
        else:
            _junk(s)
    # junk-zone exclusion (see docstring): survivors starting near a second
    # saturated with dropped cues are song lines inside a typeset block.
    hot = {sec for sec, n in junk_per_s.items() if n >= _REF_JUNK_ZONE_PER_S}
    if not hot:
        return survivors
    return [(s, en, t) for s, en, t in survivors
            if not any((s // 1000 + d) in hot
                       for d in range(-_REF_JUNK_ZONE_PAD_S, _REF_JUNK_ZONE_PAD_S + 1))]


def _dialogue_starts(path: Path) -> list[int]:
    return sorted(s for s, _e, _t in _dialogue_events(path))


def _write_dialogue_ref(ref: Path, dest: Path) -> Optional[Path]:
    """Write the dialogue-only view of `ref` to `dest` (SRT) for use as an
    aligner reference. None when filtering leaves too little to anchor on or
    the write fails — callers then fall back to the raw reference."""
    ev = _dialogue_events(ref)
    if len(ev) < _REF_MIN_DIALOGUE_CUES:
        return None
    try:
        out = pysubs2.SSAFile()
        out.events = [pysubs2.SSAEvent(start=s, end=e, text=t) for s, e, t in ev]
        out.save(str(dest), format_="srt")
        return dest
    except Exception as e:
        log.info("align: could not write dialogue reference (%s)", e)
        return None


def _onset_agreement(ja_path: Path, ref_path: Path, tol_ms: int = _ONSET_TOL_MS) -> Optional[float]:
    """Fraction of the reference's *dialogue* cue-onsets that have a JA cue-onset
    within tol_ms. High => JA cues start when the dialogue starts (per the
    trusted reference). The reference side is hygiene-filtered (block above) so
    karaoke/typesetting storms can't dominate the denominator."""
    import bisect
    ref = _dialogue_starts(ref_path)
    ja = _cue_starts(ja_path)
    if not ref or not ja:
        return None
    hit = 0
    for s in ref:
        i = bisect.bisect_left(ja, s)
        near = min((abs(ja[j] - s) for j in (i - 1, i, i + 1) if 0 <= j < len(ja)), default=10 ** 9)
        if near <= tol_ms:
            hit += 1
    return hit / len(ref)


def _vad_speech_mask(video_path: Path) -> Optional[list[int]]:
    """Binary speech mask over 30ms frames of the video's audio (webrtcvad,
    aggressiveness 3), cached per (path, mtime). None if unavailable."""
    try:
        key = (str(video_path), video_path.stat().st_mtime_ns)
    except Exception:
        return None
    if key in _AUDIO_VAD_CACHE:
        return _AUDIO_VAD_CACHE[key]
    mask: Optional[list[int]] = None
    tmp_wav = None
    try:
        import tempfile
        import wave
        import webrtcvad
        tmp_wav = Path(tempfile.gettempdir()) / f"_migaku_vad_{abs(hash(key)) & 0xffffffff}.wav"
        p = subprocess.run(
            ["ffmpeg", "-y", "-i", str(video_path), "-ac", "1", "-ar", "16000",
             "-f", "wav", str(tmp_wav)],
            capture_output=True, timeout=240, check=False,
        )
        if p.returncode == 0 and tmp_wav.exists():
            w = wave.open(str(tmp_wav), "rb")
            sr = w.getframerate()
            pcm = w.readframes(w.getnframes())
            w.close()
            vad = webrtcvad.Vad(3)
            fs = int(sr * _VAD_FRAME_S) * 2  # bytes per 30ms frame (16-bit mono)
            m = []
            for i in range(0, len(pcm) - fs + 1, fs):
                try:
                    m.append(1 if vad.is_speech(pcm[i:i + fs], sr) else 0)
                except Exception:
                    m.append(0)
            mask = m
    except Exception as e:  # webrtcvad/ffmpeg missing, decode error, …
        log.info("vad: could not build speech mask for %s: %s", video_path, e)
    finally:
        if tmp_wav is not None:
            try:
                tmp_wav.unlink(missing_ok=True)
            except Exception:
                pass
    _AUDIO_VAD_CACHE[key] = mask
    return mask


def _vad_precision(ja_path: Path, video_path: Path) -> Optional[float]:
    """Fraction of JA cue time that lands on detected speech (reference-free)."""
    mask = _vad_speech_mask(video_path)
    if not mask:
        return None
    T = len(mask)
    try:
        subs = [e for e in _load_subs(ja_path)
                if not e.is_comment and (e.text or "").strip()]
    except Exception:
        return None
    cue_on = inter = 0
    for e in subs:
        a = int((e.start / 1000) / _VAD_FRAME_S)
        b = int((e.end / 1000) / _VAD_FRAME_S)
        for k in range(max(0, a), min(T, b)):
            cue_on += 1
            if mask[k]:
                inter += 1
    return (inter / cue_on) if cue_on else None


def alignment_score(ja_path: str | Path, *, reference_sub: Optional[str | Path] = None,
                    video_path: Optional[str | Path] = None) -> tuple[Optional[float], Optional[str]]:
    """Objective 0..1 alignment quality for a JA sub. Prefers the trusted
    reference (onset agreement); falls back to video-audio VAD. Returns
    (score, ref_kind) where ref_kind is 'en' | 'vad' | None."""
    if reference_sub and Path(reference_sub).exists():
        s = _onset_agreement(Path(ja_path), Path(reference_sub))
        if s is not None:
            return s, "en"
    if video_path and Path(video_path).exists():
        s = _vad_precision(Path(ja_path), Path(video_path))
        if s is not None:
            return s, "vad"
    return None, None


def align(sub_path: str | Path, video_path: str | Path,
          *, reference_sub: Optional[str | Path] = None
          ) -> tuple[bool, Optional[str], Optional[float], Optional[str]]:
    """Align `sub_path` in place, and return (changed, tool, score, ref_kind).

    Two regimes, because only a *trusted reference* yields a signal strong enough
    to safely choose between alignments:

      * VERIFIED (a trusted, video-timed reference sub exists — the embedded
        English): run alass→reference, alass→video and ffsubsync→video, and keep
        whichever maximises cue-onset agreement with the reference, never
        regressing below the input. Onset agreement reliably rejects a wild shift.

      * UNVERIFIED (no reference): audio VAD is too weak for anime (BGM/music read
        as speech) to *pick* between alignments — trusting it can push a good sub
        out of sync. So we just apply the robust default (alass→video, else
        ffsubsync), gate it on the in-range sanity check, and report the VAD score
        as a rough confidence only. `align_episode` won't even call this regime —
        it waits for a reference — so unverified alignment happens only at first
        fetch, where alass→video is the best available anyway.

    Best-effort — never raises.
    """
    sub_path = Path(sub_path)
    video_path = Path(video_path) if video_path else None
    ref = Path(reference_sub) if reference_sub else None
    if ref and (not ref.exists() or ref.resolve() == sub_path.resolve()):
        ref = None
    have_video = bool(video_path and video_path.exists())
    if not sub_path.exists() or (not have_video and not ref):
        return False, None, None, None

    duration_ms = _video_duration_ms(video_path) if have_video else None
    alass = _alass_cmd()

    def _run(tool: str, build) -> Optional[Path]:
        """Run one aligner to a temp file; return it iff it produced in-range output."""
        tmp = sub_path.with_name(sub_path.stem + f".{tool}.aligned.srt")
        tmp.unlink(missing_ok=True)
        if not _run_aligner(build(tmp), tmp):
            return None
        frac = _in_range_fraction(tmp, duration_ms)
        if frac is not None and frac < _ALIGN_MIN_IN_RANGE:
            log.info("align: %s rejected (%.0f%% in range)", tool, frac * 100)
            tmp.unlink(missing_ok=True)
            return None
        return tmp

    # ---- VERIFIED: pick the best cue-onset agreement with the trusted reference
    if ref:
        # alass anchors on the reference's cue structure — feed it the dialogue-
        # only view (reference hygiene above) so OP/ED karaoke storms can't
        # attract splits. Scoring uses the same view via _onset_agreement.
        ref_clean = _write_dialogue_ref(
            ref, sub_path.with_name(sub_path.stem + ".refdialogue.srt")) or ref
        attempts: list[tuple[str, "callable"]] = [
            ("alass-en", lambda out: alass + [str(ref_clean), str(sub_path), str(out), "--split-penalty", "7"])
        ] if alass else []
        if have_video and alass:
            attempts.append(("alass", lambda out: alass + [
                str(video_path), str(sub_path), str(out), "--split-penalty", "7"]))
        if have_video:
            attempts.append(("ffsubsync", lambda out: _ffsubsync_cmd() + [
                str(video_path), "-i", str(sub_path), "-o", str(out)]))
        best_score = _onset_agreement(sub_path, ref)  # input is the floor
        best_tool, best_file = "none", None
        tmps: list[Path] = []
        try:
            for tool, build in attempts:
                tmp = _run(tool, build)
                if tmp is None:
                    continue
                tmps.append(tmp)
                sc = _onset_agreement(tmp, ref)
                if sc is not None and (best_score is None or sc > best_score):
                    best_score, best_tool, best_file = sc, tool, tmp
            if best_file is not None:
                best_file.replace(sub_path)
                log.info("align: %s best for %s (onset=%.2f vs EN)", best_tool,
                         sub_path.name, best_score if best_score is not None else -1)
                return True, best_tool, best_score, "en"
        finally:
            for t in tmps:
                try:
                    t.unlink(missing_ok=True)
                except Exception:
                    pass
            if ref_clean != ref:
                try:
                    ref_clean.unlink(missing_ok=True)
                except Exception:
                    pass
        return False, None, best_score, "en"

    # ---- UNVERIFIED: robust default only (no VAD-driven selection)
    order: list[tuple[str, "callable"]] = []
    if alass:
        order.append(("alass", lambda out: alass + [
            str(video_path), str(sub_path), str(out), "--split-penalty", "7"]))
    order.append(("ffsubsync", lambda out: _ffsubsync_cmd() + [
        str(video_path), "-i", str(sub_path), "-o", str(out)]))
    for tool, build in order:
        tmp = _run(tool, build)
        if tmp is None:
            continue
        tmp.replace(sub_path)
        score = _vad_precision(sub_path, video_path)  # confidence hint only
        log.info("align: %s (unverified, vad=%.2f) for %s", tool,
                 score if score is not None else -1, sub_path.name)
        return True, tool, score, "vad"
    return False, None, _vad_precision(sub_path, video_path), "vad"


# --------------------------------------------------------------------------
# Ingest (parse -> corpus)
# --------------------------------------------------------------------------


def _clean_text(s: str) -> str:
    """Strip ASS override tags + drawing commands, collapse newlines.

    Language-agnostic markup cleanup — shared by the English pipeline. Japanese
    broadcast-caption cleanup lives in `clean_ja_caption` (see below); keep this
    one free of JP-specific rules so English (which legitimately uses `(...)` for
    SDH / parentheticals) is never mangled.
    """
    if not s:
        return ""
    s = re.sub(r"\{[^}]*\}", "", s)          # {\an8} etc. (ASS override tags)
    s = re.sub(r"<[^>]+>", "", s)            # <font…>/<i>/<b> (ffmpeg ASS→SRT markup)
    s = s.replace(r"\N", " ").replace(r"\n", " ").replace(r"\h", " ")
    s = s.replace("\n", " ")
    return re.sub(r"\s+", " ", s).strip()


# --- Japanese broadcast-caption (ARIB / TV rip) cleanup -------------------
#
# jimaku subtitles are ripped from Japanese TV broadcasts, so — unlike the clean
# streaming/BD English tracks — they carry broadcast-caption ANNOTATIONS that are
# noise for both display (Migaku shows the .srt verbatim) and the study corpus
# (tokens / comprehension / FTS / furigana). These conventions are universal
# across series, so stripping them is general, not a per-show patch:
#
#   （話者） / （カッターナイフで切り裂く音）  speaker labels & sound-effect
#                                                   captions          -> drop whole
#   《…》 〈…〉        inner-monologue / narration brackets -> unwrap (KEEP the text)
#   →               line-continuation marker (cue is continued next) -> drop marker
#   ♪ ♬ ♩ ♫         song / music markers                             -> drop marker
#   登場人物(とうじょうじんぶつ)  inline furigana in half-width () -> drop reading
#   【提供】         lenticular labels (sponsor / section tags)       -> drop whole
#   　 (U+3000)      full-width caption spacing between JP characters  -> remove
#
# Deliberately PRESERVED (these are real Japanese text, not annotations):
#   「…」 『…』       Japanese quotation marks (e.g. 次回「…」 episode titles)
#   ● ■ 〇           on-air censoring of a word (バ●ス) — carries content
#   。、！？…〜       ordinary Japanese punctuation
#
# The English pipeline keeps using `_clean_text` and is untouched.
_JA_MUSIC_RE = re.compile(r"[♩♪♫♬🎵🎶〽]")
_JA_ARROW_RE = re.compile(r"[→⟶⇒⇨➡➔⤴⤵]")
# Parenthetical annotation = speaker label, sound-effect caption, or inline
# furigana reading — remove the group AND its contents. Matches either width on
# each side (rips can mix full-width and half-width brackets) and the
# negated class keeps one group from swallowing across a neighbour. Applied in a
# loop so nested groups (（頬(ほお)を…）) fully unwind. NB: this intentionally
# removes half-width parentheticals too ((笑)/(CV:…)) — in a TV-caption rip they
# are annotations, never dialogue.
_JA_PAREN_RE = re.compile(r"[（(][^（）()]*[）)]")
# 【…】 lenticular labels (提供/字幕 etc.) — annotation, not dialogue.
_JA_LENTICULAR_RE = re.compile(r"【[^】]*】")
# Dangling brackets left when a （…）/【…】 annotation is SPLIT across two cues
# (this cue holds only one side, e.g. 「荒い息）」). Stripped after the paired
# removals above. Deliberately excludes 「」『』 — those are real JP quotes.
_JA_ORPHAN_BRACKET_RE = re.compile(r"[（）()【】]")
# Narration / monologue brackets — unwrap: the text inside IS dialogue.
_JA_ANGLE_RE = re.compile(r"[《》〈〉]")
# Caption spacing between two non-ASCII (CJK/kana) chars — Japanese has no inter-
# word spaces, so these are layout artifacts. Spaces around ASCII (romaji) stay.
_JA_CJK_SPACE_RE = re.compile(r"(?<=[^\x00-\x7F])[ 　]+(?=[^\x00-\x7F])")


def clean_ja_caption(s: str) -> str:
    """Clean one Japanese broadcast-caption cue for display + corpus ingest.

    Builds on `_clean_text` (tags/newlines) then strips the TV-caption annotation
    conventions documented above, keeping legitimate Japanese text/punctuation.
    Returns "" when nothing linguistic remains (music stings like ``♪～``, a
    pure sound-effect caption, or a fully-censored line) so the caller drops the
    empty cue. General across all series — encodes no show-specific rule.
    """
    s = _clean_text(s)
    if not s:
        return ""
    s = _JA_MUSIC_RE.sub("", s)               # drop ♪♬ song / music markers
    s = _JA_ARROW_RE.sub("", s)               # drop → line-continuation markers
    # drop （speaker）/（SFX）/(furigana) groups, unwinding nesting, then any
    # dangling bracket left by an annotation split across a cue boundary.
    prev = None
    while prev != s:
        prev = s
        s = _JA_PAREN_RE.sub("", s)
        s = _JA_LENTICULAR_RE.sub("", s)
    s = _JA_ORPHAN_BRACKET_RE.sub("", s)
    s = _JA_ANGLE_RE.sub("", s)               # unwrap 《…》/〈…〉 — keep the dialogue
    s = _JA_CJK_SPACE_RE.sub("", s)           # remove caption spacing between JP chars
    s = re.sub(r"\s+", " ", s).strip()
    # Content gate: drop anything with no kana/kanji/latin/digit left (a stray
    # wave-dash from ``♪～``, a fully-censored ●●● line, lone punctuation …).
    if not learn_service._is_content(s):
        return ""
    return s


def _detect_format(path: Path) -> str:
    suf = path.suffix.lower().lstrip(".")
    if suf in ("srt", "ass", "ssa", "vtt"):
        return "ass" if suf == "ssa" else suf
    return "srt"


def _load_subs(path: Path):
    """Load a subtitle file, detecting its encoding. jimaku files aren't always
    UTF-8 (UTF-16/Shift-JIS/EUC-JP appear), which used to crash the parser."""
    raw = path.read_bytes()
    if raw[:3] == b"\xef\xbb\xbf":
        enc = "utf-8-sig"
    elif raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        enc = "utf-16"
    else:
        enc = "utf-8"
        try:
            import chardet
            guess = chardet.detect(raw)
            if guess and guess.get("encoding") and (guess.get("confidence") or 0) >= 0.6:
                enc = guess["encoding"]
        except Exception:
            pass
    try:
        return pysubs2.load(str(path), encoding=enc)
    except (UnicodeDecodeError, LookupError):
        return pysubs2.SSAFile.from_string(raw.decode("utf-8", errors="replace"))


def _drop_non_ja_twins(cues: list[tuple[int, int, str]]) -> list[tuple[int, int, str]]:
    """Drop translation twins from a bilingual track: a cue with no Japanese that
    shares its exact (start, end) with a cue that HAS Japanese is the other
    language's copy of the same line (JP+Chinese dual-language rips time both
    copies identically). Surgical by design — keyed on exact timestamps only, so
    ordinary non-Japanese lines (romaji, English inserts) and genuinely
    simultaneous dialogue are untouched."""
    ja_windows = {(s, e) for (s, e, t) in cues if _has_japanese(t)}
    return [c for c in cues if _has_japanese(c[2]) or (c[0], c[1]) not in ja_windows]


def ingest_subtitle(episode_id: int, sub_path: str | Path,
                    *, source: str = "jimaku",
                    jimaku_entry_id: Optional[int] = None,
                    jimaku_file_id: Optional[int] = None,
                    jimaku_filename: Optional[str] = None,
                    aligned: int = 0, align_tool: Optional[str] = None,
                    align_score: Optional[float] = None,
                    align_ref: Optional[str] = None) -> int:
    """Parse a subtitle file and load it into the corpus.

    Inserts a `subtitles` row, then `subtitle_lines` (idx/start/end/text +
    furigana), tokenizes each line into `line_lemmas` (token_source='local'),
    and mirrors text into `subtitle_fts`. Idempotent per (episode, file): a
    re-ingest of the same (episode_id, source, jimaku_file_id) replaces its
    lines/lemmas/fts rather than duplicating.

    Returns the subtitle_id.
    """
    sub_path = Path(sub_path)
    if not sub_path.exists():
        raise RuntimeError(f"subtitle not found: {sub_path}")

    fmt = _detect_format(sub_path)
    subs = _load_subs(sub_path)

    if jimaku_filename is None:
        jimaku_filename = sub_path.name

    # ---- compute everything expensive OUTSIDE the write transaction ----------
    # The Migaku sidecar tokenize is a network call (up to 180s) and furigana is
    # CPU-bound; doing them INSIDE the write transaction held SQLite's single
    # write lock the whole time, so concurrent writers (the poll_downloads
    # periodic, API routes, other workers) hit 'database is locked'. Build the
    # cleaned cues, tokens and furigana first; the transaction below does only
    # the fast INSERTs.
    #
    # Clean each cue of Japanese broadcast-caption artifacts (（speaker）/
    # 《narration》/→/♪/furigana/full-width spacing). Cues that reduce to nothing
    # linguistic (pure SFX/music captions) are dropped.
    cues: list[tuple[int, int, str]] = []          # (start_ms, end_ms, text)
    for ev in subs:
        if ev.is_comment:
            continue
        text = clean_ja_caption(ev.text)
        if not text:
            continue
        cues.append((int(ev.start), int(ev.end), text))

    # Exact duplicates (same start, end AND text) are styling artifacts — ASS
    # rips commonly layer the identical event twice for border/glow effects.
    # Keep the first; dropping a verbatim copy can never lose content.
    _seen: set[tuple[int, int, str]] = set()
    cues = [c for c in cues if not (c in _seen or _seen.add(c))]

    # Bilingual rips (jimaku carries many JP+Chinese dual-language files) repeat
    # every line in a second language at the same timestamps — in the JAPANESE
    # study corpus/display those twins are duplicates, not dialogue. Drop them.
    cues = _drop_non_ja_twins(cues)

    specs: list[tuple[int, int, int, str]] = [     # (idx, start_ms, end_ms, text)
        (i, s, e, t) for i, (s, e, t) in enumerate(cues)
    ]

    # Structural sanity of what we're about to make the study corpus: a track
    # whose cleaned cues still heavily self-overlap will display stacked lines in
    # the player and double Moments/transcript rows. A corpus is still better
    # than no corpus (comprehension tolerates it), so ingest proceeds — but warn
    # loudly instead of shipping the problem silently.
    _frac = _overlap_fraction_of([(s, e) for (_, s, e, _) in specs])
    if _frac is not None and _frac > _MAX_OVERLAP_FRACTION:
        log.warning("ingest: subtitle for ep %s self-overlaps (%.0f%% of subtitled "
                    "time has 2+ simultaneous cues) — will display stacked lines",
                    episode_id, _frac * 100)
        _record_event(
            "subtitle", f"Self-overlapping subtitle for {_ep_label(episode_id)}", "warning",
            detail=f"{_frac:.0%} of subtitled time has 2+ simultaneous cues — "
                   "duplicated/conflicting timings; lines will stack in the player",
            meta={"episode_id": episode_id, "overlap_fraction": round(_frac, 3)},
        )

    # Batch-tokenize with Migaku's own analyzer (sidecar) for Migaku-exact
    # dictForms; fall back to the local fugashi tokenizer if it's unavailable.
    from ..learn import migaku_tok
    _texts = [t if _has_japanese(t) else "" for (_, _, _, t) in specs]
    _mig = migaku_tok.tokenize_lines(_texts)
    _tok_source = "migaku-local" if _mig is not None else "local"
    if _mig is not None:
        log.info("tokenized %d lines via migaku sidecar (ep %s)", len(_texts), episode_id)

    per_line: list[tuple[Optional[str], list[dict]]] = []  # (furigana, tokens) per spec
    for i, (_, _, _, text) in enumerate(specs):
        try:
            fg = learn_service.furigana(text)
        except Exception:
            fg = None
        if _mig is not None:
            # Migaku tokens: map dictForm->lemma; drop symbol-only tokens
            # (…, →, ♬, 《》 …) which Migaku emits with an empty pos. _PUNCT_RE
            # misses the General-Punct/Arrows/Misc-Symbols blocks, so gate on
            # linguistic content (kana/kanji/alnum) — see _is_content.
            toks = [
                {
                    "lemma": t.get("dictForm") or t.get("surface") or "",
                    "reading": t.get("reading") or "",
                    "pos": t.get("pos") or "",
                    "surface": t.get("surface") or "",
                }
                for t in (_mig[i] or [])
                if learn_service._is_content(t.get("dictForm"), t.get("surface"))
            ]
        else:
            try:
                toks = learn_service.tokenize(text) if _has_japanese(text) else []
            except Exception as e:
                log.warning("tokenize failed on line %s: %s", i, e)
                toks = []
        per_line.append((fg, toks))

    # ---- short write transaction: idempotency, purge, inserts ----------------
    line_rows: list[tuple] = []
    with cursor() as cx:
        # idempotency: find an existing subtitle row for this (episode, source, file).
        # Match on jimaku_file_id when we have one; otherwise fall back to path.
        if jimaku_file_id is not None:
            existing = cx.execute(
                "SELECT id FROM subtitles WHERE episode_id=? AND source=? AND jimaku_file_id=?",
                (episode_id, source, jimaku_file_id),
            ).fetchone()
        else:
            existing = cx.execute(
                "SELECT id FROM subtitles WHERE episode_id=? AND source=? AND path=?",
                (episode_id, source, str(sub_path)),
            ).fetchone()
        if existing:
            sub_id = existing["id"]
            _purge_fts_for_subs(cx, [sub_id])          # drop this row's FTS rows
            cx.execute("DELETE FROM subtitle_lines WHERE subtitle_id=?", (sub_id,))  # cascade lemmas
            cx.execute(
                "UPDATE subtitles SET jimaku_entry_id=?, jimaku_filename=?, path=?, "
                "format=?, aligned=?, align_tool=?, align_score=?, align_ref=?, "
                "version=version+1 WHERE id=?",
                (jimaku_entry_id, jimaku_filename, str(sub_path), fmt, aligned, align_tool,
                 align_score, align_ref, sub_id),
            )
        else:
            cur = cx.execute(
                "INSERT INTO subtitles(episode_id,source,jimaku_entry_id,jimaku_file_id,"
                "jimaku_filename,lang,path,format,aligned,align_tool,align_score,align_ref,version) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,1)",
                (episode_id, source, jimaku_entry_id, jimaku_file_id, jimaku_filename,
                 "ja", str(sub_path), fmt, aligned, align_tool, align_score, align_ref),
            )
            sub_id = cur.lastrowid

        # Enforce ONE Japanese study corpus per episode: drop any OTHER non-EN
        # subtitle rows. A re-fetch that picked a DIFFERENT jimaku file would
        # otherwise leave the previous file's lines/lemmas behind, double-counting
        # every token in comprehension + Moments (the playback layer only reads
        # one, so the skew was invisible). NB: the clause must match what readers
        # (`_ja_lines`, comprehension) treat as the JA corpus — COALESCE, not
        # lang='ja' — or rows outside the stricter clause survive as permanent
        # duplicates.
        stale = [
            r["id"] for r in cx.execute(
                "SELECT id FROM subtitles WHERE episode_id=? "
                "AND COALESCE(lang,'ja') <> 'en' AND id<>?",
                (episode_id, sub_id),
            ).fetchall()
        ]
        if stale:
            _purge_fts_for_subs(cx, stale)
            qmarks = ",".join("?" * len(stale))
            cx.execute(f"DELETE FROM subtitles WHERE id IN ({qmarks})", stale)  # cascade lines+lemmas
            log.info("ingest: purged %d stale JA subtitle set(s) for episode %s", len(stale), episode_id)

        # insert cleaned lines + their precomputed furigana / lemmas / fts
        for i, (idx, start, end, text) in enumerate(specs):
            cur = cx.execute(
                "INSERT INTO subtitle_lines(subtitle_id,episode_id,idx,start_ms,end_ms,text) "
                "VALUES(?,?,?,?,?,?)",
                (sub_id, episode_id, idx, start, end, text),
            )
            line_id = cur.lastrowid
            line_rows.append((sub_id, episode_id, idx, start, end, text))
            fg, toks = per_line[i]
            if fg:
                cx.execute("UPDATE subtitle_lines SET text_furigana=? WHERE id=?", (fg, line_id))
            if toks:
                cx.executemany(
                    "INSERT INTO line_lemmas(line_id,episode_id,lemma,reading,pos,surface,token_source) "
                    "VALUES(?,?,?,?,?,?,?)",
                    [
                        (line_id, episode_id, t["lemma"], t["reading"], t["pos"], t["surface"], _tok_source)
                        for t in toks
                    ],
                )
            cx.execute(
                "INSERT INTO subtitle_fts(text, line_id, episode_id) VALUES(?,?,?)",
                (text, line_id, episode_id),
            )

    # Rewrite the on-disk .srt from the cleaned cues so what Migaku displays
    # matches the corpus (it streams this file verbatim — see media.router). Uses
    # the same cleaned text + original alignment timings; drops the emptied
    # SFX/music cues. Also normalises any raw-ASS-in-.srt to real SRT. Best-effort
    # — a failed rewrite must never fail the (already-committed) ingest.
    _rewrite_clean_srt(sub_path, line_rows)

    log.info("ingested %d lines for episode %s from %s", len(line_rows), episode_id, sub_path.name)
    return sub_id


def _purge_fts_for_subs(cx, sub_ids: list[int]) -> None:
    """Delete subtitle_fts rows for every line belonging to `sub_ids`.

    subtitle_fts is a plain (non-content) FTS5 table with no delete triggers, so
    dropping subtitle_lines (even via cascade) leaves its FTS rows orphaned. Call
    this before deleting the lines/subtitles to keep the index in sync.
    """
    if not sub_ids:
        return
    q = ",".join("?" * len(sub_ids))
    line_ids = [
        r["id"] for r in cx.execute(
            f"SELECT id FROM subtitle_lines WHERE subtitle_id IN ({q})", sub_ids
        ).fetchall()
    ]
    if line_ids:
        lq = ",".join("?" * len(line_ids))
        cx.execute(f"DELETE FROM subtitle_fts WHERE line_id IN ({lq})", line_ids)


def _rewrite_clean_srt(sub_path: Path, line_rows: list[tuple]) -> None:
    """Overwrite `sub_path` with an SRT built from the cleaned ingest rows
    (row = (sub_id, episode_id, idx, start_ms, end_ms, text)). Best-effort."""
    try:
        out = pysubs2.SSAFile()
        for row in line_rows:
            out.append(pysubs2.SSAEvent(start=int(row[3]), end=int(row[4]), text=row[5]))
        if len(out):
            out.save(str(sub_path), format_="srt")
    except Exception as e:  # pragma: no cover — display nicety, not critical
        log.warning("could not rewrite cleaned srt %s: %s", sub_path, e)


# --------------------------------------------------------------------------
# Full pipeline
# --------------------------------------------------------------------------


def _episode_row(episode_id: int) -> Optional[dict]:
    with connect() as cx:
        row = cx.execute(
            "SELECT e.id, e.anilist_id, e.ep_number, e.video_path, "
            "t.romaji AS romaji, t.english AS english "
            "FROM episodes e JOIN titles t ON t.anilist_id = e.anilist_id WHERE e.id=?",
            (episode_id,),
        ).fetchone()
    return dict(row) if row else None


def _sub_dest(ep: dict) -> Path:
    """Where to write the .ja.srt: next to the video if we have one (so Migaku
    smart-pairs by filename), else into the library/inbox area."""
    vp = ep.get("video_path")
    if vp:
        v = Path(vp)
        return v.with_suffix("").with_suffix(".ja.srt")
    # fallback path keyed by episode id
    base = settings.library_dir / "_subs"
    return base / f"episode_{ep['id']}.ja.srt"


# How many ranked candidates to try aligning before settling for the top pick.
# alass fixes most timing, so this rarely goes past the first; the cap just
# bounds the cost (alass re-extracts the video's audio per attempt) when a
# title's leading candidates happen to be unalignable.
_MAX_ALIGN_CANDIDATES = 3


def _release_hint(ep: dict) -> str:
    """Best-effort source-release string for ranking candidates. The organized
    video filename has had its [group]/resolution/source tokens stripped, but the
    original download still carries them — use the title's most descriptive
    completed/postprocessed download name, plus the romaji as a fallback. Soft
    signal only: `align()` re-syncs whatever we end up picking."""
    parts: list[str] = []
    try:
        with connect() as cx:
            row = cx.execute(
                "SELECT title_guess FROM downloads "
                "WHERE anilist_id=? AND title_guess IS NOT NULL AND title_guess <> '' "
                "AND state IN ('postprocessed','completed','seeding') "
                "ORDER BY length(title_guess) DESC LIMIT 1",
                (ep.get("anilist_id"),),
            ).fetchone()
        if row and row["title_guess"]:
            parts.append(row["title_guess"])
    except Exception:
        pass
    if ep.get("romaji"):
        parts.append(ep["romaji"])
    return " ".join(parts)


# An alignment score at/above this (against the best reference kind) is "good
# enough" — a re-evaluation triggered by unrelated data won't redo it.
_ALIGN_GOOD = 0.6
# Below this, even the best candidate is untrustworthy (unalignable cut, or a
# reference/JA that don't correspond) — keep it best-effort but flag it.
_ALIGN_LOW = 0.45


def sub_fits_video(sub_path: str | Path, video_path: Optional[str | Path]) -> bool:
    """True if a subtitle's timeline plausibly fits the video — the track's BODY
    (95th-percentile cue onset) must not start >60s past the runtime. Used to
    reject a mismatched / wrong-episode / corrupt track (as a JA-alignment
    reference or as an English display track).

    Judging by the single LAST cue proved too brittle: aligners routinely
    over-shift a tiny trailing block (an OP/ED/next-episode-preview tail), and a
    ~98%-correct track would hard-fail on cues that could never display anyway
    (they start after the video ends). A genuinely wrong-cut / double-length
    track has a large share of its cues out of range and still fails the
    percentile. Returns True when the duration can't be measured (can't
    disprove -> don't reject)."""
    if not video_path:
        return True
    dur = _video_duration_ms(Path(video_path))
    if not dur:
        return True
    starts = _cue_starts(Path(sub_path))  # sorted
    if not starts:
        return True
    p95 = starts[min(len(starts) - 1, max(0, math.ceil(len(starts) * 0.95) - 1))]
    return p95 <= dur + 60_000


# Max share of subtitled time that may be covered by 2+ simultaneous cues before
# a track counts as structurally broken. This documented heuristic permits
# limited benign overlap (including SDH stacking and dual-speaker cues) while
# rejecting tracks with sustained conflicting timelines.
_MAX_OVERLAP_FRACTION = 0.25


def sub_overlap_fraction(sub_path: str | Path) -> Optional[float]:
    """Fraction of this track's subtitled time covered by >=2 simultaneous cues.

    ~0 for a sane single-language dialogue track; high for structural breakage
    that a runtime-range check can never catch: a track that contains the same
    dialogue twice at conflicting timings (bad double-episode rips, an aligner
    "folding" an over-long track into range, a botched merge), or a bilingual
    file carrying every line in two languages. None when unmeasurable.
    """
    try:
        subs = _load_subs(Path(sub_path))
    except Exception:
        return None
    ev = sorted(
        (int(e.start), int(e.end)) for e in subs
        if not e.is_comment and (e.text or "").strip() and int(e.end) > int(e.start)
    )
    if not ev:
        return None
    return _overlap_fraction_of(ev)


def _overlap_fraction_of(intervals: list[tuple[int, int]]) -> Optional[float]:
    """Sweep-line: (time covered by >=2 intervals) / (time covered by >=1)."""
    pts: list[tuple[int, int]] = []
    for s, e in intervals:
        if e > s:
            pts.append((s, 1))
            pts.append((e, -1))
    if not pts:
        return None
    pts.sort()
    depth = 0
    last: Optional[int] = None
    covered = multi = 0
    for t, d in pts:
        if last is not None and t > last:
            if depth >= 1:
                covered += t - last
            if depth >= 2:
                multi += t - last
        depth += d
        last = t
    return (multi / covered) if covered else None


def sub_display_sane(sub_path: str | Path, video_path: Optional[str | Path]
                     ) -> tuple[bool, Optional[str]]:
    """The single acceptance gate for any subtitle track we display or trust as
    an alignment reference: it must fit the video's runtime AND not be
    self-overlapping (two cues on screen at once, one of them mistimed).

    Range and structure are orthogonal failure modes — a wrong-cut track can be
    *shifted into range* by an aligner (alass' split mode legitimately folds
    timelines), after which only the overlap check still catches it. Both checks
    are can't-disprove-tolerant: unmeasurable -> accepted.

    Returns (ok, reason) — reason is a short log/event string when not ok.
    """
    if not sub_fits_video(sub_path, video_path):
        return False, "timeline exceeds video runtime (wrong episode/cut?)"
    frac = sub_overlap_fraction(sub_path)
    if frac is not None and frac > _MAX_OVERLAP_FRACTION:
        return False, (f"self-overlapping track ({frac:.0%} of subtitled time has "
                       f"2+ simultaneous cues — duplicated/conflicting timings)")
    return True, None


def _reference_sub_for(ep: dict) -> Optional[Path]:
    """A trusted, video-timed English sidecar to align + score the JA study sub
    against — the embedded softsub (muxed into the release, frame-accurate by
    construction) or an AnimeTosho official track. NOT machine translation (it's
    derived from JA timing, so it can't verify JA). Returns <base>.en.srt or None.

    On the first pass there's no EN `subtitles` row yet, but the embedded sidecar
    is already on disk (media post-process extracts it *before* the JA fetch is
    enqueued), so a missing row is treated as the trusted embedded case."""
    vp = ep.get("video_path")
    if not vp:
        return None
    en = Path(vp).with_suffix("").with_suffix(".en.srt")
    if not (en.exists() and en.stat().st_size > 0):
        return None
    try:
        with connect() as cx:
            row = cx.execute(
                "SELECT source FROM subtitles WHERE episode_id=? AND lang='en' "
                "ORDER BY version DESC LIMIT 1", (ep["id"],),
            ).fetchone()
    except Exception:
        row = None
    if row and (row["source"] or "") == "mt":
        return None  # machine translation is not ground truth for JA timing

    # Trust the reference only if it passes the full display-sanity gate: it must
    # fit THIS video's runtime AND not be self-overlapping. A mismatched / corrupt
    # sidecar (wrong episode, double-length concat, bad framerate) OR a doubled
    # track (original + shifted copy of the same dialogue) would otherwise become
    # a false "ground truth" and drag a good JA sub out of sync — ignore it and
    # fall back to video-audio alignment. This catches both an overlong reference
    # and an in-range reference containing every line twice.
    ok, why = sub_display_sane(en, vp)
    if not ok:
        log.info("align: EN reference for ep %s untrusted (%s) — ignoring",
                 ep.get("id"), why)
        return None
    # A track that is nearly all typesetting/karaoke (reference hygiene block)
    # has too few dialogue-like cues left to anchor an episode — not a usable
    # ground truth, even though it displays fine.
    n_dialogue = len(_dialogue_events(en))
    if n_dialogue < _REF_MIN_DIALOGUE_CUES:
        log.info("align: EN reference for ep %s untrusted (only %d dialogue-like "
                 "cues after hygiene filtering) — ignoring", ep.get("id"), n_dialogue)
        return None
    return en


def fetch_for_episode(episode_id: int) -> dict:
    """Full subtitle pipeline for one episode. Returns a summary dict.

    Best-effort and idempotent: re-running re-fetches and re-ingests.
    """
    ep = _episode_row(episode_id)
    if not ep:
        raise RuntimeError(f"episode {episode_id} not found")
    anilist_id = ep["anilist_id"]
    ep_number = ep["ep_number"]

    entries = jimaku_search(anilist_id)
    if not entries:
        return {"episode_id": episode_id, "status": "no_entries", "anilist_id": anilist_id}

    # try entries in order until one yields files for this episode
    chosen_entry = None
    files: list[dict] = []
    for entry in entries:
        eid = entry.get("id")
        if eid is None:
            continue
        try:
            fs = jimaku_files(eid, ep_number)
        except Exception as e:
            log.warning("jimaku_files(%s,%s) failed: %s", eid, ep_number, e)
            continue
        if fs:
            chosen_entry = entry
            files = fs
            break

    if not files:
        return {"episode_id": episode_id, "status": "no_files", "anilist_id": anilist_id,
                "entry_id": chosen_entry.get("id") if chosen_entry else None}

    prefer = _release_hint(ep)
    # Whether THIS episode is a re-edited cut — from its own identity (organized
    # filename + title), never the release hint: a season-pack name can list
    # "Director's Cut" among its contents without this episode being one.
    vp = ep.get("video_path")
    ident = " ".join(x for x in ((Path(vp).name if vp else ""), ep.get("romaji") or "") if x)
    ranked = rank_files(files, prefer=prefer, video_is_recut=_is_recut(ident))
    if not ranked:
        return {"episode_id": episode_id, "status": "no_pick", "anilist_id": anilist_id}

    dest = _sub_dest(ep)
    ref = _reference_sub_for(ep)
    can_verify = bool(ep.get("video_path")) or bool(ref)

    # Self-verifying candidate SELECTION. When we can score alignment (a video or
    # a trusted reference exists), download each top-ranked candidate, align it,
    # and keep the one that objectively best matches the dialogue — this is how we
    # pick the *right* file (e.g. rejecting a PAL/25fps sub that won't sync)
    # instead of trusting filename tokens. Without any reference we can't verify
    # timing yet, so we take the top-ranked pick — comprehension still works (it
    # doesn't need alignment) and `align_episode` re-evaluates once a video /
    # English arrives.
    best = ranked[0]
    aligned, tool, score, ref_kind = 0, None, None, None
    if can_verify:
        winner: Optional[Path] = None
        for i, cand in enumerate(ranked[:_MAX_ALIGN_CANDIDATES]):
            tmp = dest.with_name(dest.stem + f".cand{i}.srt")
            try:
                download_subtitle(cand, tmp)
            except Exception as e:
                log.warning("subs: cand %d download failed ep %s: %s", i, episode_id, e)
                tmp.unlink(missing_ok=True)
                continue
            changed, t, sc, rk = align(tmp, ep.get("video_path"), reference_sub=ref)
            log.info("subs: ep %s cand %d/%d (%s) score=%.2f tool=%s", episode_id,
                     i + 1, len(ranked[:_MAX_ALIGN_CANDIDATES]), _file_name(cand),
                     sc if sc is not None else -1.0, t)
            if sc is not None and (score is None or sc > score):
                best, aligned, tool, score, ref_kind = cand, int(bool(changed)), t, sc, rk
                if winner and winner != tmp:
                    winner.unlink(missing_ok=True)
                winner = tmp
            elif winner is None and score is None:
                best, aligned, tool, winner = cand, int(bool(changed)), t, tmp
            else:
                tmp.unlink(missing_ok=True)
        if winner is not None:
            winner.replace(dest)
        else:
            download_subtitle(best, dest)  # nothing scored — best-effort top pick
    else:
        download_subtitle(best, dest)

    sub_id = ingest_subtitle(
        episode_id, dest,
        source="jimaku",
        jimaku_entry_id=chosen_entry.get("id") if chosen_entry else None,
        jimaku_file_id=best.get("id"),
        jimaku_filename=_file_name(best),
        aligned=aligned, align_tool=tool, align_score=score, align_ref=ref_kind,
    )

    # compute aligned comprehension now that the corpus exists (best-effort)
    comp = None
    try:
        comp = learn_service.comprehension_aligned(episode_id)
    except Exception as e:
        log.warning("comprehension_aligned failed for ep %s: %s", episode_id, e)

    with connect() as cx:
        nlines = cx.execute(
"SELECT COUNT(*) AS n FROM subtitle_lines WHERE subtitle_id=?", (sub_id,)
        ).fetchone()["n"]

    return {
        "episode_id": episode_id,
        "status": "ok",
        "anilist_id": anilist_id,
        "entry_id": chosen_entry.get("id") if chosen_entry else None,
        "file": _file_name(best),
        "subtitle_id": sub_id,
        "lines": nlines,
        "aligned": bool(aligned),
        "align_tool": tool,
        "align_score": score,
        "align_ref": ref_kind,
        "comprehension_pct": comp.comprehension_pct if comp else None,
        "comprehension_rating": comp.rating if comp else None,
    }


# --------------------------------------------------------------------------
# Whole-title fetch (all episodes from one search + files call)
# --------------------------------------------------------------------------


def fetch_for_title(anilist_id: int) -> dict:
    """Fetch + ingest jimaku subtitles for ALL episodes of a title in one pass:
    one search + one files call for the whole entry, then download/ingest each
    episode's sub (skipping episodes already ingested). Far fewer jimaku API
    calls than per-episode fetching."""
    import anitopy

    entries = jimaku_search(anilist_id)
    if not entries:
        return {"anilist_id": anilist_id, "status": "no_entries", "episodes": 0}

    chosen = None
    files: list[dict] = []
    for entry in entries:
        eid = entry.get("id")
        if eid is None:
            continue
        try:
            fs = jimaku_files(eid)  # all episodes (no episode filter)
        except Exception as e:
            log.warning("jimaku_files(%s) failed: %s", eid, e)
            continue
        if fs:
            chosen = entry
            files = fs
            break
    if not files:
        return {"anilist_id": anilist_id, "status": "no_files", "episodes": 0}

    # episode count for this title — used to normalize absolute (cross-season)
    # numbering back to season-relative and to drop out-of-range files (otherwise
    # a sequel's absolute-numbered subs inflate the episode list; see
    # match.normalize_episode_number).
    from ..match import service as match_service

    with connect() as cx:
        trow = cx.execute(
            "SELECT total_episodes FROM titles WHERE anilist_id=?", (anilist_id,)
        ).fetchone()
    total_eps = (trow["total_episodes"] if trow else None) or None

    # group subtitle files by SEASON-RELATIVE episode number (parsed + normalized)
    by_ep: dict[int, list] = {}
    skipped_out_of_range = 0
    for f in files:
        try:
            p = anitopy.parse(_file_name(f)) or {}
        except Exception:  # anitopy crashes on some odd filenames — skip cleanly
            p = {}
        epn = p.get("episode_number")
        if isinstance(epn, list):
            epn = epn[0] if epn else None
        rel = match_service.normalize_episode_number(anilist_id, epn, total_eps)
        if rel is None:
            if epn is not None:
                skipped_out_of_range += 1
            continue
        by_ep.setdefault(rel, []).append(f)
    if not by_ep:
        return {"anilist_id": anilist_id, "status": "no_episodic_files", "episodes": 0}
    if skipped_out_of_range:
        log.info(
            "title %s: skipped %d subtitle file(s) outside 1..%s (absolute/extra numbering)",
            anilist_id, skipped_out_of_range, total_eps,
        )

    # episodes already ingested -> skip (idempotent + saves downloads)
    with connect() as cx:
        have = {
            r["ep_number"]
            for r in cx.execute(
                "SELECT DISTINCT e.ep_number FROM episodes e "
                "JOIN subtitles s ON s.episode_id=e.id "
                "JOIN subtitle_lines sl ON sl.subtitle_id=s.id WHERE e.anilist_id=?",
                (anilist_id,),
            )
        }

    from ..catalog import service as catalog
    from ..jobs.service import enqueue

    done = 0
    for epn, ep_files in sorted(by_ep.items()):
        if epn in have:
            continue
        best = pick_best_file(ep_files)
        if not best:
            continue
        ep_id = catalog.upsert_episode(anilist_id, epn, title=f"Episode {epn}")
        dest = settings.library_dir / "_subs" / f"{anilist_id}_e{epn:02d}.ja.srt"
        try:
            download_subtitle(best, dest)
            ingest_subtitle(
                ep_id, dest, source="jimaku",
                jimaku_entry_id=chosen.get("id"),
                jimaku_file_id=best.get("id"),
                jimaku_filename=_file_name(best),
            )
            enqueue("comprehension", {"episode_id": ep_id})
            if settings.english_subs_enabled:
                enqueue("english_subtitle_fetch", {"episode_id": ep_id})
            done += 1
        except Exception as e:
            log.warning("ingest ep %s of title %s failed: %s", epn, anilist_id, e)

    if done:
        _record_event(
            "subtitle", f"Subtitles for {done} new episode(s)", "success",
            meta={"anilist_id": anilist_id, "episodes": done},
        )
    return {"anilist_id": anilist_id, "status": "ok", "episodes": done,
            "entry_id": chosen.get("id") if chosen else None}


def _job_title_subtitle_fetch(payload: dict) -> None:
    aid = payload.get("anilist_id")
    if aid is None:
        raise RuntimeError("title_subtitle_fetch: missing anilist_id")
    fetch_for_title(int(aid))


# --------------------------------------------------------------------------
# Job handler registration
# --------------------------------------------------------------------------


def _ep_label(episode_id: int) -> str:
    """Human label for an episode, e.g. 'Example Series - E01'. Best-effort."""
    try:
        ep = _episode_row(episode_id)
        if not ep:
            return f"episode {episode_id}"
        name = ep.get("romaji") or ep.get("english") or f"AniList {ep.get('anilist_id')}"
        en = ep.get("ep_number")
        return f"{name} - E{int(en):02d}" if en is not None else name
    except Exception:
        return f"episode {episode_id}"


def _record_event(category, title, kind="info", detail=None, meta=None) -> None:
    """Record a pipeline event; never raises (lazy import to avoid cycles)."""
    try:
        from ..events import service as events
        events.record(category, title, kind, detail=detail, meta=meta)
    except Exception as e:  # pragma: no cover
        log.debug("event record (%s) failed: %s", category, e)


# fetch_for_episode statuses that mean "a subtitle was ingested"
_SUB_OK_STATUSES = {"ok"}


def _job_subtitle_fetch(payload: dict) -> None:
    episode_id = payload.get("episode_id")
    if episode_id is None:
        raise RuntimeError("subtitle_fetch: missing episode_id")
    episode_id = int(episode_id)
    res = fetch_for_episode(episode_id)
    status = (res or {}).get("status")
    label = _ep_label(episode_id)

    if status in _SUB_OK_STATUSES:
        # subtitle ingested -> record + kick off comprehension on the new corpus
        _record_event(
            "subtitle", f"Subtitles matched for {label}", "success",
            detail=f"{res.get('lines')} lines"
            + (f", aligned via {res.get('align_tool')}" if res.get("aligned") else ""),
            meta={"episode_id": episode_id, "subtitle_id": res.get("subtitle_id")},
        )
        try:
            from ..jobs.service import enqueue
            enqueue("comprehension", {"episode_id": episode_id})
        except Exception as e:  # pragma: no cover
            log.warning("could not enqueue comprehension for ep %s: %s", episode_id, e)
    else:
        # nothing found this round; the late-sub sweep will retry later
        _record_event(
            "subtitle", f"No subtitle yet for {label}", "warning",
            detail=f"status={status}",
            meta={"episode_id": episode_id, "status": status},
        )

    # chain the English secondary-subtitle pass (idempotent). Runs after the JA
    # attempt so the translation-merge has Japanese lines to attach to; still runs
    # when JA wasn't found (an embedded English track is independent of JA).
    if settings.english_subs_enabled:
        try:
            from ..jobs.service import enqueue
            enqueue("english_subtitle_fetch", {"episode_id": episode_id})
        except Exception as e:  # pragma: no cover
            log.warning("could not enqueue english_subtitle_fetch for ep %s: %s", episode_id, e)


# --------------------------------------------------------------------------
# Late-sub retry sweep
# --------------------------------------------------------------------------


# how many episodes to re-enqueue per sweep, and how far to stagger them
_LATE_SWEEP_CAP = 20
_LATE_SWEEP_STAGGER_SECONDS = 15


def _sweep_batch(where_sql: str, cursor_key: str, cap: int) -> list[int]:
    """Rotating-cursor page over episodes matching `where_sql` (alias `e`).

    Ends sweep starvation: the old `ORDER BY e.id LIMIT cap` always re-picked the
    SAME lowest-id episodes, so if the first N permanently lacked subs, episodes
    past N were NEVER retried. Here each run advances a kv cursor past the ids it
    returns and wraps to the start when it reaches the tail, so every matching
    episode is eventually swept and permanently-subless ones are retried only once
    per full rotation instead of every hour.
    """
    from ..db import kv_get, kv_set
    try:
        after = int(kv_get(cursor_key, "0") or "0")
    except (TypeError, ValueError):
        after = 0
    with connect() as cx:
        rows = cx.execute(
            f"SELECT e.id AS id FROM episodes e WHERE {where_sql} AND e.id > ? "
            "ORDER BY e.id LIMIT ?",
            (after, cap),
        ).fetchall()
    ids = [r["id"] for r in rows]
    # wrap when we've reached the tail (fewer than a full page), else advance.
    kv_set(cursor_key, "0" if len(ids) < cap else str(ids[-1]))
    return ids


def late_sub_sweep(payload: Optional[dict] = None) -> dict:
    """Find episodes that have a video but no usable subtitle and re-enqueue a
    `subtitle_fetch` for each (capped, staggered, rotating cursor).

    "No usable subtitle" = the episode has a video_path AND either no rows in
    `subtitles`, or every subtitle for it has zero `subtitle_lines`.
    Records a summary event when at least one episode is retried.
    """
    payload = payload or {}
    cap = int(payload.get("cap", _LATE_SWEEP_CAP))

    episode_ids = _sweep_batch(
        "e.video_path IS NOT NULL AND e.video_path <> '' "
        "AND NOT EXISTS ("
        "  SELECT 1 FROM subtitles s "
        "  JOIN subtitle_lines sl ON sl.subtitle_id = s.id "
        "  WHERE s.episode_id = e.id"
        ")",
        "sweep_cursor:late_sub",
        cap,
    )
    if episode_ids:
        try:
            from ..jobs.service import enqueue
            for i, eid in enumerate(episode_ids):
                enqueue(
                    "subtitle_fetch", {"episode_id": eid},
                    delay_seconds=i * _LATE_SWEEP_STAGGER_SECONDS,
                )
        except Exception as e:  # pragma: no cover
            log.warning("late_sub_sweep: could not enqueue: %s", e)
        _record_event(
            "subtitle", f"Retrying subtitles for {len(episode_ids)} episode(s)", "info",
            meta={"episode_ids": episode_ids},
        )

    log.info("late_sub_sweep: retried %d episode(s)", len(episode_ids))
    return {"retried": len(episode_ids), "episode_ids": episode_ids}


def _job_late_sub_sweep(payload: dict) -> None:
    late_sub_sweep(payload or {})


# --------------------------------------------------------------------------
# Re-entrant alignment — re-evaluate whenever new data (video / English) arrives
# --------------------------------------------------------------------------


def _current_ja_sub(episode_id: int) -> Optional[dict]:
    """The episode's active JA study subtitle row (prefer aligned/newest)."""
    with connect() as cx:
        row = cx.execute(
            "SELECT id, path, source, jimaku_entry_id, jimaku_file_id, jimaku_filename, "
            "       aligned, align_tool, align_score, align_ref "
            "FROM subtitles WHERE episode_id=? AND COALESCE(lang,'ja')<>'en' "
            "AND path IS NOT NULL "
            "ORDER BY aligned DESC, version DESC, id DESC LIMIT 1",
            (episode_id,),
        ).fetchone()
    return dict(row) if row else None


def align_episode(episode_id: int, *, force: bool = False) -> dict:
    """(Re)align the episode's JA study sub against the best now-available
    reference and re-ingest if it improves. Re-entrant + idempotent: safe to call
    whenever new data arrives (video extracted, English fetched). No-op when there
    is nothing to align against yet (comprehension-only phase) or the current
    alignment is already good against the best available reference.

    This is the single alignment authority — `fetch_for_episode` does the initial
    multi-candidate selection; this keeps it correct as better references appear
    (e.g. an AnimeTosho English track arriving after the JA sub)."""
    ep = _episode_row(episode_id)
    if not ep:
        return {"episode_id": episode_id, "status": "no_episode"}
    ref = _reference_sub_for(ep)
    video = ep.get("video_path")
    # Re-alignment is only safe when driven by a trusted, video-timed reference:
    # audio VAD is too weak for anime to re-time a sub without risking a regression
    # (a good sub shoved out of sync). Without a reference we keep whatever the
    # initial fetch produced (alass→video) and wait for one to appear.
    if not ref:
        return {"episode_id": episode_id, "status": "no_trusted_reference"}

    row = _current_ja_sub(episode_id)
    if not row:
        return {"episode_id": episode_id, "status": "no_ja_sub"}
    sub_path = Path(row["path"])
    if not sub_path.exists():
        return {"episode_id": episode_id, "status": "missing_file", "path": str(sub_path)}

    if (not force and row.get("align_ref") == "en"
            and (row.get("align_score") or 0) >= _ALIGN_GOOD):
        return {"episode_id": episode_id, "status": "already_aligned",
                "score": row.get("align_score"), "ref": "en"}

    changed, tool, score, ref_kind = align(sub_path, video, reference_sub=ref)
    if score is None:
        return {"episode_id": episode_id, "status": "unscored"}

    # Re-ingest so the corpus (timings + text) matches the re-aligned file, and
    # persist the new confidence. Idempotent — updates the same row in place.
    ingest_subtitle(
        episode_id, sub_path, source=row["source"] or "jimaku",
        jimaku_entry_id=row["jimaku_entry_id"], jimaku_file_id=row["jimaku_file_id"],
        jimaku_filename=row["jimaku_filename"],
        aligned=1 if changed else (row["aligned"] or 0),
        align_tool=tool or row["align_tool"], align_score=score, align_ref=ref_kind,
    )

    # JA timings moved and re-ingest rebuilt the line rows (new ids, translation
    # column cleared) -> refresh comprehension now, and re-run the English pass so
    # its translations re-merge onto the new JA lines. Best-effort.
    try:
        learn_service.comprehension_aligned(episode_id)
    except Exception:
        pass
    if changed and settings.english_subs_enabled:
        try:
            from ..jobs.service import enqueue
            enqueue("english_subtitle_fetch", {"episode_id": episode_id})
        except Exception as e:
            log.info("align_episode: could not enqueue english refresh ep %s: %s", episode_id, e)

    # Surface genuinely-bad alignments instead of shipping them silently. align()
    # never regresses, so a low score means no method could sync this episode
    # (unalignable JA cut, or a reference/JA that don't correspond) — flag it for
    # attention rather than pretend success.
    low_conf = score < _ALIGN_LOW
    if low_conf:
        log.warning("align_episode: ep %s LOW confidence (score=%.2f ref=%s) — flagging",
                    episode_id, score, ref_kind)
        _record_event(
            "subtitle", f"Low-confidence alignment for {_ep_label(episode_id)}", "warning",
            detail=f"best score {score:.2f} (ref={ref_kind}) — may be mistimed",
            meta={"episode_id": episode_id, "align_score": score, "align_ref": ref_kind},
        )
    elif changed:
        _record_event(
            "subtitle", f"Re-aligned {_ep_label(episode_id)}", "success",
            detail=f"score {score:.2f} (ref={ref_kind}, via {tool})",
            meta={"episode_id": episode_id, "align_score": score, "align_ref": ref_kind},
        )
    return {"episode_id": episode_id, "status": "ok", "changed": bool(changed),
            "tool": tool, "score": score, "ref": ref_kind, "low_confidence": low_conf,
            "prev_score": row.get("align_score")}


def _job_subtitle_align(payload: dict) -> None:
    episode_id = payload.get("episode_id")
    if episode_id is None:
        raise RuntimeError("subtitle_align: missing episode_id")
    align_episode(int(episode_id), force=bool(payload.get("force")))


def subtitle_align_sweep(payload: Optional[dict] = None) -> dict:
    """Backfill: enqueue `subtitle_align` for episodes that could align better —
    have a JA sub + a video, and either no score yet or were only VAD-aligned
    while a trusted English sidecar now exists. Capped + staggered."""
    payload = payload or {}
    cap = int(payload.get("cap", _LATE_SWEEP_CAP))
    force = bool(payload.get("force"))
    with connect() as cx:
        rows = cx.execute(
            "SELECT DISTINCT e.id AS id FROM episodes e "
            "JOIN subtitles s ON s.episode_id=e.id AND COALESCE(s.lang,'ja')<>'en' "
            "WHERE e.video_path IS NOT NULL AND e.video_path <> '' "
            "AND (? OR s.align_score IS NULL OR s.align_ref='vad') "
            "ORDER BY e.id LIMIT ?",
            (1 if force else 0, cap),
        ).fetchall()
    episode_ids = [r["id"] for r in rows]
    if episode_ids:
        try:
            from ..jobs.service import enqueue
            for i, eid in enumerate(episode_ids):
                enqueue("subtitle_align", {"episode_id": eid, "force": force},
                        delay_seconds=i * _LATE_SWEEP_STAGGER_SECONDS)
        except Exception as e:  # pragma: no cover
            log.warning("subtitle_align_sweep: could not enqueue: %s", e)
    log.info("subtitle_align_sweep: queued %d episode(s)", len(episode_ids))
    return {"queued": len(episode_ids), "episode_ids": episode_ids}


def _job_subtitle_align_sweep(payload: dict) -> None:
    subtitle_align_sweep(payload or {})


def register_jobs() -> None:
    try:
        from ..jobs.service import register
        register("subtitle_fetch", _job_subtitle_fetch)
        register("late_sub_sweep", _job_late_sub_sweep)
        register("title_subtitle_fetch", _job_title_subtitle_fetch)
        register("subtitle_align", _job_subtitle_align)
        register("subtitle_align_sweep", _job_subtitle_align_sweep)
    except Exception as e:  # pragma: no cover
        log.warning("could not register subs handlers: %s", e)


# register at import time so the worker can dispatch 'subtitle_fetch'
register_jobs()

# import the English secondary-subtitle module last (it imports helpers from this
# module) so its 'english_subtitle_fetch' + 'english_sub_sweep' handlers register too.
from . import english  # noqa: E402,F401
