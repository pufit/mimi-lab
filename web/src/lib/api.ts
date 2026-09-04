import type {
  AppEvent,
  BrowserPlayInfo,
  ClipResult,
  ConnectorSetup,
  ConnectorStatus,
  ComprehensionResult,
  ContinueEntry,
  Download,
  EnglishConfig,
  Episode,
  EpisodeReleases,
  HealthReport,
  JobRow,
  JobStats,
  KnownGrowth,
  KnownWordsSummary,
  LeverageResponse,
  SweetSpotItem,
  MalStatus,
  MatchCandidate,
  MatchQueueItem,
  MatchSuggestion,
  MigakuDriftReport,
  Moment,
  MomentSort,
  NewWord,
  NyaaResult,
  QbtStatus,
  ReleaseOption,
  RssFollow,
  SeasonReleases,
  SelfcheckResult,
  StatsResponse,
  Title,
  TranscriptResponse,
  TranslateResult,
} from "./types";

/**
 * `body` carries the parsed JSON error payload when the server sent one, so a
 * caller can read a typed conflict body (e.g. `SrsReviewConflict` on a 409 from
 * `POST /srs/review`, `SrsCreateCardConflict` on `POST /srs/cards`) instead of
 * re-parsing a string. Use the generic parameter at the call site:
 * `(e as ApiError<SrsReviewConflict>).body?.card`.
 */
export class ApiError<B = unknown> extends Error {
  status: number;
  detail?: string;
  body?: B;
  constructor(status: number, message: string, detail?: string, body?: B) {
    super(message);
    this.status = status;
    this.detail = detail;
    this.body = body;
  }
}

/** Narrow an unknown error to a typed `ApiError` body (undefined when absent). */
export function apiErrorBody<B>(e: unknown): B | undefined {
  return e instanceof ApiError ? (e.body as B | undefined) : undefined;
}

export async function req<T>(
  path: string,
  init?: RequestInit & { json?: unknown },
): Promise<T> {
  const { json, ...rest } = init ?? {};
  const headers = new Headers(rest.headers);
  let body = rest.body;
  if (json !== undefined) {
    headers.set("Content-Type", "application/json");
    body = JSON.stringify(json);
  }
  const res = await fetch(`/api${path}`, { ...rest, headers, body });
  if (!res.ok) {
    let detail: string | undefined;
    let errBody: unknown;
    try {
      const data = await res.json();
      errBody = data;
      detail = typeof data?.detail === "string" ? data.detail : JSON.stringify(data?.detail);
    } catch {
      /* ignore */
    }
    throw new ApiError(res.status, detail || `${res.status} ${res.statusText}`, detail, errBody);
  }
  if (res.status === 204) return undefined as T;
  const ct = res.headers.get("content-type") ?? "";
  // An /api call returning HTML means the route was missing and the SPA catch-all
  // served index.html. Never hand that back as data (callers do data.x → crash).
  if (ct.includes("text/html")) {
    throw new ApiError(res.status, "API route not found (received HTML, not JSON)");
  }
  if (!ct.includes("application/json")) return (await res.text()) as unknown as T;
  return res.json() as Promise<T>;
}

/** Trigger a browser download for a Blob via a temporary object URL. */
export function saveBlob(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

export const api = {
  // ── Catalog ──────────────────────────────────────────────
  titles: () => req<Title[]>("/catalog/titles"),
  title: (id: number) => req<Title>(`/catalog/titles/${id}`),
  episodes: (id: number) => req<Episode[]>(`/catalog/titles/${id}/episodes`),
  scan: () => req<Record<string, unknown>>("/catalog/scan", { method: "POST" }),
  setWatched: (episodeId: number, watched: boolean) =>
    req<{ episode_id: number; watched: boolean }>(
      `/catalog/episodes/${episodeId}/watched`,
      { method: "POST", json: { watched } },
    ),
  watchedUpTo: (anilistId: number, epNumber: number) =>
    req<{ anilist_id: number; marked: number }>(
      `/catalog/titles/${anilistId}/watched-up-to`,
      { method: "POST", json: { ep_number: epNumber } },
    ),
  deleteTitle: (anilistId: number) =>
    req<{ ok: boolean; title: string; episodes: number; lines: number }>(
      `/catalog/titles/${anilistId}`,
      { method: "DELETE" },
    ),
  continueWatching: () => req<ContinueEntry[]>("/catalog/continue"),

  // ── Watch / Play ─────────────────────────────────────────
  // deviceId targets a specific Connector device; omitted = server default
  // routing (single connected device → it).
  play: (episodeId: number, seekMs?: number, deviceId?: string) =>
    req<Record<string, unknown>>("/watch/play", {
      method: "POST",
      json: { episode_id: episodeId, seek_ms: seekMs, device_id: deviceId },
    }),
  playMoment: (lineId: number, deviceId?: string) =>
    req<Record<string, unknown>>("/watch/play-moment", {
      method: "POST",
      json: { line_id: lineId, device_id: deviceId },
    }),
  connectorStatus: () => req<ConnectorStatus>("/connector/status"),
  connectorSetup: () => req<ConnectorSetup>("/connector/setup"),
  connectorSelfcheck: (deviceId?: string) =>
    req<SelfcheckResult>(
      `/connector/selfcheck${deviceId ? `?device_id=${encodeURIComponent(deviceId)}` : ""}`,
      { method: "POST" },
    ),
  browserPlay: (episodeId: number) =>
    req<BrowserPlayInfo>(`/watch/browser-play/${episodeId}`),
  watchProgress: (episodeId: number, positionMs: number, durationMs: number) =>
    req<Record<string, unknown>>("/watch/progress", {
      method: "POST",
      json: { episode_id: episodeId, position_ms: positionMs, duration_ms: durationMs },
    }),

  // ── Learn ────────────────────────────────────────────────
  comprehension: (episodeId: number) =>
    req<ComprehensionResult>(`/learn/comprehension/${episodeId}`),
  moments: (opts: {
    q: string;
    limit?: number;
    offset?: number;
    anilistId?: number | null;
    sort?: MomentSort;
  }) => {
    const p = new URLSearchParams({ q: opts.q });
    if (opts.limit != null) p.set("limit", String(opts.limit));
    if (opts.offset) p.set("offset", String(opts.offset));
    if (opts.anilistId != null) p.set("anilist_id", String(opts.anilistId));
    if (opts.sort) p.set("sort", opts.sort);
    return req<Moment[]>(`/learn/moments?${p.toString()}`);
  },
  translateLine: (lineId: number) =>
    req<TranslateResult>(`/learn/moments/${lineId}/translate`, { method: "POST" }),
  clip: (lineId: number) =>
    req<ClipResult>(`/learn/clip/${lineId}`, { method: "POST" }),
  newWords: (episodeId: number) =>
    req<NewWord[]>(`/learn/episode/${episodeId}/new-words`),
  transcript: (episodeId: number) =>
    req<TranscriptResponse>(`/learn/episode/${episodeId}/transcript`),
  leverage: (refresh = false, top?: number) =>
    req<LeverageResponse>(
      `/learn/leverage?refresh=${refresh}${top != null ? `&top=${top}` : ""}`,
    ),
  stats: (days = 90) => req<StatsResponse>(`/learn/stats?days=${days}`),
  migakuHealth: () => req<MigakuDriftReport>("/learn/health/migaku"),

  // ── Known words ──────────────────────────────────────────
  knownSummary: () => req<KnownWordsSummary>("/known/summary"),
  knownSync: (deviceId?: string) =>
    req<KnownWordsSummary>(
      `/known/sync${deviceId ? `?device_id=${encodeURIComponent(deviceId)}` : ""}`,
      { method: "POST" },
    ),
  knownGrowth: (days = 7) => req<KnownGrowth>(`/known/growth?days=${days}`),

  // ── Sweet spot (what to watch next at my level) ──────────
  sweetSpot: (lo = 80, hi = 95, limit = 60, includeWatched = false) =>
    req<SweetSpotItem[]>(
      `/learn/sweet-spot?lo=${lo}&hi=${hi}&limit=${limit}&include_watched=${includeWatched}`,
    ),

  // ── Acquire ──────────────────────────────────────────────
  acquireSearch: (q: string, trusted = true) =>
    req<NyaaResult[]>(
      `/acquire/search?q=${encodeURIComponent(q)}&trusted=${trusted}`,
    ),
  acquireDownload: (payload: Partial<NyaaResult>) =>
    req<Download>("/acquire/download", { method: "POST", json: payload }),
  episodeReleases: (episodeId: number, llm = true) =>
    req<EpisodeReleases>(`/acquire/episode/${episodeId}/releases?llm=${llm}`),
  downloadEpisode: (episodeId: number, release?: ReleaseOption) =>
    req<{ ok: boolean; release?: string; state?: string; reason?: string }>(
      `/acquire/episode/${episodeId}`,
      { method: "POST", json: release ? { release } : {} },
    ),
  titleBatches: (anilistId: number, llm = true) =>
    req<SeasonReleases>(`/acquire/title/${anilistId}/batches?llm=${llm}`),
  downloadBatch: (anilistId: number, release: ReleaseOption, episodes?: number[]) =>
    req<{ ok: boolean; release?: string; state?: string; kind?: string; total_files?: number; reason?: string }>(
      `/acquire/title/${anilistId}/batch`,
      { method: "POST", json: episodes ? { release, episodes } : { release } },
    ),
  downloads: () => req<Download[]>("/acquire/downloads"),
  cancelDownload: (id: number) =>
    req<{ ok: boolean; state?: string }>(`/acquire/downloads/${id}`, { method: "DELETE" }),
  retryDownload: (id: number) =>
    req<{ ok: boolean; action?: string }>(`/acquire/downloads/${id}/retry`, {
      method: "POST",
    }),
  follows: () => req<RssFollow[]>("/acquire/follows"),
  addFollow: (payload: Record<string, unknown>) =>
    req<RssFollow>("/acquire/follows", { method: "POST", json: payload }),
  followTitle: (anilistId: number) =>
    req<RssFollow>(`/acquire/title/${anilistId}/follow`, { method: "POST" }),
  deleteFollow: (id: number) =>
    req<{ ok: boolean }>(`/acquire/follows/${id}`, { method: "DELETE" }),
  qbt: () => req<QbtStatus>("/acquire/qbt"),

  // ── Match queue ──────────────────────────────────────────
  queue: () => req<MatchQueueItem[]>("/match/queue"),
  confirm: (queueId: number, anilistId: number) =>
    req<Record<string, unknown>>(`/match/queue/${queueId}/confirm`, {
      method: "POST",
      json: { anilist_id: anilistId },
    }),
  matchSearch: (q: string, epNumber?: number | null) =>
    req<MatchCandidate[]>(
      `/match/search?q=${encodeURIComponent(q)}${epNumber != null ? `&ep_number=${epNumber}` : ""}`,
    ),
  suggestMatch: (queueId: number) =>
    req<MatchSuggestion>(`/match/queue/${queueId}/suggest`, { method: "POST" }),
  deleteQueueItem: (queueId: number) =>
    req<{ ok: boolean }>(`/match/queue/${queueId}`, { method: "DELETE" }),

  // ── Subs ─────────────────────────────────────────────────
  fetchSubs: (episodeId: number) =>
    req<Record<string, unknown>>(`/subs/fetch/${episodeId}`, { method: "POST" }),
  englishConfig: () => req<EnglishConfig>("/subs/english/config"),
  setEnglishSource: (source: string) =>
    req<EnglishConfig>("/subs/english/config", { method: "PUT", json: { source } }),
  fetchEnglish: (episodeId: number) =>
    req<Record<string, unknown>>(`/subs/english/${episodeId}`, { method: "POST" }),

  // ── Analyze (comprehension without download) ─────────────
  analyzeTitle: (anilistId: number, maxEps?: number) =>
    req<{ queued?: number; episodes_targeted?: number }>(
      `/analyze/title/${anilistId}${maxEps ? `?max_eps=${maxEps}` : ""}`,
      { method: "POST" },
    ),
  analyzeLibrary: (status?: string) =>
    req<{ titles: number; queued: number }>(
      `/analyze/library${status ? `?status=${encodeURIComponent(status)}` : ""}`,
      { method: "POST" },
    ),

  // ── MAL ──────────────────────────────────────────────────
  malStatus: () => req<MalStatus>("/mal/status"),
  malSync: () => req<MalStatus>("/mal/sync", { method: "POST" }),

  // ── System health + job queue ────────────────────────────
  health: () => req<HealthReport>("/health?full=1"),
  jobs: (state?: string, type?: string, limit = 100) => {
    const p = new URLSearchParams({ limit: String(limit) });
    if (state) p.set("state", state);
    if (type) p.set("type", type);
    return req<JobRow[]>(`/jobs?${p.toString()}`);
  },
  jobStats: () => req<JobStats>("/jobs/stats"),
  retryJob: (id: number) =>
    req<Record<string, unknown>>(`/jobs/${id}/retry`, { method: "POST" }),
  retryErrorJobs: (type?: string) =>
    req<Record<string, unknown>>(
      `/jobs/retry-errors${type ? `?type=${encodeURIComponent(type)}` : ""}`,
      { method: "POST" },
    ),
  cancelJob: (id: number) =>
    req<Record<string, unknown>>(`/jobs/${id}/cancel`, { method: "POST" }),

  // ── Notifications / events ───────────────────────────────
  events: (unreadOnly = false, limit = 30) =>
    req<AppEvent[]>(`/events?unread=${unreadOnly}&limit=${limit}`),
  eventsUnreadCount: () => req<{ count: number }>("/events/unread-count"),
  eventsMarkRead: (arg?: number[] | { all: true }) => {
    const json = Array.isArray(arg) ? { ids: arg } : arg ?? { all: true };
    return req<{ updated: number }>("/events/read", { method: "POST", json });
  },
};

export const MAL_AUTH_URL = "/api/mal/auth";
