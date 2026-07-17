"""Pydantic API models — the response/request contracts shared across modules."""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


class Title(BaseModel):
    anilist_id: int
    mal_id: Optional[int] = None
    romaji: Optional[str] = None
    english: Optional[str] = None
    native: Optional[str] = None
    format: Optional[str] = None
    total_episodes: Optional[int] = None
    season: Optional[str] = None
    year: Optional[int] = None
    cover_url: Optional[str] = None
    banner_url: Optional[str] = None
    description: Optional[str] = None
    status: Optional[str] = None
    mal_status: Optional[str] = None
    mal_score: Optional[int] = None
    mal_progress: Optional[int] = None
    # derived for the grid
    episode_count_local: int = 0
    avg_comprehension: Optional[float] = None


class Episode(BaseModel):
    id: int
    anilist_id: int
    ep_number: int
    title: Optional[str] = None
    video_path: Optional[str] = None
    codec: Optional[str] = None
    container: Optional[str] = None
    duration_ms: Optional[int] = None
    watched: bool = False
    has_subtitle: bool = False           # has a Japanese (study) subtitle
    has_english: bool = False            # has an English secondary/reference track
    download_state: Optional[str] = None
    download_id: Optional[int] = None
    comprehension_pct: Optional[float] = None
    comprehension_rating: Optional[str] = None
    comprehension_source: Optional[str] = None
    new_word_count: Optional[int] = None


class MatchCandidate(BaseModel):
    anilist_id: int
    romaji: Optional[str] = None
    english: Optional[str] = None
    format: Optional[str] = None
    episodes: Optional[int] = None
    year: Optional[int] = None
    cover_url: Optional[str] = None
    score: float = 0.0           # ranking score, higher = better
    reason: Optional[str] = None


class MatchResult(BaseModel):
    filename: str
    parsed: dict
    ep_number: Optional[int] = None
    best: Optional[MatchCandidate] = None
    candidates: list[MatchCandidate] = []
    confident: bool = False


class MatchQueueItem(BaseModel):
    id: int
    filename: str
    ep_number: Optional[int] = None
    title_guess: Optional[str] = None     # parsed anime_title, for the manual search box
    candidates: list[MatchCandidate] = []
    chosen_anilist_id: Optional[int] = None
    state: str = "pending"


class SubtitleLine(BaseModel):
    id: int
    episode_id: int
    idx: int
    start_ms: int
    end_ms: int
    text: str
    text_furigana: Optional[str] = None
    translation: Optional[str] = None


class NewWord(BaseModel):
    lemma: str
    reading: Optional[str] = None
    freq_rank: Optional[int] = None
    gloss: Optional[str] = None
    count: int = 1               # occurrences in the episode


class ComprehensionResult(BaseModel):
    episode_id: int
    comprehension_pct: float
    rating: Optional[str] = None
    source: str = "aligned"       # aligned | exact
    total_tokens: int = 0
    known_tokens: int = 0
    unknown_unique: int = 0
    new_words: list[NewWord] = []


class Moment(BaseModel):
    """A subtitle line where a searched word appears, with media anchors.

    `title` is the SHOW name (romaji/english) — it used to carry the episode
    title ("Episode 1"), which made results anonymous. `episode_title` keeps
    the per-episode label. image/audio URLs are None when the episode has no
    video (a clip can never be made — the UI used to render blank boxes).
    `unknown_count` = tokens in this line not yet KNOWN (drives i+1 sorting).
    """
    line_id: int
    anilist_id: int
    episode_id: int
    title: Optional[str] = None
    episode_title: Optional[str] = None
    ep_number: Optional[int] = None
    text: str
    text_furigana: Optional[str] = None
    translation: Optional[str] = None
    start_ms: int
    end_ms: int
    video_path: Optional[str] = None
    image_url: Optional[str] = None
    audio_url: Optional[str] = None
    unknown_count: Optional[int] = None


class KnownWordsSummary(BaseModel):
    total: int = 0
    known: int = 0
    learning: int = 0
    unknown: int = 0
    ignored: int = 0
    updated_at: Optional[str] = None


class Download(BaseModel):
    id: int
    title_guess: Optional[str] = None
    state: str
    progress: float = 0.0
    anilist_id: Optional[int] = None
    ep_number: Optional[int] = None
    release_group: Optional[str] = None
    resolution: Optional[str] = None
    size_bytes: Optional[int] = None
    kind: str = "single"               # 'single' | 'batch' (season pack)
    total_files: Optional[int] = None  # episodes a batch is expected to yield
    done_files: Optional[int] = None   # episodes a batch has imported so far


class NyaaResult(BaseModel):
    nyaa_id: Optional[str] = None
    title: str
    magnet: Optional[str] = None
    torrent_url: Optional[str] = None
    size: Optional[str] = None
    seeders: Optional[int] = None
    leechers: Optional[int] = None
    trusted: bool = False
    timestamp: Optional[str] = None


class RssFollow(BaseModel):
    id: int
    anilist_id: Optional[int] = None
    title: Optional[str] = None
    query: str
    category: str = "1_2"
    trusted_only: bool = True
    resolution: str = "1080p"
    enabled: bool = True


class ReleaseOption(NyaaResult):
    """A nyaa release candidate (single episode OR a batch/season pack), with
    parsed metadata and a recommendation flag (the top pick — heuristic,
    optionally refined by Haiku)."""
    release_group: Optional[str] = None
    resolution: Optional[str] = None
    parsed_episode: Optional[int] = None
    recommended: bool = False
    reason: Optional[str] = None
    # batch-only fields (default-safe so the single-episode picker is unaffected)
    kind: str = "single"               # 'single' | 'batch'
    episode_span: Optional[str] = None  # e.g. "01–25", "Complete", "S1+S2"
    episode_count: Optional[int] = None  # episodes the pack is expected to contain


class EpisodeReleases(BaseModel):
    """The release-picker payload for one episode."""
    episode_id: int
    ep_number: Optional[int] = None
    title: Optional[str] = None
    releases: list[ReleaseOption] = []
    matched_by: str = "heuristic"   # heuristic | haiku
    reason: Optional[str] = None


class SeasonReleases(BaseModel):
    """The batch-picker payload for a whole title/season."""
    anilist_id: int
    title: Optional[str] = None
    total_episodes: Optional[int] = None
    releases: list[ReleaseOption] = []
    matched_by: str = "heuristic"   # heuristic | haiku
    reason: Optional[str] = None
