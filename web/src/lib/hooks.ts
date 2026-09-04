import { useEffect, useSyncExternalStore } from "react";
import {
  keepPreviousData,
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import type { InfiniteData } from "@tanstack/react-query";
import { api, ApiError } from "./api";
import { toast } from "@/components/ui/toast";
import type { Moment, MomentSort, ReleaseOption } from "./types";

export const qk = {
  titles: ["titles"] as const,
  title: (id: number) => ["title", id] as const,
  episodes: (id: number) => ["episodes", id] as const,
  comprehension: (id: number) => ["comprehension", id] as const,
  newWords: (id: number) => ["new-words", id] as const,
  // offset is the infinite-query page param, so it never appears in the key
  moments: (q: string, sort: MomentSort, anilistId: number | null) =>
    ["moments", q, sort, anilistId] as const,
  knownSummary: ["known-summary"] as const,
  knownGrowth: (days: number) => ["known-growth", days] as const,
  sweetSpot: (lo: number, hi: number, limit: number, includeWatched: boolean) =>
    ["sweet-spot", lo, hi, limit, includeWatched] as const,
  downloads: ["downloads"] as const,
  follows: ["follows"] as const,
  qbt: ["qbt"] as const,
  queue: ["queue"] as const,
  connectorStatus: ["connector-status"] as const,
  connectorSetup: ["connector-setup"] as const,
  malStatus: ["mal-status"] as const,
  events: (unreadOnly: boolean, limit: number) => ["events", unreadOnly, limit] as const,
  unreadCount: ["events-unread-count"] as const,
  continue: ["continue"] as const,
  leverage: ["leverage"] as const,
  transcript: (id: number) => ["transcript", id] as const,
  stats: (days: number) => ["stats", days] as const,
  health: ["health"] as const,
  jobs: (state: string, type: string) => ["jobs", state, type] as const,
  jobStats: ["job-stats"] as const,
};

// ── Play target device (multi-device Connector) ────────────
// Which Connector device plays/syncs should go to. "" = Auto: the server
// picks (single connected device → it; else Migaku-ready, most recently
// used). Persisted per browser in localStorage; the picker lives in the
// sidebar Connector chip and only shows when >1 device is connected.
const TARGET_DEVICE_KEY = "mimi-lab.play-device";
const targetDeviceListeners = new Set<() => void>();

export function getTargetDevice(): string {
  try {
    return localStorage.getItem(TARGET_DEVICE_KEY) ?? "";
  } catch {
    return "";
  }
}

export function setTargetDevice(deviceId: string) {
  try {
    if (deviceId) localStorage.setItem(TARGET_DEVICE_KEY, deviceId);
    else localStorage.removeItem(TARGET_DEVICE_KEY);
  } catch {
    /* ignore (private mode) */
  }
  targetDeviceListeners.forEach((l) => l());
}

function subscribeTargetDevice(cb: () => void) {
  targetDeviceListeners.add(cb);
  return () => {
    targetDeviceListeners.delete(cb);
  };
}

export const useTargetDevice = () =>
  useSyncExternalStore(subscribeTargetDevice, getTargetDevice, () => "");

/** The device id to send with a connector command: explicit > picker > Auto. */
function resolveTargetDevice(explicit?: string): string | undefined {
  return explicit ?? (getTargetDevice() || undefined);
}

// ── Catalog ────────────────────────────────────────────────
export const useTitles = () =>
  useQuery({ queryKey: qk.titles, queryFn: api.titles });

export const useTitle = (id: number) =>
  useQuery({ queryKey: qk.title(id), queryFn: () => api.title(id), enabled: !!id });

export const useEpisodes = (id: number, refetchActive = false) =>
  useQuery({
    queryKey: qk.episodes(id),
    queryFn: () => api.episodes(id),
    enabled: !!id,
    // poll while a download/import is in flight so episodes flip to "Play" live
    refetchInterval: refetchActive ? 4000 : false,
  });

export function useScan() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: api.scan,
    onMutate: () => toast.loading("Scanning library…", "Reading files from disk"),
    onSuccess: (data, _v, tid) => {
      const added = (data as { added?: number })?.added;
      toast.update(tid, {
        title: "Scan complete",
        description:
          typeof added === "number" ? `${added} new file(s) indexed` : "Library refreshed",
        variant: "success",
      });
      qc.invalidateQueries({ queryKey: qk.titles });
      qc.invalidateQueries({ queryKey: qk.queue });
    },
    onError: (e, _v, tid) =>
      toast.update(tid, {
        title: "Scan failed",
        description: errMsg(e),
        variant: "error",
      }),
  });
}

export function useSetWatched(anilistId?: number) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ episodeId, watched }: { episodeId: number; watched: boolean }) =>
      api.setWatched(episodeId, watched),
    onSuccess: (_d, { watched }) => {
      toast.success(watched ? "Marked watched" : "Marked unwatched");
      if (anilistId) qc.invalidateQueries({ queryKey: qk.episodes(anilistId) });
      qc.invalidateQueries({ queryKey: qk.titles });
    },
    onError: (e) => toast.error("Couldn't update", errMsg(e)),
  });
}

export function useWatchedUpTo(anilistId: number) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (epNumber: number) => api.watchedUpTo(anilistId, epNumber),
    onSuccess: (d) => {
      toast.success(
        "Marked watched",
        `${d.marked} episode${d.marked === 1 ? "" : "s"} up to here`,
      );
      qc.invalidateQueries({ queryKey: qk.episodes(anilistId) });
      qc.invalidateQueries({ queryKey: qk.titles });
      qc.invalidateQueries({ queryKey: qk.continue });
    },
    onError: (e) => toast.error("Couldn't update", errMsg(e)),
  });
}

export function useDeleteTitle(anilistId: number) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => api.deleteTitle(anilistId),
    onSuccess: (d) => {
      toast.success(
        "Removed from library",
        `${d.title}: ${d.episodes} episodes + ${d.lines.toLocaleString()} corpus lines deleted`,
      );
      qc.invalidateQueries({ queryKey: qk.titles });
      qc.invalidateQueries({ queryKey: qk.continue });
    },
    onError: (e) => toast.error("Couldn't remove title", errMsg(e)),
  });
}

export const useContinueWatching = () =>
  useQuery({
    queryKey: qk.continue,
    queryFn: api.continueWatching,
    retry: false,
  });

// ── Play ───────────────────────────────────────────────────
/** The device a play landed on, for toasts (for example, "Device A"). */
function playedOn(d: unknown): string | null {
  const name = (d as { device_name?: unknown } | null)?.device_name;
  return typeof name === "string" && name ? name : null;
}

export function usePlay() {
  return useMutation({
    mutationFn: ({
      episodeId,
      seekMs,
      deviceId,
    }: {
      episodeId: number;
      seekMs?: number;
      deviceId?: string;
    }) => api.play(episodeId, seekMs, resolveTargetDevice(deviceId)),
    onMutate: () => toast.loading("Launching Migaku…", "Loading into the Player"),
    onSuccess: (d, _v, tid) =>
      toast.update(tid, {
        title: playedOn(d) ? `Playing in Migaku on ${playedOn(d)}` : "Playing in Migaku",
        description: "Subtitles auto-paired · immersion features live",
        variant: "success",
      }),
    onError: (e, _v, tid) =>
      toast.update(tid, {
        title: "Couldn't start playback",
        description: connectorErr(e),
        variant: "error",
      }),
  });
}

export function usePlayMoment() {
  return useMutation({
    mutationFn: (lineId: number) => api.playMoment(lineId, resolveTargetDevice()),
    onMutate: () => toast.loading("Jumping in Migaku…"),
    onSuccess: (d, _v, tid) =>
      toast.update(tid, {
        title: playedOn(d) ? `Playing from this line on ${playedOn(d)}` : "Playing from this line",
        variant: "success",
      }),
    onError: (e, _v, tid) =>
      toast.update(tid, { title: "Couldn't start playback", description: connectorErr(e), variant: "error" }),
  });
}

export const useConnectorStatus = () =>
  useQuery({
    queryKey: qk.connectorStatus,
    queryFn: api.connectorStatus,
    refetchInterval: 15000,
    retry: false,
  });

export const useConnectorSetup = () =>
  useQuery({
    queryKey: qk.connectorSetup,
    queryFn: api.connectorSetup,
    retry: false,
  });

// ── Learn ──────────────────────────────────────────────────
export const useComprehension = (id: number, enabled: boolean) =>
  useQuery({
    queryKey: qk.comprehension(id),
    queryFn: () => api.comprehension(id),
    enabled: enabled && !!id,
    retry: false,
  });

export const MOMENTS_PAGE = 30;

/** Paged moments search — "Load more" appends the next offset page. */
export function useMoments(q: string, sort: MomentSort, anilistId: number | null) {
  return useInfiniteQuery({
    queryKey: qk.moments(q, sort, anilistId),
    queryFn: ({ pageParam }) =>
      api.moments({ q, limit: MOMENTS_PAGE, offset: pageParam, sort, anilistId }),
    initialPageParam: 0,
    getNextPageParam: (lastPage, allPages) =>
      lastPage.length < MOMENTS_PAGE
        ? undefined
        : allPages.reduce((n, p) => n + p.length, 0),
    enabled: q.trim().length > 0,
    placeholderData: keepPreviousData,
  });
}

/** On-demand single-line MT; patches the translated line into every cached moments page. */
export function useTranslateLine() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (lineId: number) => api.translateLine(lineId),
    onSuccess: (res) => {
      if (!res.translation) {
        toast.error("No translation available", "The translator returned nothing for this line.");
        return;
      }
      qc.setQueriesData<InfiniteData<Moment[]>>({ queryKey: ["moments"] }, (old) => {
        if (!old?.pages) return old;
        return {
          ...old,
          pages: old.pages.map((page) =>
            page.map((m) =>
              m.line_id === res.line_id ? { ...m, translation: res.translation } : m,
            ),
          ),
        };
      });
    },
    onError: (e) => toast.error("Translation failed", errMsg(e)),
  });
}

export function useClip() {
  return useMutation({
    mutationFn: (lineId: number) => api.clip(lineId),
    onMutate: () => toast.loading("Extracting clip…", "Grabbing screenshot + audio"),
    onSuccess: (_d, _v, tid) =>
      toast.update(tid, { title: "Clip ready", variant: "success" }),
    onError: (e, _v, tid) =>
      toast.update(tid, { title: "Clip failed", description: errMsg(e), variant: "error" }),
  });
}

export function useFetchSubs(anilistId?: number) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (episodeId: number) => api.fetchSubs(episodeId),
    onMutate: () => toast.loading("Fetching subtitles…", "jimaku → align → ingest"),
    onSuccess: (_d, _v, tid) => {
      toast.update(tid, { title: "Subtitles ready", variant: "success" });
      if (anilistId) qc.invalidateQueries({ queryKey: qk.episodes(anilistId) });
    },
    onError: (e, _v, tid) =>
      toast.update(tid, { title: "No subtitles found", description: errMsg(e), variant: "error" }),
  });
}

// ── English secondary subtitles ────────────────────────────
export const useEnglishConfig = () =>
  useQuery({ queryKey: ["english-config"], queryFn: api.englishConfig, retry: false });

export function useSetEnglishSource() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (source: string) => api.setEnglishSource(source),
    onSuccess: (data) => {
      toast.success(
        "English source updated",
        data.source === "llm" ? "Machine translation (Claude)" : "Human subtitles",
      );
      qc.invalidateQueries({ queryKey: ["english-config"] });
    },
    onError: (e) => toast.error("Couldn't update English source", errMsg(e)),
  });
}

export function useAnalyzeTitle(anilistId: number) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (maxEps?: number) => api.analyzeTitle(anilistId, maxEps),
    onMutate: () => toast.loading("Analyzing…", "Fetching subtitles + scoring comprehension"),
    onSuccess: (data, _v, tid) => {
      const q = data?.queued ?? 0;
      toast.update(tid, {
        title: q > 0 ? "Analysis started" : "Already analyzed",
        description:
          q > 0
            ? `${q} episode(s) queued — comprehension appears shortly`
            : "Comprehension is up to date",
        variant: "success",
      });
      qc.invalidateQueries({ queryKey: qk.episodes(anilistId) });
      qc.invalidateQueries({ queryKey: qk.title(anilistId) });
    },
    onError: (e, _v, tid) =>
      toast.update(tid, { title: "Analysis failed", description: errMsg(e), variant: "error" }),
  });
}

export function useAnalyzeLibrary() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (status?: string) => api.analyzeLibrary(status),
    onMutate: () =>
      toast.loading("Analyzing your library…", "Fetching subtitles + scoring comprehension"),
    onSuccess: (data, _v, tid) => {
      toast.update(tid, {
        title: data.titles > 0 ? "Analysis started" : "All caught up",
        description:
          data.titles > 0
            ? `${data.titles} title(s) queued — comprehension fills in over the next few minutes`
            : "Every title already has comprehension",
        variant: "success",
      });
      qc.invalidateQueries({ queryKey: qk.titles });
    },
    onError: (e, _v, tid) =>
      toast.update(tid, { title: "Analysis failed", description: errMsg(e), variant: "error" }),
  });
}

export function useEpisodeReleases(episodeId: number, enabled: boolean, llm = true) {
  return useQuery({
    queryKey: ["episode-releases", episodeId, llm],
    queryFn: () => api.episodeReleases(episodeId, llm),
    enabled: enabled && !!episodeId,
    staleTime: 60_000,
    retry: false,
  });
}

export function useDownloadEpisode(anilistId?: number) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ episodeId, release }: { episodeId: number; release?: ReleaseOption }) =>
      api.downloadEpisode(episodeId, release),
    onMutate: () =>
      toast.loading("Queuing download…", "Sending to Transmission"),
    onSuccess: (data, _v, tid) => {
      toast.update(tid, {
        title: "Download started",
        description: data.release ? data.release.slice(0, 60) : "Queued to Transmission",
        variant: "success",
      });
      if (anilistId) qc.invalidateQueries({ queryKey: qk.episodes(anilistId) });
      qc.invalidateQueries({ queryKey: qk.downloads });
    },
    onError: (e, _v, tid) =>
      toast.update(tid, { title: "No release found", description: errMsg(e), variant: "error" }),
  });
}

export function useSeasonReleases(anilistId: number, enabled: boolean, llm = true) {
  return useQuery({
    queryKey: ["season-releases", anilistId, llm],
    queryFn: () => api.titleBatches(anilistId, llm),
    enabled: enabled && !!anilistId,
    staleTime: 60_000,
    retry: false,
  });
}

export function useDownloadBatch(anilistId: number) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ release, episodes }: { release: ReleaseOption; episodes?: number[] }) =>
      api.downloadBatch(anilistId, release, episodes),
    onMutate: () => toast.loading("Queuing season pack…", "Sending the selected episodes to Transmission"),
    onSuccess: (data, _v, tid) => {
      toast.update(tid, {
        title: "Season download started",
        description: data.release ? data.release.slice(0, 60) : "Queued to Transmission",
        variant: "success",
      });
      qc.invalidateQueries({ queryKey: qk.episodes(anilistId) });
      qc.invalidateQueries({ queryKey: qk.downloads });
    },
    onError: (e, _v, tid) =>
      toast.update(tid, {
        title: "Couldn't start season download",
        description: errMsg(e),
        variant: "error",
      }),
  });
}

export function useCancelDownload(anilistId?: number) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (downloadId: number) => api.cancelDownload(downloadId),
    onSuccess: () => {
      toast.success("Download cancelled", "Removed from Transmission");
      qc.invalidateQueries({ queryKey: qk.downloads });
      if (anilistId) qc.invalidateQueries({ queryKey: qk.episodes(anilistId) });
    },
    onError: (e) => toast.error("Couldn't cancel", errMsg(e)),
  });
}

export function useRetryDownload(anilistId?: number) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (downloadId: number) => api.retryDownload(downloadId),
    onSuccess: (d) => {
      toast.success("Retrying download", d.action ?? "Re-queued");
      qc.invalidateQueries({ queryKey: qk.downloads });
      if (anilistId) qc.invalidateQueries({ queryKey: qk.episodes(anilistId) });
    },
    onError: (e) => toast.error("Retry failed", errMsg(e)),
  });
}

// ── Known words ────────────────────────────────────────────
export const useKnownSummary = () =>
  useQuery({ queryKey: qk.knownSummary, queryFn: api.knownSummary, retry: false });

export const useKnownGrowth = (days = 7) =>
  useQuery({
    queryKey: qk.knownGrowth(days),
    queryFn: () => api.knownGrowth(days),
    retry: false,
  });

// ── Sweet spot (what to watch next at my level) ────────────
export const useSweetSpot = (lo = 80, hi = 95, limit = 60, includeWatched = false) =>
  useQuery({
    queryKey: qk.sweetSpot(lo, hi, limit, includeWatched),
    queryFn: () => api.sweetSpot(lo, hi, limit, includeWatched),
  });

// ── One-click Follow for a title (RSS auto-download) ───────
export function useFollowTitle() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (anilistId: number) => api.followTitle(anilistId),
    onMutate: () => toast.loading("Following…", "Creating an RSS auto-download rule"),
    onSuccess: (_d, _v, tid) => {
      toast.update(tid, {
        title: "Following",
        description: "New episodes will auto-download to your library",
        variant: "success",
      });
      qc.invalidateQueries({ queryKey: qk.follows });
    },
    onError: (e, _v, tid) =>
      toast.update(tid, { title: "Couldn't follow", description: errMsg(e), variant: "error" }),
  });
}

export function useKnownSync() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => api.knownSync(resolveTargetDevice()),
    onMutate: () => toast.loading("Syncing from Migaku…", "Reading the WordList"),
    onSuccess: (data, _v, tid) => {
      toast.update(tid, {
        title: "Known words synced",
        description: `${data.known.toLocaleString()} known · ${data.learning.toLocaleString()} learning`,
        variant: "success",
      });
      qc.invalidateQueries({ queryKey: qk.knownSummary });
      qc.invalidateQueries({ queryKey: ["known-growth"] });
    },
    onError: (e, _v, tid) =>
      toast.update(tid, {
        title: "Sync failed",
        description: connectorErr(e),
        variant: "error",
      }),
  });
}

// ── Acquire ────────────────────────────────────────────────
export function useAcquireSearch(q: string, trusted: boolean, enabled: boolean) {
  return useQuery({
    queryKey: ["acquire-search", q, trusted],
    queryFn: () => api.acquireSearch(q, trusted),
    enabled: enabled && q.trim().length > 0,
    placeholderData: keepPreviousData,
    retry: false,
  });
}

export const useDownloads = () =>
  useQuery({
    queryKey: qk.downloads,
    queryFn: api.downloads,
    refetchInterval: 4000,
  });

export function useAddDownload() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: api.acquireDownload,
    onSuccess: (d) => {
      toast.success("Download added", d.title_guess ?? "Queued to Transmission");
      qc.invalidateQueries({ queryKey: qk.downloads });
    },
    onError: (e) => toast.error("Download failed", errMsg(e)),
  });
}

export const useFollows = () =>
  useQuery({ queryKey: qk.follows, queryFn: api.follows });

export function useAddFollow() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: api.addFollow,
    onSuccess: () => {
      toast.success("Follow added");
      qc.invalidateQueries({ queryKey: qk.follows });
    },
    onError: (e) => toast.error("Couldn't add follow", errMsg(e)),
  });
}

export function useDeleteFollow() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: api.deleteFollow,
    onSuccess: () => {
      toast.success("Follow removed");
      qc.invalidateQueries({ queryKey: qk.follows });
    },
    onError: (e) => toast.error("Couldn't remove follow", errMsg(e)),
  });
}

export const useQbt = () =>
  useQuery({ queryKey: qk.qbt, queryFn: api.qbt, refetchInterval: 15000, retry: false });

// ── Match queue ────────────────────────────────────────────
export const useQueue = () =>
  useQuery({ queryKey: qk.queue, queryFn: api.queue });

export function useConfirmMatch() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ queueId, anilistId }: { queueId: number; anilistId: number }) =>
      api.confirm(queueId, anilistId),
    onSuccess: () => {
      toast.success("Match confirmed", "Episode linked to its title");
      qc.invalidateQueries({ queryKey: qk.queue });
      qc.invalidateQueries({ queryKey: qk.titles });
    },
    onError: (e) => toast.error("Couldn't confirm", errMsg(e)),
  });
}

export function useDeleteQueueItem() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (queueId: number) => api.deleteQueueItem(queueId),
    onSuccess: () => {
      toast.success("Removed from queue");
      qc.invalidateQueries({ queryKey: qk.queue });
    },
    onError: (e) => toast.error("Couldn't remove", errMsg(e)),
  });
}

export function useMatchSearch() {
  return useMutation({
    mutationFn: ({ q, epNumber }: { q: string; epNumber?: number | null }) =>
      api.matchSearch(q, epNumber),
    onError: (e) => toast.error("Search failed", errMsg(e)),
  });
}

export function useSuggestMatch() {
  return useMutation({
    mutationFn: (queueId: number) => api.suggestMatch(queueId),
    onError: (e) => toast.error("Suggestion failed", errMsg(e)),
  });
}

// ── MAL (stub-tolerant) ────────────────────────────────────
export const useMalStatus = () =>
  useQuery({ queryKey: qk.malStatus, queryFn: api.malStatus, retry: false });

export function useMalSync() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: api.malSync,
    onMutate: () => toast.loading("Syncing MyAnimeList…"),
    onSuccess: (_d, _v, tid) => {
      toast.update(tid, { title: "MAL synced", variant: "success" });
      qc.invalidateQueries({ queryKey: qk.titles });
      qc.invalidateQueries({ queryKey: qk.malStatus });
    },
    onError: (e, _v, tid) =>
      toast.update(tid, {
        title: "MAL sync unavailable",
        description: errMsg(e),
        variant: "error",
      }),
  });
}

// ── Learn: leverage / transcript / stats ───────────────────
export const useLeverage = () =>
  useQuery({
    queryKey: qk.leverage,
    queryFn: () => api.leverage(false),
    staleTime: 5 * 60_000,
    retry: false,
  });

export function useRefreshLeverage() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => api.leverage(true),
    onMutate: () => toast.loading("Recomputing leverage…", "Scanning the 60–80% band"),
    onSuccess: (data, _v, tid) => {
      qc.setQueryData(qk.leverage, data);
      toast.update(tid, {
        title: "Study queue refreshed",
        description:
          data.computed_in_s != null ? `Computed in ${data.computed_in_s.toFixed(1)}s` : undefined,
        variant: "success",
      });
    },
    onError: (e, _v, tid) =>
      toast.update(tid, { title: "Refresh failed", description: errMsg(e), variant: "error" }),
  });
}

export const useTranscript = (episodeId: number) =>
  useQuery({
    queryKey: qk.transcript(episodeId),
    queryFn: () => api.transcript(episodeId),
    enabled: !!episodeId,
    retry: false,
  });

export const useStats = (days = 90) =>
  useQuery({ queryKey: qk.stats(days), queryFn: () => api.stats(days), retry: false });

// ── System health + job queue ──────────────────────────────
export const useHealth = () =>
  useQuery({
    queryKey: qk.health,
    queryFn: api.health,
    refetchInterval: 15000,
    retry: false,
  });

export const useJobs = (state: string, type: string, limit = 100) =>
  useQuery({
    queryKey: qk.jobs(state, type),
    queryFn: () => api.jobs(state || undefined, type || undefined, limit),
    refetchInterval: 8000,
    placeholderData: keepPreviousData,
    retry: false,
  });

export const useJobStats = () =>
  useQuery({
    queryKey: qk.jobStats,
    queryFn: api.jobStats,
    refetchInterval: 8000,
    retry: false,
  });

export function useRetryJob() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => api.retryJob(id),
    onSuccess: () => {
      toast.success("Job re-queued");
      qc.invalidateQueries({ queryKey: ["jobs"] });
      qc.invalidateQueries({ queryKey: qk.jobStats });
    },
    onError: (e) => toast.error("Retry failed", errMsg(e)),
  });
}

export function useRetryErrorJobs() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (type?: string) => api.retryErrorJobs(type),
    onSuccess: () => {
      toast.success("Errored jobs re-queued");
      qc.invalidateQueries({ queryKey: ["jobs"] });
      qc.invalidateQueries({ queryKey: qk.jobStats });
    },
    onError: (e) => toast.error("Retry failed", errMsg(e)),
  });
}

export function useCancelJob() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => api.cancelJob(id),
    onSuccess: () => {
      toast.success("Job cancelled");
      qc.invalidateQueries({ queryKey: ["jobs"] });
      qc.invalidateQueries({ queryKey: qk.jobStats });
    },
    onError: (e) => toast.error("Cancel failed", errMsg(e)),
  });
}

// ── Migaku integration health (Settings) ───────────────────
/** Button-triggered GET of the tokenizer drift self-test. */
export function useMigakuDriftCheck() {
  return useMutation({
    mutationFn: () => api.migakuHealth(),
    onError: (e) => toast.error("Drift check failed", errMsg(e)),
  });
}

/** Full end-to-end Connector/Migaku smoke test — can take up to ~90s. */
export function useConnectorSelfcheck() {
  return useMutation({
    mutationFn: () => api.connectorSelfcheck(resolveTargetDevice()),
    onMutate: () =>
      toast.loading("Running full self-check…", "Plays a probe clip through Migaku — up to 90s"),
    onSuccess: (data, _v, tid) =>
      toast.update(tid, {
        title: data.ok ? "Self-check passed" : "Self-check found problems",
        description: data.ok ? "Play, tokenize, and panel scrape all work" : data.error ?? undefined,
        variant: data.ok ? "success" : "error",
      }),
    onError: (e, _v, tid) =>
      toast.update(tid, {
        title: "Self-check failed",
        description: connectorErr(e),
        variant: "error",
      }),
  });
}

// ── Notifications / events ─────────────────────────────────
export const useEvents = (unreadOnly = false, limit = 30) =>
  useQuery({
    queryKey: qk.events(unreadOnly, limit),
    queryFn: () => api.events(unreadOnly, limit),
    refetchInterval: 15000,
    retry: false,
  });

export const useUnreadCount = () =>
  useQuery({
    queryKey: qk.unreadCount,
    queryFn: api.eventsUnreadCount,
    refetchInterval: 15000,
    retry: false,
    select: (d) => d.count,
  });

export function useMarkEventsRead() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (arg?: number[] | { all: true }) => api.eventsMarkRead(arg),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: qk.unreadCount });
      qc.invalidateQueries({ queryKey: ["events"] });
    },
    onError: (e) => toast.error("Couldn't update notifications", errMsg(e)),
  });
}

// ── live updates (Server-Sent Events) ─────────────────────
// One EventSource to /api/events/stream. Each server nudge invalidates the
// affected queries so screens update the instant a background job finishes —
// most importantly comprehension pills, which used to fill in silently and stay
// stale until a manual refresh (refetchOnWindowFocus is off). EventSource
// auto-reconnects, so a dropped stream self-heals. Mounted once (in <App/>).
export function useServerEvents() {
  const qc = useQueryClient();
  useEffect(() => {
    let es: EventSource | null = null;
    const inval = (keys: readonly (readonly unknown[])[]) =>
      keys.forEach((queryKey) => qc.invalidateQueries({ queryKey }));

    // "Loading into Migaku…" progress toast — updated in place per push and
    // auto-dismissed 3s after the last update (or success-finished at 100%).
    let progressToastId: number | null = null;
    let progressTimer: ReturnType<typeof setTimeout> | undefined;
    const finishProgress = (title: string) => {
      if (progressToastId == null) return;
      toast.update(progressToastId, { title, variant: "success", duration: 2500 });
      progressToastId = null;
      clearTimeout(progressTimer);
    };

    try {
      es = new EventSource("/api/events/stream");
    } catch {
      return; // SSE unsupported — the existing polling remains the backstop
    }

    es.onmessage = (ev) => {
      let msg: {
        type?: string;
        job?: string;
        category?: string;
        ok?: boolean;
        episode_id?: number;
        pct?: number;
        phase?: string;
      } = {};
      try {
        msg = JSON.parse(ev.data);
      } catch {
        return;
      }
      // always keep the notification bell fresh
      inval([qk.unreadCount, ["events"]]);

      if (msg.type === "play-progress") {
        const pct = Math.max(0, Math.min(100, Math.round(msg.pct ?? 0)));
        const title =
          msg.phase === "loading"
            ? `Preparing episode… ${pct}%`
            : `Loading into Migaku… ${pct}%`;
        if (progressToastId == null) progressToastId = toast.loading(title);
        else toast.update(progressToastId, { title, variant: "loading" });
        clearTimeout(progressTimer);
        if (pct >= 100) {
          finishProgress("Loaded into Migaku");
        } else {
          progressTimer = setTimeout(() => finishProgress("Loaded into Migaku"), 3000);
        }
        return;
      }

      if (msg.type === "watch") {
        inval([qk.continue, ["episodes"]]);
        return;
      }

      const job = msg.job;
      const cat = msg.category;

      // SRS. `["srs"]` is a PREFIX match, so it must exclude the review queue
      // (imperative: nothing mounts it, and a refetch would inject/reorder
      // cards under the session reducer) and, while a stack move is in flight,
      // the card lists (an invalidation mid-drag snaps the row back under the
      // cursor). ~1,000 `srs_clip` completions land after the initial import.
      if (
        msg.type === "srs" ||
        cat === "srs" ||
        (typeof job === "string" && job.startsWith("srs_"))
      ) {
        const moving = qc.isMutating({ mutationKey: ["srs", "move"] }) > 0;
        qc.invalidateQueries({
          queryKey: ["srs"],
          predicate: (q) =>
            q.queryKey[1] !== "queue" && !(moving && q.queryKey[1] === "cards"),
        });
        inval([qk.leverage]);
        return;
      }

      if (job === "comprehension" || cat === "comprehension") {
        inval([qk.titles, ["episodes"], ["comprehension"]]);
      }
      if (
        job === "subtitle_fetch" ||
        job === "title_subtitle_fetch" ||
        job === "subtitle_align" ||
        job === "english_subtitle_fetch" ||
        cat === "subtitle"
      ) {
        inval([qk.titles, ["episodes"]]);
      }
      if (job === "postprocess" || cat === "download" || cat === "match") {
        inval([qk.titles, qk.downloads, qk.queue, ["episodes"]]);
      }
      if (job === "mal_sync" || cat === "mal") {
        inval([qk.titles, qk.malStatus]);
      }
    };
    es.onerror = () => {
      /* browser auto-reconnects; nothing to do */
    };
    return () => {
      es?.close();
      clearTimeout(progressTimer);
      if (progressToastId != null) toast.dismiss(progressToastId);
    };
  }, [qc]);
}

// ── document.title per page ────────────────────────────────
export function usePageTitle(title: string) {
  useEffect(() => {
    document.title = `${title} · Mimi Lab`;
    return () => {
      document.title = "Mimi Lab";
    };
  }, [title]);
}

// ── helpers ────────────────────────────────────────────────
function errMsg(e: unknown): string {
  if (e instanceof ApiError) return e.detail || e.message;
  if (e instanceof Error) return e.message;
  return "Unexpected error";
}

function connectorErr(e: unknown): string {
  const m = errMsg(e);
  // 409 is the server's "no Connector connected" status for play/sync.
  if (
    (e instanceof ApiError && e.status === 409) ||
    m.toLowerCase().includes("connector") ||
    m.includes("connect")
  ) {
    return "Connector offline — start it on the machine where you watch (Chrome + Migaku).";
  }
  return m;
}
