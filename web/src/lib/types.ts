// Mirrors app/models.py — keep field names exact.

export type * from "./srs-types";

export interface Title {
  anilist_id: number;
  mal_id?: number | null;
  romaji?: string | null;
  english?: string | null;
  native?: string | null;
  format?: string | null;
  total_episodes?: number | null;
  season?: string | null;
  year?: number | null;
  cover_url?: string | null;
  banner_url?: string | null;
  description?: string | null;
  status?: string | null;
  mal_status?: string | null;
  mal_score?: number | null;
  mal_progress?: number | null;
  episode_count_local: number;
  avg_comprehension?: number | null;
}

export interface Episode {
  id: number;
  anilist_id: number;
  ep_number: number;
  title?: string | null;
  video_path?: string | null;
  codec?: string | null;
  container?: string | null;
  duration_ms?: number | null;
  watched: boolean;
  has_subtitle: boolean; // Japanese (study) subtitle
  has_english: boolean; // English secondary/reference track
  /** In-browser watch position (ms), when a fallback-player session exists. */
  watch_progress_ms?: number | null;
  download_state?: string | null; // may include 'pp_failed' / 'lost'
  download_id?: number | null;
  comprehension_pct?: number | null;
  comprehension_rating?: string | null;
  comprehension_source?: string | null;
  new_word_count?: number | null;
}

export interface NewWord {
  lemma: string;
  reading?: string | null;
  freq_rank?: number | null;
  gloss?: string | null;
  count: number;
}

export interface ComprehensionResult {
  episode_id: number;
  comprehension_pct: number;
  rating?: string | null;
  source: string; // aligned | exact
  total_tokens: number;
  known_tokens: number;
  unknown_unique: number;
  new_words: NewWord[];
}

export interface Moment {
  line_id: number;
  anilist_id: number;
  episode_id: number;
  /** Show name (was previously the episode title). */
  title?: string | null;
  episode_title?: string | null;
  ep_number?: number | null;
  /** Unknown-word count for the line — set when sorting by i+1 minability. */
  unknown_count?: number | null;
  text: string;
  text_furigana?: string | null;
  translation?: string | null;
  start_ms: number;
  end_ms: number;
  video_path?: string | null;
  /** Null when the episode has no local video. */
  image_url?: string | null;
  /** Null when the episode has no local video. */
  audio_url?: string | null;
}

export type MomentSort = "position" | "iplus1";

export interface TranslateResult {
  line_id: number;
  translation: string | null;
  cached: boolean;
}

export interface EnglishConfig {
  enabled: boolean;
  source: "human" | "llm";
  default_source: string;
  options: string[];
  translation_model: string;
  anthropic_configured: boolean;
}

export interface KnownWordsSummary {
  total: number;
  known: number;
  learning: number;
  unknown: number;
  ignored: number;
  updated_at?: string | null;
}

export interface KnownGrowth {
  known: number;
  known_prev: number;
  delta: number;
  since?: string | null;
  days: number;
}

export interface SweetSpotItem {
  episode_id: number;
  anilist_id: number;
  ep_number: number;
  title: string;
  cover_url?: string | null;
  comprehension_pct: number;
  comprehension_rating?: string | null;
  comprehension_source?: string | null;
  new_word_count?: number | null;
  watched: boolean;
  has_video: boolean;
  /** How many upcoming episodes of this show sit in the requested band. */
  band_episodes: number;
}

export interface Download {
  id: number;
  title_guess?: string | null;
  state: string;
  progress: number;
  anilist_id?: number | null;
  ep_number?: number | null;
  release_group?: string | null;
  resolution?: string | null;
  size_bytes?: number | null;
  kind?: string; // 'single' | 'batch'
  total_files?: number | null;
  done_files?: number | null;
}

export interface NyaaResult {
  nyaa_id?: string | null;
  title: string;
  magnet?: string | null;
  torrent_url?: string | null;
  size?: string | null;
  seeders?: number | null;
  leechers?: number | null;
  trusted: boolean;
  timestamp?: string | null;
}

export interface RssFollow {
  id: number;
  anilist_id?: number | null;
  title?: string | null;
  query: string;
  category: string;
  trusted_only: boolean;
  resolution: string;
  enabled: boolean;
}

export interface ReleaseOption extends NyaaResult {
  release_group?: string | null;
  resolution?: string | null;
  parsed_episode?: number | null;
  recommended: boolean;
  reason?: string | null;
  // batch-only (default-safe; absent on single-episode releases)
  kind?: string; // 'single' | 'batch'
  episode_span?: string | null; // "01–25", "Complete", "S1+S2"
  episode_count?: number | null;
}

export interface EpisodeReleases {
  episode_id: number;
  ep_number?: number | null;
  title?: string | null;
  releases: ReleaseOption[];
  matched_by: string; // heuristic | haiku
  reason?: string | null;
}

export interface SeasonReleases {
  anilist_id: number;
  title?: string | null;
  total_episodes?: number | null;
  releases: ReleaseOption[];
  matched_by: string; // heuristic | haiku
  reason?: string | null;
}

export interface MatchCandidate {
  anilist_id: number;
  romaji?: string | null;
  english?: string | null;
  format?: string | null;
  episodes?: number | null;
  year?: number | null;
  cover_url?: string | null;
  score: number;
  reason?: string | null;
}

export interface MatchQueueItem {
  id: number;
  filename: string;
  ep_number?: number | null;
  title_guess?: string | null;
  candidates: MatchCandidate[];
  chosen_anilist_id?: number | null;
  state: string;
}

export interface MatchSuggestion {
  suggested_title: string;
  candidates: MatchCandidate[];
  matched_by: string; // haiku | fallback
}

// One watching machine's Connector, as listed in ConnectorStatus.devices.
export interface ConnectorDeviceStatus {
  device_id: string;
  device_name?: string | null;
  connected: boolean;
  chrome: boolean;
  migaku: boolean;
  last_seen?: number | null;
  version?: string | null;
  ext_version?: string | null;
  /** True until the Connector names itself (pre-multi-device bundles). */
  provisional?: boolean;
}

// The user-side Connector(s) (replaces the old server-side Companion). Reported
// over /api/connector/status. The aggregate fields keep the old single-device
// shape (connected/chrome/migaku are true if ANY device has them); `devices`
// lists every machine, and `default_device_id` is where an untargeted Play goes.
export interface ConnectorStatus {
  connected: boolean;
  chrome: boolean;
  migaku: boolean;
  last_seen?: number | null;
  version?: string | null;
  /** Migaku browser-extension version, when a Connector could read it. */
  ext_version?: string | null;
  devices?: ConnectorDeviceStatus[];
  device_count?: number;
  default_device_id?: string | null;
}

// Copy-paste setup details shown on Settings → Connector.
export interface ConnectorSetup {
  server_url: string;
  ws_url: string;
  has_token: boolean;
  token: string;
  install_cmd: string; // one-line, self-updating installer for the watching machine
}

export interface ClipResult {
  image_url?: string | null;
  audio_url?: string | null;
}

export interface QbtStatus {
  available: boolean;
}

// GET /api/mal/status — timestamps are unix SECONDS.
export interface MalStatus {
  configured: boolean;
  authed: boolean;
  has_refresh_token: boolean;
  expires_at?: number | null;
  last_sync?: number | null;
  synced_titles?: number | null;
}

// ── Notifications / events feed ──────────────────────────────
export interface AppEvent {
  id: number;
  kind: "info" | "success" | "warning" | "error";
  category?: string | null;
  title: string;
  detail?: string | null;
  created_at: string;
  read: boolean;
}

// ── Leverage / study queue ───────────────────────────────────
export interface LeverageWord {
  lemma: string;
  reading?: string | null;
  occurrences: number;
  episodes: number;
  /** Episodes this word pushes across the comprehension target. */
  crossings: number;
  /** Episodes unlocked if you learn every word up to and including this one. */
  cumulative_unlocked: number;
  gloss: string | null;
  freq_rank: number | null;
}

export interface LeverageResponse {
  words: LeverageWord[];
  band: [number, number];
  target: number;
  episodes_in_band: number;
  computed_at?: string | number | null;
  computed_in_s?: number | null;
}

// ── Transcript reader ────────────────────────────────────────
export type TokenStatus = "KNOWN" | "UNKNOWN" | "LEARNING" | "IGNORED";

export interface TranscriptToken {
  surface: string;
  dict_form: string;
  reading: string;
  status: TokenStatus;
}

export interface TranscriptLine {
  line_id: number;
  idx: number;
  start_ms: number;
  end_ms: number;
  text: string;
  text_furigana: string | null;
  translation: string | null;
  tokens: TranscriptToken[];
}

export interface TranscriptResponse {
  episode_id: number;
  title: string;
  ep_number: number;
  anilist_id: number;
  has_video: boolean;
  comprehension_pct: number | null;
  source: "migaku-local" | "local";
  lines: TranscriptLine[];
}

// ── Stats dashboard ──────────────────────────────────────────
export interface KnownSeriesPoint {
  captured_at: string;
  known: number;
  learning: number;
  unknown: number;
  ignored: number;
}

export interface ComprehensionSeriesPoint {
  captured_at: string;
  scored_episodes: number;
  avg_pct: number;
  sweet_count: number;
  almost_count: number;
  easy_count: number;
  hard_count: number;
}

export interface WatchedDay {
  day: string;
  /** completed viewings that day (rewatches included) */
  episodes: number;
  rewatches: number;
  minutes: number;
}

export interface StatsNow {
  scored: number;
  avg_pct: number;
  sweet: number;
  almost: number;
  easy: number;
  hard: number;
}

export interface StatsResponse {
  known_series: KnownSeriesPoint[];
  comprehension_series: ComprehensionSeriesPoint[];
  /** unique episodes ever watched */
  watched_total: number;
  /** completed viewings incl. rewatches (from watch_history) */
  total_views: number;
  rewatches: number;
  watched_minutes_total: number;
  watched_by_day: WatchedDay[];
  in_progress: number;
  now: StatsNow;
}

// ── Continue watching rail ───────────────────────────────────
export interface ContinueEntry {
  kind: "resume" | "next";
  episode_id: number;
  anilist_id: number;
  ep_number: number;
  title: string;
  cover_url?: string | null;
  progress_ms: number | null;
  duration_ms: number | null;
  comprehension_pct?: number | null;
}

// ── Browser fallback player ──────────────────────────────────
export interface BrowserPlayInfo {
  episode_id: number;
  video_url: string;
  /** Already ?format=vtt; null when no Japanese track. */
  sub_url: string | null;
  sub2_url: string | null;
  seek_ms: number | null;
}

// ── System health ────────────────────────────────────────────
export interface HealthComponentBase {
  ok: boolean;
}

export interface HealthReport {
  ok: boolean;
  components: {
    db?: HealthComponentBase & { size_bytes?: number | null; wal_bytes?: number | null };
    disk?: HealthComponentBase & { free_gb?: number | null; total_gb?: number | null };
    jobs?: HealthComponentBase & {
      queued?: number;
      running?: number;
      error?: number;
      parked?: number;
      oldest_queued?: string | null;
    };
    tokenizer?: HealthComponentBase & { ext_version?: string | null };
    transmission?: HealthComponentBase;
    connector?: HealthComponentBase & {
      connected?: boolean;
      chrome?: boolean;
      migaku?: boolean;
      version?: string | null;
      ext_version?: string | null;
      last_seen?: number | null;
    };
    mal?: HealthComponentBase & {
      configured?: boolean;
      authed?: boolean;
      expires_in_days?: number | null;
      last_sync?: number | null;
    };
    jimaku?: HealthComponentBase & { configured?: boolean };
    anthropic?: HealthComponentBase & {
      configured?: boolean;
      usage?: Record<string, { in: number; out: number; calls: number }> | null;
    };
  };
}

// ── Job queue admin ──────────────────────────────────────────
export type JobState = "queued" | "running" | "done" | "error" | "parked" | "cancelled";

export interface JobRow {
  id: number;
  type: string;
  state: JobState;
  attempts: number;
  priority: number;
  payload_json?: string | null;
  last_error?: string | null;
  created_at: string;
  updated_at: string;
  run_after?: string | null;
}

export interface JobStats {
  states: Record<string, number>;
  errors_by_type: { type: string; n: number }[];
  oldest_queued?: string | null;
  registered: string[];
}

// ── Migaku integration health ────────────────────────────────
/** Tokenizer drift self-test — shape is intentionally loose. */
export interface MigakuDriftReport {
  ok?: boolean;
  [k: string]: unknown;
}

export interface SelfcheckResult {
  ok: boolean;
  checks: {
    played?: boolean;
    tokenized?: boolean;
    panel_scraped?: boolean;
  };
  stats?: Record<string, unknown> | null;
  drift?: Record<string, unknown> | null;
  error?: string | null;
  ext_version?: string | null;
}
