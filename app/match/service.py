"""Match service — resolve a release filename to an AniList title.

Pipeline: anitopy parse -> AniList GraphQL candidate search -> heuristic
ranking -> a confident best (or the manual-confirm queue). Also owns the
Fribb AniList<->MAL<->TVDB id map and the title->id cache (in `kv`).

Regression covered by the synthetic self-test: an exact-title TV candidate must
rank above a weaker-titled ONA special even when the special's episode count
coincides with the parsed episode number.
"""
from __future__ import annotations

import difflib
import json
import logging
import re
import time
from pathlib import Path
from typing import Optional

import anitopy
import httpx

from ..db import cursor, kv_get, kv_set
from ..models import MatchCandidate, MatchQueueItem, MatchResult

log = logging.getLogger("mimi_lab.match")

ANILIST_URL = "https://graphql.anilist.co"
# Page query so we get a ranked *set* of candidates (the demo used Media -> 1 hit).
ANILIST_QUERY = """
query ($s: String) {
  Page(perPage: 12) {
    media(search: $s, type: ANIME) {
      id
      idMal
      title { romaji english native }
      episodes
      format
      seasonYear
      coverImage { large }
      bannerImage
      description
    }
  }
}
""".strip()

IDMAP_URL = "https://raw.githubusercontent.com/Fribb/anime-lists/master/anime-list-full.json"
DATA_DIR = Path(__file__).resolve().parent.parent / "catalog" / "data"
IDMAP_PATH = DATA_DIR / "anime-list-full.json"

# in-process idmap indices: anilist_id -> {mal, tvdb}, and mal_id -> anilist_id
_IDMAP_BY_ANILIST: dict[int, dict] = {}
_IDMAP_MAL_TO_ANILIST: dict[int, int] = {}
_IDMAP_LOADED = False

# confidence: top score must clear this AND beat #2 by this margin
CONFIDENT_MIN_SCORE = 0.55
CONFIDENT_MARGIN = 0.12


# ---------------------------------------------------------------------------
# 1. parse
# ---------------------------------------------------------------------------
def parse_filename(name: str) -> dict:
    """anitopy parse, normalised to a small predictable dict.

    Keeps the full anitopy output too (callers may want video_term etc.) but
    guarantees the four fields we rank on are present (possibly None).
    """
    try:
        info = anitopy.parse(name) or {}
    except Exception:  # anitopy crashes on some odd filenames
        info = {}
    ep = info.get("episode_number")
    # anitopy returns a list when a file spans multiple eps; take the first.
    if isinstance(ep, list):
        ep = ep[0] if ep else None
    out = dict(info)
    out["anime_title"] = info.get("anime_title")
    out["episode_number"] = _to_int(ep)
    out["release_group"] = info.get("release_group")
    out["video_resolution"] = info.get("video_resolution")
    return out


# ---------------------------------------------------------------------------
# 2. AniList search
# ---------------------------------------------------------------------------
def anilist_search(title: str, *, max_retries: int = 4) -> list[dict]:
    """Search AniList for candidate Media. Returns flattened candidate dicts.

    Polite about 429: honours Retry-After, exponential backoff otherwise.
    Returns [] on a hard failure rather than raising (matching degrades to the
    queue instead of taking the scan down).
    """
    if not title or not title.strip():
        return []

    backoff = 1.0
    for attempt in range(max_retries):
        try:
            with httpx.Client(timeout=30) as c:
                r = c.post(
                    ANILIST_URL,
                    json={"query": ANILIST_QUERY, "variables": {"s": title}},
                    headers={"Accept": "application/json", "Content-Type": "application/json"},
                )
            if r.status_code == 429:
                wait = float(r.headers.get("Retry-After", backoff))
                log.warning("AniList 429 for %r; sleeping %.1fs", title, wait)
                time.sleep(wait)
                backoff = min(backoff * 2, 30)
                continue
            r.raise_for_status()
            media = (r.json().get("data") or {}).get("Page", {}).get("media") or []
            return [_flatten_media(m) for m in media]
        except httpx.HTTPStatusError as e:
            log.warning("AniList HTTP %s for %r", e.response.status_code, title)
            if attempt == max_retries - 1:
                return []
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)
        except Exception as e:  # network, json, etc.
            log.warning("AniList search error for %r: %s", title, e)
            if attempt == max_retries - 1:
                return []
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)
    return []


def _aired_episodes(m: dict) -> Optional[int]:
    """How many episodes of `m` have aired.

    While a show is RELEASING, AniList publishes `nextAiringEpisode.episode`
    (the number of the NEXT one), so aired == that - 1. Once it is FINISHED
    there is no next episode and the full count is the answer. Anything else
    (NOT_YET_RELEASED / unknown) has no meaningful answer -> None.
    """
    nxt = _to_int((m.get("nextAiringEpisode") or {}).get("episode"))
    if nxt is not None and nxt > 0:
        return nxt - 1
    if m.get("status") == "FINISHED":
        return _to_int(m.get("episodes"))
    return None


def _flatten_media(m: dict) -> dict:
    t = m.get("title") or {}
    return {
        "id": m.get("id"),
        "idMal": m.get("idMal"),
        "romaji": t.get("romaji"),
        "english": t.get("english"),
        "native": t.get("native"),
        "episodes": m.get("episodes"),
        "aired_episodes": _aired_episodes(m),
        "format": m.get("format"),
        "status": m.get("status"),
        "season": m.get("season"),
        "seasonYear": m.get("seasonYear"),
        "cover_url": (m.get("coverImage") or {}).get("large"),
        "banner_url": m.get("bannerImage"),
        "description": m.get("description"),
    }


# ---------------------------------------------------------------------------
# 3. ranking
# ---------------------------------------------------------------------------
_PUNCT_RE = re.compile(r"[^a-z0-9' ]+")
_WS_RE = re.compile(r"\s+")


def _norm(s: Optional[str]) -> str:
    if not s:
        return ""
    s = s.lower().replace("’", "'").replace("`", "'").replace("’", "'")
    s = _PUNCT_RE.sub(" ", s)
    return _WS_RE.sub(" ", s).strip()


def _sim(a: str, b: str) -> float:
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def _to_int(v) -> Optional[int]:
    if v is None:
        return None
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def rank_candidates(parsed: dict, candidates: list[dict]) -> list[MatchCandidate]:
    """Score candidates; return MatchCandidate[] highest-first.

    Score = base title similarity (max of romaji/english) with exact/startswith
    boosts, + a format prior (TV >> ONA/OVA/special), + an episode-count sanity
    nudge. The title signal dominates so a strong main-series title match
    outranks a weaker-titled special whose episode count merely coincides with
    the parsed episode number.
    """
    parsed_title = parsed.get("anime_title") or ""
    pt = _norm(parsed_title)
    parsed_ep = _to_int(parsed.get("episode_number"))

    scored: list[MatchCandidate] = []
    for m in candidates:
        rom = m.get("romaji") or ""
        eng = m.get("english") or ""
        nat = m.get("native") or ""
        fmt = (m.get("format") or "").upper()
        eps = _to_int(m.get("episodes"))

        sim_rom = _sim(parsed_title, rom)
        sim_eng = _sim(parsed_title, eng)
        sim_nat = _sim(parsed_title, nat)
        base = max(sim_rom, sim_eng, sim_nat)
        best_field = "english" if sim_eng >= sim_rom else "romaji"

        reasons: list[str] = []
        score = base
        reasons.append(f"title~{base:.2f}({best_field})")

        # exact / prefix boosts against either romaji or english
        n_rom, n_eng = _norm(rom), _norm(eng)
        if pt and (pt == n_rom or pt == n_eng):
            score += 0.35
            reasons.append("exact-title")
        elif pt and (n_rom.startswith(pt) or n_eng.startswith(pt) or
                     (pt.startswith(n_rom) and n_rom) or (pt.startswith(n_eng) and n_eng)):
            score += 0.12
            reasons.append("prefix")

        # format prior: strongly prefer the main TV broadcast
        if fmt == "TV":
            score += 0.30
            reasons.append("TV")
        elif fmt in ("TV_SHORT", "MOVIE"):
            score += 0.05
        elif fmt in ("ONA", "OVA", "SPECIAL", "MUSIC"):
            score -= 0.18
            reasons.append(f"{fmt.lower()}-penalty")

        # episode-count sanity: the parsed ep should fit within the title.
        # A small nudge only — never enough to override the title/format signal.
        if parsed_ep is not None and eps:
            if parsed_ep <= eps:
                score += 0.08
                reasons.append(f"ep{parsed_ep}<={eps}")
            else:
                score -= 0.10
                reasons.append(f"ep{parsed_ep}>{eps}!")

        cand = MatchCandidate(
            anilist_id=m.get("id"),
            romaji=rom or None,
            english=eng or None,
            format=m.get("format"),
            episodes=eps,
            year=_to_int(m.get("seasonYear")),
            cover_url=m.get("cover_url"),
            score=round(score, 4),
            reason=", ".join(reasons),
        )
        scored.append(cand)

    scored.sort(key=lambda c: c.score, reverse=True)
    return scored


# ---------------------------------------------------------------------------
# 4. match_file (+ title->id cache)
# ---------------------------------------------------------------------------
def _cache_key(title: str) -> str:
    return f"match:title:{_norm(title)}"


def match_file(filename: str) -> MatchResult:
    """Parse + search + rank a filename into a MatchResult.

    Caches the resolved title->anilist_id in `kv` so repeat scans of the same
    show don't re-hit AniList. Cache hit still re-fetches full candidates only
    if needed; for the common confident path we short-circuit from cache.
    """
    parsed = parse_filename(filename)
    title = parsed.get("anime_title") or ""
    parsed_ep = parsed.get("episode_number")

    candidates = anilist_search(title) if title else []
    ranked = rank_candidates(parsed, candidates)

    best = ranked[0] if ranked else None
    confident = False
    if best:
        second = ranked[1].score if len(ranked) > 1 else 0.0
        confident = best.score >= CONFIDENT_MIN_SCORE and (best.score - second) >= CONFIDENT_MARGIN
        if confident:
            kv_set(_cache_key(title), str(best.anilist_id))

    return MatchResult(
        filename=filename,
        parsed=parsed,
        ep_number=parsed_ep,
        best=best,
        candidates=ranked,
        confident=confident,
    )


def upsert_title_from_candidate(cand: MatchCandidate) -> int:
    """Fetch full Media for a chosen candidate and upsert via catalog.

    Used by scan + confirm. We re-fetch by id to get description/banner/etc.
    that the ranked candidate dict doesn't carry; falls back to the slim
    candidate fields if the lookup fails.
    """
    from ..catalog import service as catalog_service

    media = anilist_media_by_id(cand.anilist_id)
    if media:
        return catalog_service.upsert_title(media)
    # fallback: build a minimal dict from the candidate
    return catalog_service.upsert_title(
        {
            "id": cand.anilist_id,
            "romaji": cand.romaji,
            "english": cand.english,
            "format": cand.format,
            "episodes": cand.episodes,
            "seasonYear": cand.year,
            "cover_url": cand.cover_url,
        }
    )


def anilist_media_by_id(anilist_id: int) -> Optional[dict]:
    """Fetch a single Media by AniList id (flattened)."""
    q = """
    query ($id: Int) {
      Media(id: $id, type: ANIME) {
        id idMal title { romaji english native }
        episodes format season seasonYear status nextAiringEpisode { episode }
        coverImage { large } bannerImage description
      }
    }
    """.strip()
    backoff = 1.0
    for attempt in range(4):
        try:
            with httpx.Client(timeout=30) as c:
                r = c.post(ANILIST_URL, json={"query": q, "variables": {"id": anilist_id}})
            if r.status_code == 429:
                time.sleep(float(r.headers.get("Retry-After", backoff)))
                backoff = min(backoff * 2, 30)
                continue
            r.raise_for_status()
            media = (r.json().get("data") or {}).get("Media")
            return _flatten_media(media) if media else None
        except Exception as e:
            log.warning("AniList media-by-id %s error: %s", anilist_id, e)
            if attempt == 3:
                return None
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)
    return None


def anilist_media_by_ids(ids: list[int], *, max_retries: int = 4) -> list[dict]:
    """Fetch many Media by AniList id (flattened), batched 50 per request (the
    AniList page cap) — one query enriches a whole MAL pull instead of one
    request per title. Best-effort: a chunk that keeps failing is skipped;
    polite about 429 like the other AniList calls."""
    q = """
    query ($ids: [Int]) {
      Page(page: 1, perPage: 50) {
        media(id_in: $ids, type: ANIME) {
          id idMal title { romaji english native }
          episodes format season seasonYear status nextAiringEpisode { episode }
          coverImage { large } bannerImage description
        }
      }
    }
    """.strip()
    uniq = sorted({int(i) for i in ids if i is not None})
    out: list[dict] = []
    for i in range(0, len(uniq), 50):
        chunk = uniq[i:i + 50]
        backoff = 1.0
        for attempt in range(max_retries):
            try:
                with httpx.Client(timeout=30) as c:
                    r = c.post(ANILIST_URL, json={"query": q, "variables": {"ids": chunk}})
                if r.status_code == 429:
                    time.sleep(float(r.headers.get("Retry-After", backoff)))
                    backoff = min(backoff * 2, 30)
                    continue
                r.raise_for_status()
                media = (r.json().get("data") or {}).get("Page", {}).get("media") or []
                out.extend(_flatten_media(m) for m in media)
                break
            except Exception as e:
                log.warning("AniList media-by-ids error (%d ids): %s", len(chunk), e)
                if attempt == max_retries - 1:
                    break
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)
    return out


# ---------------------------------------------------------------------------
# 5. Fribb ID map
# ---------------------------------------------------------------------------
def refresh_idmap() -> dict:
    """Download the Fribb anime-list-full.json into catalog/data and index it.

    Returns a small summary {entries, with_mal, with_tvdb, path}.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with httpx.Client(timeout=120, follow_redirects=True) as c:
        r = c.get(IDMAP_URL)
        r.raise_for_status()
        raw = r.content
    IDMAP_PATH.write_bytes(raw)
    summary = _load_idmap(force=True)
    log.info("idmap refreshed: %s entries", summary.get("entries"))
    return summary


def _load_idmap(force: bool = False) -> dict:
    """Load + index the on-disk idmap into the in-process dicts."""
    global _IDMAP_LOADED
    if _IDMAP_LOADED and not force:
        return {
            "entries": len(_IDMAP_BY_ANILIST),
            "with_mal": len(_IDMAP_MAL_TO_ANILIST),
            "path": str(IDMAP_PATH),
            "loaded": True,
        }
    if not IDMAP_PATH.exists():
        raise FileNotFoundError(
            f"idmap not downloaded yet ({IDMAP_PATH}); call refresh_idmap() first"
        )

    data = json.loads(IDMAP_PATH.read_text(encoding="utf-8"))
    _IDMAP_BY_ANILIST.clear()
    _IDMAP_MAL_TO_ANILIST.clear()

    with_mal = with_tvdb = 0
    for entry in data:
        al = _to_int(entry.get("anilist_id"))
        if al is None:
            continue
        mal = _to_int(entry.get("mal_id"))
        # tvdb can be int or list in Fribb data; take first int.
        tvdb_raw = entry.get("thetvdb_id", entry.get("tvdb_id"))
        if isinstance(tvdb_raw, list):
            tvdb_raw = tvdb_raw[0] if tvdb_raw else None
        tvdb = _to_int(tvdb_raw)

        # episode_offset maps a season/cour's ABSOLUTE episode numbers back to
        # season-relative ones (absolute_ep - offset = relative_ep). Fribb keys it
        # per provider ({"tvdb": N, "tmdb": M}); providers usually agree. We keep
        # all distinct positive candidates and let the caller validate against the
        # title's episode count. Used to stop absolute-numbered subtitle/release
        # files from inflating a season's episode list (see normalize_episode_number).
        offsets: list[int] = []
        off_raw = entry.get("episode_offset")
        if isinstance(off_raw, dict):
            for v in off_raw.values():
                iv = _to_int(v)
                if iv and iv > 0:
                    offsets.append(iv)
        elif off_raw is not None:
            iv = _to_int(off_raw)
            if iv and iv > 0:
                offsets.append(iv)

        # IMDB: Fribb gives e.g. ["tt5607616"] — keep the first "tt…" string.
        imdb_raw = entry.get("imdb_id")
        if isinstance(imdb_raw, list):
            imdb_raw = imdb_raw[0] if imdb_raw else None
        imdb = imdb_raw.strip() if isinstance(imdb_raw, str) and imdb_raw.strip() else None
        # TMDB: {"tv": N} | {"movie": N} | N — prefer the TV id (anime episodes).
        tmdb_raw = entry.get("themoviedb_id")
        if isinstance(tmdb_raw, dict):
            tmdb = _to_int(tmdb_raw.get("tv") or tmdb_raw.get("movie"))
        else:
            tmdb = _to_int(tmdb_raw)

        anidb = _to_int(entry.get("anidb_id"))

        _IDMAP_BY_ANILIST[al] = {
            "mal": mal,
            "tvdb": tvdb,
            "imdb": imdb,
            "tmdb": tmdb,
            "anidb": anidb,
            "offsets": sorted(set(offsets)),
        }
        if mal is not None:
            with_mal += 1
            _IDMAP_MAL_TO_ANILIST.setdefault(mal, al)
        if tvdb is not None:
            with_tvdb += 1

    _IDMAP_LOADED = True
    return {
        "entries": len(_IDMAP_BY_ANILIST),
        "with_mal": with_mal,
        "with_tvdb": with_tvdb,
        "path": str(IDMAP_PATH),
        "loaded": True,
    }


def _ensure_idmap() -> None:
    if not _IDMAP_LOADED:
        _load_idmap()


def mal_id_for(anilist_id: int) -> Optional[int]:
    try:
        _ensure_idmap()
    except FileNotFoundError:
        return None
    e = _IDMAP_BY_ANILIST.get(_to_int(anilist_id))
    return e.get("mal") if e else None


def anilist_id_for(mal_id: int) -> Optional[int]:
    try:
        _ensure_idmap()
    except FileNotFoundError:
        return None
    return _IDMAP_MAL_TO_ANILIST.get(_to_int(mal_id))


def tvdb_id_for(anilist_id: int) -> Optional[int]:
    try:
        _ensure_idmap()
    except FileNotFoundError:
        return None
    e = _IDMAP_BY_ANILIST.get(_to_int(anilist_id))
    return e.get("tvdb") if e else None


def external_ids_for(anilist_id: int) -> dict:
    """External provider IDs for a title from the Fribb idmap:
    {"imdb": "tt…"|None, "tmdb": int|None, "tvdb": int|None}. Empty when unknown.
    Used for ID-keyed external lookups (subtitle/metadata providers)."""
    try:
        _ensure_idmap()
    except FileNotFoundError:
        return {}
    e = _IDMAP_BY_ANILIST.get(_to_int(anilist_id))
    if not e:
        return {}
    return {"imdb": e.get("imdb"), "tmdb": e.get("tmdb"),
            "tvdb": e.get("tvdb"), "anidb": e.get("anidb")}


def anidb_id_for(anilist_id: int) -> Optional[int]:
    """AniDB anime id (per-cour) for a title from the Fribb idmap, or None.
    AnimeTosho is AniDB-keyed, so this is the join key for English subs there."""
    try:
        _ensure_idmap()
    except FileNotFoundError:
        return None
    e = _IDMAP_BY_ANILIST.get(_to_int(anilist_id))
    return e.get("anidb") if e else None


def episode_offsets_for(anilist_id: int) -> list[int]:
    """Candidate absolute→relative episode offsets for a title (from the Fribb
    idmap `episode_offset`). Empty when the title has no offset (most do not)."""
    try:
        _ensure_idmap()
    except FileNotFoundError:
        return []
    e = _IDMAP_BY_ANILIST.get(_to_int(anilist_id))
    return list(e.get("offsets") or []) if e else []


def infer_episode_offset(numbers, ceiling: Optional[int]) -> Optional[int]:
    """Infer a season's absolute→relative offset from a SET of parsed episode
    numbers (one release group's / one jimaku entry's files).

    Release groups and subtitlers disagree about sequel numbering, and often
    within a single batch: `S02E01..E03` (relative) can sit right next to
    `S02E13..E19` (absolute) for the very same episodes. Per-number logic cannot
    tell those apart — but the *set* can, once you know how many episodes have
    actually aired (`ceiling`):

      • numbers within 1..ceiling are relative;
      • numbers above `ceiling` cannot be relative, so they are absolute, and
        the lowest of them is that season's episode 1 → offset = min(high) - 1.

    Two guards keep a wrong guess from corrupting the numbering — both decline
    rather than fold, because a declined fold leaves a VISIBLE extra episode row
    (prunable, obvious in the UI) while a wrong fold silently drops a real
    episode onto one that is already ingested:

      • every high number must fold back into 1..ceiling, so a stray special or
        a mis-parsed number rejects the whole inference;
      • the high run must be separated from the ceiling by a gap
        (min(high) >= ceiling + 2). A number landing exactly one past the
        ceiling is far more likely to be an episode that has just aired — subs
        show up within minutes of broadcast, while AniList's airing data lags —
        than the start of an absolute run. Cost: a sequel whose previous
        season(s) are exactly as long as the part aired so far is not folded
        until the offset is learned some other way.

    Returns the offset (> 0), or None when there is nothing to fold / no
    consistent offset. Deliberately NOT derived from AniList's PREQUEL chain:
    that chain breaks on split cours (a season whose own prequel edge skips a
    part) and then silently yields a wrong offset — measured 48 where the true
    value was 72 — which is worse than not folding at all.
    """
    ceiling = _to_int(ceiling) or 0
    if ceiling <= 0:
        return None
    nums = sorted({n for n in (_to_int(x) for x in numbers) if n is not None and n >= 1})
    high = [n for n in nums if n > ceiling]
    if not high:
        return None
    if min(high) < ceiling + 2:  # contiguous with the aired range → just aired
        return None
    off = min(high) - 1
    if off <= 0:
        return None
    if not all(1 <= n - off <= ceiling for n in high):
        return None
    return off


# AniList's airing data lags a broadcast by minutes to hours, while subtitles
# for a fresh episode show up almost immediately. When a season's full length is
# unknown we allow a folded number to land this far past the aired count rather
# than discarding a just-aired episode.
_AIRING_LAG_SLACK = 2


def _season_bounds(anilist_id: int, total_episodes: Optional[int] = None
                   ) -> tuple[int, int]:
    """(ceiling, limit) for a season's episode numbering.

    `ceiling` — the highest number that can legitimately be SEASON-RELATIVE
    right now: the aired count when known (available even while a show is
    ongoing and AniList has published no final length), else the total.

    `limit` — the highest number a FOLDED result may take: the season's full
    length when known, else the ceiling plus a little slack for airing lag.
    A folded number may legitimately exceed the aired count when AniList's
    airing data is behind the broadcast.
    """
    aired = None
    total = _to_int(total_episodes)
    try:
        with cursor() as cx:
            row = cx.execute(
                "SELECT aired_episodes, total_episodes FROM titles WHERE anilist_id=?",
                (anilist_id,),
            ).fetchone()
        if row is not None:
            aired = _to_int(row["aired_episodes"])
            total = total or _to_int(row["total_episodes"])
    except Exception as e:  # column missing on an un-migrated DB, no DB in tests…
        log.debug("_season_bounds(%s) lookup skipped: %s", anilist_id, e)
    ceiling = aired or total or 0
    limit = total or (ceiling + _AIRING_LAG_SLACK if ceiling else 0)
    return ceiling, limit


def episode_ceiling(anilist_id: int, total_episodes: Optional[int] = None) -> int:
    """Highest episode number that can legitimately be SEASON-RELATIVE. 0 = unknown."""
    return _season_bounds(anilist_id, total_episodes)[0]


def stored_episode_offset(anilist_id: int) -> Optional[int]:
    """The learned absolute→relative offset persisted on the title, if any."""
    try:
        with cursor() as cx:
            row = cx.execute(
                "SELECT episode_offset FROM titles WHERE anilist_id=?", (anilist_id,)
            ).fetchone()
        return _to_int(row["episode_offset"]) if row is not None else None
    except Exception as e:
        log.debug("stored_episode_offset(%s) lookup skipped: %s", anilist_id, e)
        return None


def set_episode_offset(anilist_id: int, offset: int) -> None:
    """Persist a learned offset so single-file paths (a downloaded release, a
    late subtitle sweep) fold the same way the batch pass did."""
    off = _to_int(offset)
    if not off or off <= 0:
        return
    with cursor() as cx:
        cx.execute(
            "UPDATE titles SET episode_offset=?, updated_at=datetime('now') "
            "WHERE anilist_id=? AND COALESCE(episode_offset,0) <> ?",
            (off, anilist_id, off),
        )


def normalize_episode_number(
    anilist_id: int,
    raw_ep,
    total_episodes: Optional[int],
    *,
    offset_hint: Optional[int] = None,
) -> Optional[int]:
    """Map a release/subtitle file's parsed episode number to a SEASON-RELATIVE
    number for `anilist_id`.

    Sequels are frequently numbered with ABSOLUTE (cross-season) episode numbers
    in release filenames. Left alone, each distinct number becomes its own
    episode row, inflating the season and duplicating episodes that were also
    ingested under relative numbering. This folds absolute numbers back using,
    in order of preference: an explicit `offset_hint` (inferred from the batch
    being imported), the offset learned for this title, then the Fribb
    `episode_offset`s — validated against the season's episode ceiling.

    Returns a relative episode number, or None if it can't be confidently placed
    within the season (caller should drop those files).

    Behaviour:
      • a number within 1..ceiling is kept as-is (covers the common
        relative-numbered and SxxEyy files; the rare absolute-vs-relative overlap
        on the first eps of a longer sequel defaults to relative);
      • a number above the ceiling is folded by the offset that lands it in
        1..ceiling;
      • when the ceiling is unknown, an offset is applied only if it keeps the
        number positive (best effort), else the number is kept.

    The ceiling is the AIRED episode count when known, else `total_episodes`.
    Using the aired count matters for an ongoing sequel: AniList often publishes
    no final length for one, and a 0 ceiling used to disable this whole function
    (every parsed number passed straight through, which is exactly how a season
    ends up holding both `1,2,3` and `13,14,15` for the same three episodes).
    """
    raw = _to_int(raw_ep)
    if raw is None or raw < 0:
        return None

    ceiling, limit = _season_bounds(anilist_id, total_episodes)
    if 1 <= raw <= ceiling:
        return raw

    offsets = [o for o in (offset_hint, stored_episode_offset(anilist_id)) if o]
    offsets += [o for o in episode_offsets_for(anilist_id) if o not in offsets]

    if ceiling > 0:
        # raw is outside 1..ceiling → try to fold an absolute number into range.
        # Prefer the largest offset that fits (later cours have larger offsets).
        for off in sorted(offsets, reverse=True):
            rel = raw - off
            if 1 <= rel <= limit:
                return rel
        # No offset places it. It may still be an episode that has just aired,
        # ahead of AniList's airing data — keep it if it fits the season at all.
        if 1 <= raw <= limit:
            return raw
        return None  # can't place it in this season → drop (don't inflate)

    # ceiling unknown (unmapped/not yet aired): best-effort offset, else keep raw.
    for off in sorted(offsets, reverse=True):
        if raw - off >= 1:
            return raw - off
    return raw if raw >= 1 else None


# ---------------------------------------------------------------------------
# 6. match_queue
# ---------------------------------------------------------------------------
def enqueue_unmatched(
    filename: str,
    parsed: dict,
    candidates: list,
    download_id: Optional[int] = None,
) -> int:
    """Add an unmatched file to the manual-confirm queue. Returns queue id.

    `candidates` may be MatchCandidate objects or plain dicts.
    """
    ep_number = _to_int(parsed.get("episode_number"))
    cand_payload = [
        c.model_dump() if isinstance(c, MatchCandidate) else c for c in candidates
    ]
    with cursor() as cx:
        cur = cx.execute(
            """
            INSERT INTO match_queue
              (filename, parsed_json, candidates_json, ep_number, state, download_id)
            VALUES (?,?,?,?, 'pending', ?)
            """,
            (
                filename,
                json.dumps(parsed, ensure_ascii=False),
                json.dumps(cand_payload, ensure_ascii=False),
                ep_number,
                download_id,
            ),
        )
        return cur.lastrowid


def list_queue(state: str = "pending") -> list[MatchQueueItem]:
    """List queued items. state='all' returns every state."""
    with cursor() as cx:
        if state == "all":
            rows = cx.execute(
                "SELECT * FROM match_queue ORDER BY id DESC"
            ).fetchall()
        else:
            rows = cx.execute(
                "SELECT * FROM match_queue WHERE state=? ORDER BY id DESC", (state,)
            ).fetchall()

    items: list[MatchQueueItem] = []
    for r in rows:
        try:
            cands_raw = json.loads(r["candidates_json"] or "[]")
        except (json.JSONDecodeError, TypeError):
            cands_raw = []
        cands = [MatchCandidate(**c) for c in cands_raw if isinstance(c, dict)]
        try:
            parsed = json.loads(r["parsed_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            parsed = {}
        items.append(
            MatchQueueItem(
                id=r["id"],
                filename=r["filename"],
                ep_number=r["ep_number"],
                title_guess=(parsed.get("anime_title") if isinstance(parsed, dict) else None),
                candidates=cands,
                chosen_anilist_id=r["chosen_anilist_id"],
                state=r["state"],
            )
        )
    return items


def delete_queue_item(queue_id: int) -> bool:
    """Remove a queue item (e.g. an unresolvable / unwanted file). Returns True
    if a row was deleted."""
    with cursor() as cx:
        cur = cx.execute("DELETE FROM match_queue WHERE id=?", (queue_id,))
        return cur.rowcount > 0


def search_candidates(query: str, ep_number: Optional[int] = None) -> list[MatchCandidate]:
    """Manual AniList search for the match queue: search + rank by the query as
    the title. Lets the user resolve an item the auto-matcher couldn't."""
    if not query or not query.strip():
        return []
    media = anilist_search(query)
    parsed = {"anime_title": query, "episode_number": ep_number}
    return rank_candidates(parsed, media)


def suggest_match(queue_id: int) -> dict:
    """Use Claude Haiku to turn a messy filename into the official anime title,
    then AniList-search + rank it. Falls back to the parsed title when Haiku is
    unavailable. Returns {suggested_title, candidates, matched_by}."""
    from ..llm import haiku_json

    with cursor() as cx:
        row = cx.execute(
            "SELECT filename, parsed_json, ep_number FROM match_queue WHERE id=?",
            (queue_id,),
        ).fetchone()
    if not row:
        raise ValueError(f"queue item {queue_id} not found")

    try:
        parsed = json.loads(row["parsed_json"] or "{}")
    except (json.JSONDecodeError, TypeError):
        parsed = {}
    guess = (parsed.get("anime_title") if isinstance(parsed, dict) else None) or row["filename"]

    suggested = guess
    matched_by = "fallback"
    res = haiku_json(
        "You identify which anime a release file belongs to. Reply only with the "
        "requested JSON.",
        f"Release filename: {row['filename']!r}\nParser's title guess: {guess!r}\n\n"
        "Return the official anime title (romaji or English) this file belongs to, "
        "as it would appear on AniList — the specific season/cour if the filename "
        "indicates one (e.g. '2nd Season', 'Part 2').",
        schema={
            "type": "object",
            "properties": {"title": {"type": "string"}},
            "required": ["title"],
            "additionalProperties": False,
        },
        max_tokens=120,
    )
    if isinstance(res, dict) and isinstance(res.get("title"), str) and res["title"].strip():
        suggested = res["title"].strip()
        matched_by = "haiku"

    cands = search_candidates(suggested, ep_number=row["ep_number"])
    return {
        "suggested_title": suggested,
        "candidates": [c.model_dump() for c in cands],
        "matched_by": matched_by,
    }


def confirm(queue_id: int, anilist_id: int) -> dict:
    """Confirm a queue item: upsert the chosen title, move the staged file into
    the Library, and kick off the subtitle pipeline.

    The download was already downloaded + transcoded to a `.mp4` staged in
    `inbox/processed/` (its path recorded on the queue row as `processed_path`).
    Confirming here calls `media.finalize_import`, which organizes the file into
    the Library, upserts the episode WITH a real `video_path`, and enqueues
    `subtitle_fetch`. Previously this searched `library_dir` for the *original*
    filename+extension — which never matched the staged `.mp4` — so the episode
    got no video and the file was orphaned forever.
    """
    from ..catalog import service as catalog_service

    with cursor() as cx:
        row = cx.execute(
            "SELECT * FROM match_queue WHERE id=?", (queue_id,)
        ).fetchone()
        if not row:
            raise ValueError(f"queue item {queue_id} not found")
        row = dict(row)

    # upsert the title (full fetch by id -> catalog)
    media = anilist_media_by_id(anilist_id)
    if media:
        catalog_service.upsert_title(media)
        title_romaji = (media.get("title") or {}).get("romaji") or media.get("romaji")
    else:
        catalog_service.upsert_title({"id": anilist_id})
        title_romaji = None

    ep_number = row.get("ep_number")
    episode_id = None
    final_path = None

    # locate the processed file: prefer the recorded staged path, else search.
    src = row.get("processed_path")
    if not src or not Path(src).exists():
        src = _find_processed_path(row["filename"])

    if src and Path(src).exists() and ep_number is not None:
        # move it into the Library + upsert episode WITH video + start subtitles.
        from ..media import service as media_service
        try:
            res = media_service.finalize_import(
                src, anilist_id, ep_number, title_romaji,
                download_id=row.get("download_id"),
            )
            episode_id = res.get("episode_id")
            final_path = res.get("final_path")
        except Exception as e:
            log.warning("confirm: finalize_import failed for queue %s: %s", queue_id, e)

    if episode_id is None and ep_number is not None:
        # fall back to a bare episode row (no playable video located).
        fields = {}
        if final_path:
            fields["video_path"] = final_path
            fields["container"] = Path(final_path).suffix.lstrip(".")
        episode_id = catalog_service.upsert_episode(anilist_id, ep_number, **fields)

    with cursor() as cx:
        cx.execute(
            "UPDATE match_queue SET chosen_anilist_id=?, state='confirmed' WHERE id=?",
            (anilist_id, queue_id),
        )

    return {
        "queue_id": queue_id,
        "anilist_id": anilist_id,
        "ep_number": ep_number,
        "episode_id": episode_id,
        "video_path": final_path,
        "state": "confirmed",
    }


def _find_processed_path(filename: str) -> Optional[str]:
    """Best-effort: locate the processed/original file for `filename`.

    Checks (a) the staged `inbox/processed/<stem>.mp4`, (b) the exact filename
    under the Library, and (c) any `<stem>.*` video under the Library. Covers the
    .mkv→.mp4 rename the transcode step performs.
    """
    from ..config import settings

    stem = Path(filename).stem
    # (a) staged processed mp4 (the common case for an unmatched download)
    staged = Path(settings.inbox_dir) / "processed" / f"{stem}.mp4"
    if staged.is_file():
        return str(staged)

    root = Path(settings.library_dir)
    if root.exists():
        # (b) exact filename
        for p in root.rglob(filename):
            if p.is_file():
                return str(p)
        # (c) same stem, any video extension
        for p in root.rglob(f"{stem}.*"):
            if p.is_file() and p.suffix.lower() in _VIDEO_SUFFIXES:
                return str(p)
    return None


_VIDEO_SUFFIXES = {".mp4", ".mkv", ".webm", ".m4v", ".avi", ".mov", ".ts"}
