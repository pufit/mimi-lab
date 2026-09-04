// Mirrors app/models.py (SRS half) — keep field names exact, byte-for-byte.
// Contract: notes/SRS_DESIGN.md §7.1 (shapes) and §7.2 (endpoints).
// Types only: no runtime values live in this file.

export type SrsState =
  | "new"
  | "learning"
  | "review"
  | "relearning"
  | "known"
  | "suspended"
  | "rejected";
export type SrsRating = 1 | 2 | 3 | 4;
export type SrsClipStatus = "pending" | "ready" | "failed" | "no_source" | "missing";
export type SrsAction =
  | "study_next"
  | "bottom"
  | "bury"
  | "unbury"
  | "suspend"
  | "resume"
  | "reject"
  | "restore"
  | "known"
  | "unknown"
  | "forget"
  | "demote";
export type SrsResortBy = "score" | "frequency" | "leverage" | "show" | "random";
export type SrsJudgeStatus =
  | "unjudged"
  | "filtered"
  | "pending"
  | "accepted"
  | "rejected"
  | "probably_known"
  | "error";
export type SrsTranslationSource = "human" | "mt" | "user" | null;

/** SrsToken */
export interface SrsToken {
  surface: string;
  reading: string | null;
  is_target: boolean;
  status: "KNOWN" | "IGNORED" | "UNKNOWN";
}

/** SrsContextLine */
export interface SrsContextLine {
  line_id: number | null;
  idx: number;
  start_ms: number;
  end_ms: number;
  text: string;
  text_furigana: string | null;
  translation: string | null;
  is_target: boolean;
}

/**
 * The role an extra line plays inside the moment.
 *
 * `evidence` — the meaning of the target line depends on it, so it is part of
 * the clip window *and* shown on the card front (Japanese only, dimmed).
 * `continuation` — the sentence spills into the next cue; clip-window only.
 * Rows written before the evidence pass carry no `role`; treat them as
 * `continuation` (that is all `extend` ever held).
 */
export type SrsLineRole = "evidence" | "continuation";

/**
 * SrsLineRef (extend) — one extra line the moment includes: an `evidence`
 * neighbour the meaning depends on, or the `continuation` cue the sentence runs
 * into. Rows written before evidence lines existed carry no `role`/`idx`.
 */
export interface SrsLineRef {
  line_id: number | null;
  /** Dialogue index within the episode; `null` on pre-evidence rows. */
  idx: number | null;
  start_ms: number;
  end_ms: number;
  text: string;
  /** `null` on pre-evidence rows → read as `continuation`. */
  role: SrsLineRole | null;
}

/** SrsClip */
export interface SrsClip {
  status: SrsClipStatus;
  video_url: string | null;
  poster_url: string | null;
  start_ms: number | null;
  end_ms: number | null;
  bytes: number | null;
  error: string | null;
  version: number;
}

/** SrsIntervalPreview */
export interface SrsIntervalPreview {
  again: string;
  hard: string;
  good: string;
  easy: string;
}

/** SrsCard */
export interface SrsCard {
  id: number;
  lemma: string;
  reading: string | null;
  pos: string | null;
  gloss: string | null;
  meaning_short: string | null;
  meaning_full: string | null;
  why_clear: string | null;
  usage_note: string | null;
  tags: string[];
  freq_rank: number | null;
  source: "auto" | "manual" | "curated-initial" | "confirm";
  score: number | null;
  clarity: number | null;
  usefulness: number | null;
  priority: number | null;
  line_id: number | null;
  episode_id: number | null;
  anilist_id: number | null;
  show_title: string | null;
  ep_number: number | null;
  start_ms: number;
  end_ms: number;
  text: string;
  text_furigana: string | null;
  translation: string | null;
  translation_source: SrsTranslationSource;
  target_surface: string;
  tokens: SrsToken[];
  tokens_source: "migaku" | "local" | null;
  context: SrsContextLine[];
  extend: SrsLineRef[];
  alt_moment_ids: number[];
  clip: SrsClip;
  state: SrsState;
  queue_pos: number | null;
  study_now: boolean;
  buried_until: string | null;
  stability: number | null;
  difficulty: number | null;
  step: number | null;
  due_at: string | null;
  last_review_at: string | null;
  scheduled_days: number;
  reps: number;
  lapses: number;
  fail_count: number;
  demoted_count: number;
  demoted_at: string | null;
  introduced_at: string | null;
  known_source: "srs" | "user" | "migaku" | "sibling" | null;
  known_at: string | null;
  suspend_reason: string | null;
  notes: string | null;
  /** derived KNOWN_SQL */
  is_known: boolean;
  /** episode video on disk right now (Watch scene / Regenerate / Swap enabled) */
  source_available: boolean;
  /** subtitle_lines row resolvable after relink (transcript link) */
  line_available: boolean;
  created_at: string;
  updated_at: string;
  /** only on queue cards */
  preview?: SrsIntervalPreview | null;
}

/** SrsMoment */
export interface SrsMoment {
  id: number;
  lemma: string;
  line_id: number | null;
  episode_id: number;
  show_title: string | null;
  ep_number: number | null;
  text: string;
  text_furigana: string | null;
  translation: string | null;
  translation_source: SrsTranslationSource;
  translation_shared: boolean;
  start_ms: number;
  end_ms: number;
  has_video: boolean;
  target_surface: string | null;
  other_unknowns: number | null;
  line_score: number | null;
  clarity: number | null;
  translation_renders_word: boolean | null;
  clean_utterance: boolean | null;
  accepted: boolean | null;
  verdict: "accept" | "reject" | "user" | "user_rejected" | null;
  note: string | null;
  judged_at: string | null;
  is_primary: boolean;
  /** Dialogue index of the moment's line (side-splits its evidence lines). */
  idx: number | null;
  /** The lines a swap onto this moment would carry: same-cue half + the
   * evidence lines the blind judge leaned on (role `evidence`), continuation. */
  extend: SrsLineRef[];
  /** Unpadded span of target + extend — what Preview should play. */
  window_start_ms: number | null;
  window_end_ms: number | null;
  /** Another card of this word already shows this moment (not a swap choice). */
  used_by_card_id: number | null;
  used_by_state: string | null;
}

/** Another card of the same word (multi-card words). */
export interface SrsSiblingCard {
  id: number;
  state: string;
  show_title: string | null;
  ep_number: number | null;
  text: string;
  clarity: number | null;
  queue_pos: number | null;
  due_at: string | null;
  clip_status: string;
}

/** SrsReview */
export interface SrsReview {
  id: number;
  reviewed_at: string;
  review_day: string;
  rating: SrsRating;
  state_before: SrsState;
  state_after: SrsState;
  scheduled_days: number;
  elapsed_ms: number | null;
  counted_fail: boolean;
  demoted: boolean;
  undone: boolean;
}

/** SrsCardDetail */
export interface SrsCardDetail {
  card: SrsCard;
  moments: SrsMoment[];
  reviews: SrsReview[];
  /** The word's other cards (multi-card words). */
  siblings: SrsSiblingCard[];
}

/** SrsCardList */
export interface SrsCardList {
  items: SrsCard[];
  total: number;
}

/** SrsQueueCounts */
export interface SrsQueueCounts {
  learning_due: number;
  review_due: number;
  new_left_today: number;
  new_stack_total: number;
  reviewed_today: number;
  clips_pending: number;
}

/** SrsQueue */
export interface SrsQueue {
  cards: SrsCard[];
  learning_soon: SrsCard[];
  server_time: string;
  day: string;
  counts: SrsQueueCounts;
}

/** SrsReviewRequest */
export interface SrsReviewRequest {
  card_id: number;
  rating: SrsRating;
  elapsed_ms: number | null;
  client_id: string;
}

/** SrsReviewResult */
export interface SrsReviewResult {
  card: SrsCard;
  review_id: number;
  duplicate: boolean;
  demoted: boolean;
  suspended: boolean;
  graduated: boolean;
  became_known: boolean;
  known_crossed: "up" | "down" | null;
  next_due_at: string | null;
  message: string | null;
}

/** SrsReviewConflict (409 body) */
export interface SrsReviewConflict {
  detail: string;
  card: SrsCard;
}

/** SrsUndoRequest */
export interface SrsUndoRequest {
  review_id?: number | null;
}

/** SrsUndoResult */
export interface SrsUndoResult {
  card: SrsCard;
  undone_review_id: number;
}

/** SrsBulkResult */
export interface SrsBulkResult {
  updated: number;
  previous: { card_id: number; state: SrsState; queue_pos: number | null }[];
}

/** SrsGenerationRun */
export interface SrsGenerationRun {
  id: number;
  trigger: string;
  state: string;
  want: number | null;
  words_scored: number;
  words_planned: number;
  batches_planned: number;
  batches_done: number;
  moments_judged: number;
  accepted: number;
  rejected: number;
  cards_created: number;
  llm_calls: number;
  llm_in_tokens: number;
  llm_out_tokens: number;
  error: string | null;
  started_at: string;
  finished_at: string | null;
}

/** SrsGeneration; running = open run WITH a live batch job */
export interface SrsGeneration {
  runs: SrsGenerationRun[];
  running: boolean;
  llm_available: boolean;
  judge_model_id: string;
  candidates_ready: number;
  probably_known: number;
  census_at: string | null;
}

/** SrsSummary */
export interface SrsSummary {
  day: string;
  server_time: string;
  due_learning: number;
  due_review: number;
  new_today_done: number;
  new_today_limit: number;
  new_available: number;
  new_stack_total: number;
  reviewed_today: number;
  again_today: number;
  time_today_ms: number;
  streak_days: number;
  next_due_at: string | null;
  review_cap_hit: boolean;
  /** ALWAYS all seven keys, zero-filled by the service */
  states: Record<SrsState, number>;
  known_total: number;
  known_week_delta: number;
  demoted_in_stack: number;
  generation: SrsGeneration;
  clips: {
    ready: number;
    pending: number;
    failed: number;
    no_source: number;
    missing: number;
    bytes: number;
  };
  /** the three inline Deck controls */
  settings: SrsSettings;
}

/** SrsCandidate */
export interface SrsCandidate {
  lemma: string;
  reading: string | null;
  gloss: string | null;
  freq_rank: number | null;
  pos1: string | null;
  score: number;
  occ: number;
  eps: number;
  iplus1_lines: number;
  moment_lines: number;
  leverage_crossings: number;
  next_watch_hits: number;
  migaku_status: string | null;
  in_unwatched: boolean;
  judge_status: SrsJudgeStatus;
  judge_reason: string | null;
  judge_note: string | null;
  judged_at: string | null;
  user_flag: string | null;
  card_id: number | null;
  best_moment: {
    moment_id: number;
    line_id: number | null;
    text: string;
    translation: string | null;
    show_title: string | null;
    ep_number: number | null;
  } | null;
}

/** SrsCandidates */
export interface SrsCandidates {
  items: SrsCandidate[];
  total: number;
  computed_at: string | null;
}

/** SrsImportReport */
export interface SrsImportReport {
  created: number;
  skipped_existing: number;
  clip_jobs_queued: number;
  errors: { lemma: string; reason: string }[];
}

/** SrsSettings (all optional on PUT → SrsSettingsPatch) */
export interface SrsSettings {
  new_per_day: number;
  demote_after_fails: number;
  moment_source: "any" | "watched_only";
}

/** SrsStats */
export interface SrsStats {
  days: {
    day: string;
    reviews: number;
    again: number;
    hard: number;
    good: number;
    easy: number;
    new_cards: number;
    time_ms: number;
    demotions: number;
  }[];
  /** next 30 SRS days, review-state cards */
  forecast: { day: string; due: number }[];
  retention_7d: number | null;
  retention_30d: number | null;
  /** states zero-filled */
  states: Record<SrsState, number>;
  known_total: number;
  avg_time_per_card_ms: number | null;
  streak_days: number;
  intervals: { bucket: string; count: number }[];
}

// ── Request bodies (§7.2; Python names from the §7 model list) ────────────

/** SrsSettingsPatch */
export interface SrsSettingsPatch {
  new_per_day?: number;
  demote_after_fails?: number;
  moment_source?: "any" | "watched_only";
}

/** SrsCreateCard */
export interface SrsCreateCard {
  lemma: string;
  line_id?: number | null;
  study_next?: boolean;
}

/** SrsPatchCard */
export interface SrsPatchCard {
  reading?: string | null;
  gloss?: string | null;
  meaning_short?: string | null;
  meaning_full?: string | null;
  usage_note?: string | null;
  notes?: string | null;
  target_surface?: string | null;
  translation?: string | null;
}

/** SrsActionRequest */
export interface SrsActionRequest {
  action: SrsAction;
}

/** SrsBulkRequest */
export interface SrsBulkRequest {
  card_ids: number[];
  action: SrsAction;
}

/** SrsSwapMoment — exactly one of the two */
export interface SrsSwapMoment {
  moment_id?: number | null;
  line_id?: number | null;
}

/** SrsMove */
export interface SrsMove {
  card_id: number;
  position: number;
}

/** SrsResort */
export interface SrsResort {
  by: SrsResortBy;
  keep_top?: number;
}

/** SrsGenerateRequest */
export interface SrsGenerateRequest {
  want?: number | null;
  lemma?: string | null;
}

/** SrsImportRequest */
export interface SrsImportRequest {
  path?: string | null;
}

// ── Response envelopes (§7.2 "Response" column, not standalone models) ────

/** `{card}` — PATCH/action/move/swap responses */
export interface SrsCardEnvelope {
  card: SrsCard;
}

/** `201 {card, warning}` or `202 {queued: true, lemma}` */
export interface SrsCreateCardResult {
  card?: SrsCard | null;
  warning?: string | null;
  queued?: boolean;
  lemma?: string;
}

/** 409 body of `POST /srs/cards` */
export interface SrsCreateCardConflict {
  detail: string;
  card_id: number;
}

/** `{card, queued: true}` — regenerate clip */
export interface SrsClipQueued {
  card: SrsCard;
  queued: boolean;
}

/** `202 {queued: true}` — find-moments */
export interface SrsQueued {
  queued: boolean;
}

/** `{updated}` — resort */
export interface SrsResortResult {
  updated: number;
}

/** `202 {queued: true, run_id}` — generate */
export interface SrsGenerateResult {
  queued: boolean;
  run_id: number | null;
}

/** `{candidate}` — word skip/unskip/confirm-known/judge */
export interface SrsWordActionResult {
  candidate: SrsCandidate;
}

// ── Client-side presentation prefs (localStorage `srs.prefs`, §8.2/§8.9) ──

export interface SrsPrefs {
  ratingMode: "four" | "two";
  frontFurigana: "none" | "target" | "all";
}
