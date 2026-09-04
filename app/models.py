"""Pydantic API models — the response/request contracts shared across modules."""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


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


# ---------------------------------------------------------------------------
# SRS (app/srs) — notes/SRS_DESIGN.md §7. Field names are the API contract and
# are mirrored byte-for-byte in web/src/lib/srs-types.ts (WP-C).
# ---------------------------------------------------------------------------

SrsState = Literal["new", "learning", "review", "relearning", "known", "suspended", "rejected"]
SrsRating = Literal[1, 2, 3, 4]
SrsClipStatus = Literal["pending", "ready", "failed", "no_source", "missing"]
SrsAction = Literal[
    "study_next", "bottom", "bury", "unbury", "suspend", "resume",
    "reject", "restore", "known", "unknown", "forget", "demote",
]
SrsResortBy = Literal["score", "frequency", "leverage", "show", "random"]
SrsJudgeStatus = Literal["unjudged", "filtered", "pending", "accepted", "rejected", "probably_known", "error"]
SrsTranslationSource = Optional[Literal["human", "mt", "user"]]

SRS_STATES: tuple[str, ...] = (
    "new", "learning", "review", "relearning", "known", "suspended", "rejected",
)


def zero_states() -> dict[str, int]:
    """All seven states at 0 — `states` is never sparse (§7.1: GROUP BY only
    returns non-empty states, the service zero-fills before returning)."""
    return {s: 0 for s in SRS_STATES}


class SrsToken(BaseModel):
    surface: str
    reading: Optional[str] = None
    is_target: bool = False
    status: Literal["KNOWN", "IGNORED", "UNKNOWN"] = "UNKNOWN"


class SrsContextLine(BaseModel):
    line_id: Optional[int] = None
    idx: int = 0
    start_ms: int = 0
    end_ms: int = 0
    text: str = ""
    text_furigana: Optional[str] = None
    translation: Optional[str] = None
    is_target: bool = False


class SrsLineRef(BaseModel):
    """One extra line the moment includes (`srs_cards.extend_json`): an
    `evidence` neighbour the meaning depends on, or the `continuation` cue the
    sentence runs into. Rows written before evidence lines existed carry no
    `role`/`idx` (read as a continuation cue)."""
    line_id: Optional[int] = None
    idx: Optional[int] = None
    start_ms: int = 0
    end_ms: int = 0
    text: str = ""
    role: Optional[Literal["evidence", "continuation"]] = None


class SrsClip(BaseModel):
    status: SrsClipStatus = "pending"
    video_url: Optional[str] = None
    poster_url: Optional[str] = None
    start_ms: Optional[int] = None
    end_ms: Optional[int] = None
    bytes: Optional[int] = None
    error: Optional[str] = None
    version: int = 1


class SrsIntervalPreview(BaseModel):
    again: str
    hard: str
    good: str
    easy: str


class SrsCard(BaseModel):
    id: int
    lemma: str
    reading: Optional[str] = None
    pos: Optional[str] = None
    gloss: Optional[str] = None
    meaning_short: Optional[str] = None
    meaning_full: Optional[str] = None
    why_clear: Optional[str] = None
    usage_note: Optional[str] = None
    tags: list[str] = []
    freq_rank: Optional[int] = None
    source: Literal["auto", "manual", "curated-initial", "confirm"] = "auto"
    score: Optional[float] = None
    clarity: Optional[float] = None
    usefulness: Optional[float] = None
    priority: Optional[int] = None
    line_id: Optional[int] = None
    episode_id: Optional[int] = None
    anilist_id: Optional[int] = None
    show_title: Optional[str] = None
    ep_number: Optional[int] = None
    start_ms: int = 0
    end_ms: int = 0
    text: str = ""
    text_furigana: Optional[str] = None
    translation: Optional[str] = None
    translation_source: SrsTranslationSource = None
    target_surface: str = ""
    tokens: list[SrsToken] = []
    tokens_source: Optional[Literal["migaku", "local"]] = None
    context: list[SrsContextLine] = []
    extend: list[SrsLineRef] = []
    alt_moment_ids: list[int] = []
    clip: SrsClip = SrsClip()
    state: SrsState = "new"
    queue_pos: Optional[int] = None
    study_now: bool = False
    buried_until: Optional[str] = None
    stability: Optional[float] = None
    difficulty: Optional[float] = None
    step: Optional[int] = None
    due_at: Optional[str] = None
    last_review_at: Optional[str] = None
    scheduled_days: int = 0
    reps: int = 0
    lapses: int = 0
    fail_count: int = 0
    demoted_count: int = 0
    demoted_at: Optional[str] = None
    introduced_at: Optional[str] = None
    known_source: Optional[Literal["srs", "user", "migaku", "sibling"]] = None
    known_at: Optional[str] = None
    suspend_reason: Optional[str] = None
    notes: Optional[str] = None
    is_known: bool = False           # derived KNOWN_SQL
    source_available: bool = False   # episode video on disk right now
    line_available: bool = False     # subtitle_lines row resolvable after relink
    created_at: str = ""
    updated_at: str = ""
    preview: Optional[SrsIntervalPreview] = None   # only on queue cards


class SrsMoment(BaseModel):
    id: int
    lemma: str
    line_id: Optional[int] = None
    episode_id: int
    show_title: Optional[str] = None
    ep_number: Optional[int] = None
    text: str = ""
    text_furigana: Optional[str] = None
    translation: Optional[str] = None
    translation_source: SrsTranslationSource = None
    translation_shared: bool = False
    start_ms: int = 0
    end_ms: int = 0
    has_video: bool = False
    target_surface: Optional[str] = None
    other_unknowns: Optional[int] = None
    line_score: Optional[float] = None
    clarity: Optional[float] = None
    translation_renders_word: Optional[bool] = None
    clean_utterance: Optional[bool] = None
    accepted: Optional[bool] = None
    verdict: Optional[Literal["accept", "reject", "user", "user_rejected"]] = None
    # the moment's own context (2026-09-03): the lines a swap would carry —
    # same-cue half + the evidence lines the blind judge leaned on — and the
    # raw span they cover (target + extend, unpadded) for previews
    idx: Optional[int] = None
    extend: list[SrsLineRef] = []
    window_start_ms: Optional[int] = None
    window_end_ms: Optional[int] = None
    # another card of this word already shows this moment (it is not a swap
    # choice; the UI lists it under the word's other cards)
    used_by_card_id: Optional[int] = None
    used_by_state: Optional[str] = None
    note: Optional[str] = None
    judged_at: Optional[str] = None
    is_primary: bool = False


class SrsReview(BaseModel):
    id: int
    reviewed_at: str
    review_day: str
    rating: SrsRating
    state_before: SrsState
    state_after: SrsState
    scheduled_days: int = 0
    elapsed_ms: Optional[int] = None
    counted_fail: bool = False
    demoted: bool = False
    undone: bool = False


class SrsSiblingCard(BaseModel):
    """Another card of the same word (multi-card words, 2026-09-03)."""
    id: int
    state: str
    show_title: Optional[str] = None
    ep_number: Optional[int] = None
    text: str = ""
    clarity: Optional[float] = None
    queue_pos: Optional[int] = None
    due_at: Optional[str] = None
    clip_status: str = "pending"


class SrsCardDetail(BaseModel):
    card: SrsCard
    moments: list[SrsMoment] = []
    reviews: list[SrsReview] = []
    siblings: list[SrsSiblingCard] = []


class SrsCardList(BaseModel):
    items: list[SrsCard] = []
    total: int = 0


class SrsQueueCounts(BaseModel):
    learning_due: int = 0
    review_due: int = 0
    new_left_today: int = 0
    new_stack_total: int = 0
    reviewed_today: int = 0
    clips_pending: int = 0


class SrsQueue(BaseModel):
    cards: list[SrsCard] = []
    learning_soon: list[SrsCard] = []
    server_time: str
    day: str
    counts: SrsQueueCounts = SrsQueueCounts()


class SrsReviewRequest(BaseModel):
    card_id: int
    rating: SrsRating
    elapsed_ms: Optional[int] = None
    client_id: str


class SrsReviewResult(BaseModel):
    card: SrsCard
    review_id: int
    duplicate: bool = False
    demoted: bool = False
    suspended: bool = False
    graduated: bool = False
    became_known: bool = False
    known_crossed: Optional[Literal["up", "down"]] = None
    next_due_at: Optional[str] = None
    message: Optional[str] = None


class SrsReviewConflict(BaseModel):
    """409 body of POST /srs/review — carries the current card so the client can
    drop it from the session."""
    detail: str
    card: SrsCard


class SrsUndoRequest(BaseModel):
    review_id: Optional[int] = None


class SrsUndoResult(BaseModel):
    card: SrsCard
    undone_review_id: int


class SrsBulkPrevious(BaseModel):
    """One entry of SrsBulkResult.previous — enough to issue the exact inverse."""
    card_id: int
    state: SrsState
    queue_pos: Optional[int] = None


class SrsBulkResult(BaseModel):
    updated: int = 0
    previous: list[SrsBulkPrevious] = []


class SrsGenerationRun(BaseModel):
    id: int
    trigger: str
    state: str
    want: Optional[int] = None
    words_scored: int = 0
    words_planned: int = 0
    batches_planned: int = 0
    batches_done: int = 0
    moments_judged: int = 0
    accepted: int = 0
    rejected: int = 0
    cards_created: int = 0
    llm_calls: int = 0
    llm_in_tokens: int = 0
    llm_out_tokens: int = 0
    error: Optional[str] = None
    started_at: str = ""
    finished_at: Optional[str] = None


class SrsGeneration(BaseModel):
    runs: list[SrsGenerationRun] = []
    running: bool = False            # open run WITH a live batch job
    llm_available: bool = False
    judge_model_id: str = ""
    candidates_ready: int = 0
    probably_known: int = 0
    census_at: Optional[str] = None


class SrsSettings(BaseModel):
    """kv `srs.settings` (§9.5). Three learner-facing choices; everything else is
    a constant in app/srs/constants.py."""
    new_per_day: int = Field(default=10, ge=0, le=50)
    demote_after_fails: int = Field(default=2, ge=0, le=5)   # 0 = off
    moment_source: Literal["any", "watched_only"] = "any"


class SrsSettingsPatch(BaseModel):
    """PUT /srs/settings body — every field optional (partial update)."""
    new_per_day: Optional[int] = Field(default=None, ge=0, le=50)
    demote_after_fails: Optional[int] = Field(default=None, ge=0, le=5)
    moment_source: Optional[Literal["any", "watched_only"]] = None


class SrsClipCounts(BaseModel):
    ready: int = 0
    pending: int = 0
    failed: int = 0
    no_source: int = 0
    missing: int = 0
    bytes: int = 0


class SrsSummary(BaseModel):
    day: str
    server_time: str
    due_learning: int = 0
    due_review: int = 0
    new_today_done: int = 0
    new_today_limit: int = 0
    new_available: int = 0
    new_stack_total: int = 0
    reviewed_today: int = 0
    again_today: int = 0
    time_today_ms: int = 0
    streak_days: int = 0
    next_due_at: Optional[str] = None
    review_cap_hit: bool = False
    states: dict[str, int] = Field(default_factory=zero_states)   # ALWAYS all seven keys
    known_total: int = 0
    known_week_delta: int = 0
    demoted_in_stack: int = 0
    generation: SrsGeneration = SrsGeneration()
    clips: SrsClipCounts = SrsClipCounts()
    settings: SrsSettings = SrsSettings()


class SrsCandidateMoment(BaseModel):
    """SrsCandidate.best_moment — the top-ranked moment of a candidate word."""
    moment_id: int
    line_id: Optional[int] = None
    text: str = ""
    translation: Optional[str] = None
    show_title: Optional[str] = None
    ep_number: Optional[int] = None


class SrsCandidate(BaseModel):
    lemma: str
    reading: Optional[str] = None
    gloss: Optional[str] = None
    freq_rank: Optional[int] = None
    pos1: Optional[str] = None
    score: float = 0.0
    occ: int = 0
    eps: int = 0
    iplus1_lines: int = 0
    moment_lines: int = 0
    leverage_crossings: int = 0
    next_watch_hits: int = 0
    migaku_status: Optional[str] = None
    in_unwatched: bool = False
    judge_status: SrsJudgeStatus = "unjudged"
    judge_reason: Optional[str] = None
    judge_note: Optional[str] = None
    judged_at: Optional[str] = None
    user_flag: Optional[str] = None
    card_id: Optional[int] = None
    best_moment: Optional[SrsCandidateMoment] = None


class SrsCandidates(BaseModel):
    items: list[SrsCandidate] = []
    total: int = 0
    computed_at: Optional[str] = None


class SrsImportError(BaseModel):
    lemma: str
    reason: str


class SrsImportReport(BaseModel):
    created: int = 0
    skipped_existing: int = 0
    clip_jobs_queued: int = 0
    errors: list[SrsImportError] = []


class SrsStatsDay(BaseModel):
    day: str
    reviews: int = 0
    again: int = 0
    hard: int = 0
    good: int = 0
    easy: int = 0
    new_cards: int = 0
    time_ms: int = 0
    demotions: int = 0


class SrsStatsForecastDay(BaseModel):
    day: str
    due: int = 0


class SrsStatsInterval(BaseModel):
    bucket: str
    count: int = 0


class SrsStats(BaseModel):
    days: list[SrsStatsDay] = []
    forecast: list[SrsStatsForecastDay] = []       # next 30 SRS days, review-state cards
    retention_7d: Optional[float] = None
    retention_30d: Optional[float] = None
    states: dict[str, int] = Field(default_factory=zero_states)
    known_total: int = 0
    avg_time_per_card_ms: Optional[float] = None
    streak_days: int = 0
    intervals: list[SrsStatsInterval] = []


# --- request bodies (§7.2) -------------------------------------------------

class SrsCreateCard(BaseModel):
    lemma: str
    line_id: Optional[int] = None
    study_next: bool = False


class SrsPatchCard(BaseModel):
    reading: Optional[str] = None
    gloss: Optional[str] = None
    meaning_short: Optional[str] = None
    meaning_full: Optional[str] = None
    usage_note: Optional[str] = None
    notes: Optional[str] = None
    target_surface: Optional[str] = None
    translation: Optional[str] = None


class SrsActionRequest(BaseModel):
    action: SrsAction


class SrsBulkRequest(BaseModel):
    card_ids: list[int]
    action: SrsAction


class SrsSwapMoment(BaseModel):
    """Exactly one of moment_id / line_id."""
    moment_id: Optional[int] = None
    line_id: Optional[int] = None


class SrsMove(BaseModel):
    card_id: int
    position: int          # 0-based, absolute, among state='new'


class SrsResort(BaseModel):
    by: SrsResortBy
    keep_top: int = 0


class SrsGenerateRequest(BaseModel):
    want: Optional[int] = None
    lemma: Optional[str] = None


class SrsImportRequest(BaseModel):
    path: Optional[str] = None      # default data/srs-curation/cards.json; must be under ROOT/data
