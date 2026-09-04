// React Query hooks for the SRS surfaces (design §8.2).
//
// Two rules this file exists to enforce:
//  1. The review queue is *imperative*. It is never mounted as a `useQuery`, so
//     no SSE invalidation, focus refetch or reconnect can inject or reorder
//     cards under the session reducer. Fetch it with `fetchSrsQueue(qc, …)`.
//  2. Every other SRS query hangs under the `["srs", <surface>, …]` prefix so
//     the single SSE branch in `hooks.ts` can invalidate the feature with one
//     call while excluding the queue (and the card lists during a drag).

import { useSyncExternalStore } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { QueryClient } from "@tanstack/react-query";
import { ApiError } from "./api";
import { srsApi } from "./srs-api";
import type { SrsCandidatesParams, SrsCardsParams, SrsWordOp } from "./srs-api";
import { qkSrs, qkSrsCardsAll } from "./srs-keys";
import { toast } from "@/components/ui/toast";
import type {
  SrsAction,
  SrsCard,
  SrsCardList,
  SrsCreateCard,
  SrsCreateCardConflict,
  SrsGenerateRequest,
  SrsPatchCard,
  SrsPrefs,
  SrsQueue,
  SrsResortBy,
  SrsReviewConflict,
  SrsReviewRequest,
  SrsReviewResult,
  SrsSettingsPatch,
  SrsSwapMoment,
  SrsUndoRequest,
} from "./srs-types";

/** `errMsg` is module-private in `hooks.ts`; re-implemented here (§8.2). */
export function errMsg(e: unknown): string {
  if (e instanceof ApiError) return e.detail || e.message;
  if (e instanceof Error) return e.message;
  return "Unexpected error";
}

/**
 * Invalidate the whole SRS feature except the imperative queue. Every mutation
 * below funnels through this so no hook can accidentally refetch the queue.
 */
function invalidateSrs(qc: QueryClient) {
  qc.invalidateQueries({
    queryKey: qkSrs.all,
    predicate: (q) => q.queryKey[1] !== "queue",
  });
}

// ── Summary (Deck header + nav badge) ──────────────────────

export const useSrsSummary = () =>
  useQuery({
    queryKey: qkSrs.summary,
    queryFn: srsApi.summary,
    refetchInterval: 60_000,
  });

// ── Queue: imperative only ─────────────────────────────────

/**
 * The one way to get review cards. `staleTime: 0` + `gcTime: 0` means the
 * result is never cached and never re-served: the session reducer owns the
 * cards from the moment they land.
 */
export function fetchSrsQueue(
  qc: QueryClient,
  limit = 20,
  extraNew = 0,
): Promise<SrsQueue> {
  return qc.fetchQuery({
    queryKey: qkSrs.queue,
    queryFn: () => srsApi.queue(limit, extraNew),
    staleTime: 0,
    gcTime: 0,
  });
}

// ── Review ─────────────────────────────────────────────────

export interface UseReviewCardOptions {
  /** 409: the card is no longer active — drop it from the session, never re-insert. */
  onConflict?: (conflict: SrsReviewConflict | undefined, vars: SrsReviewRequest) => void;
  /**
   * Network / 5xx: the caller re-inserts the card at the current index. When it
   * is supplied the generic "Rating failed" toast is suppressed — the caller owns
   * the failure toast (§8.3.5 wants a *Retry* action on it, replaying `vars`).
   */
  onFailure?: (error: unknown, vars: SrsReviewRequest) => void;
  onReviewed?: (result: SrsReviewResult, vars: SrsReviewRequest) => void;
}

/**
 * Rating mutation. `client_id` is part of the **variables** (the reducer mints
 * it when the rating is dispatched), so a manual retry replays the exact same
 * variables object and the server dedupes instead of double-rating.
 */
export function useReviewCard(opts: UseReviewCardOptions = {}) {
  const qc = useQueryClient();
  return useMutation({
    mutationKey: ["srs", "review"],
    mutationFn: (vars: SrsReviewRequest) => srsApi.review(vars),
    onSuccess: (result, vars) => {
      opts.onReviewed?.(result, vars);
      qc.invalidateQueries({ queryKey: qkSrs.summary });
    },
    onError: (e, vars) => {
      if (e instanceof ApiError && e.status === 409) {
        toast.warning("Card was retired meanwhile", errMsg(e));
        qc.invalidateQueries({ queryKey: qkSrs.summary });
        opts.onConflict?.(e.body as SrsReviewConflict | undefined, vars);
        return;
      }
      if (opts.onFailure) opts.onFailure(e, vars);
      else toast.error("Rating failed", errMsg(e));
    },
  });
}

export function useUndoReview() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: SrsUndoRequest = {}) => srsApi.undo(body),
    onSuccess: (res) => {
      toast.success("Undone", res.card.lemma);
      invalidateSrs(qc);
    },
    onError: (e) => toast.error("Couldn't undo", errMsg(e)),
  });
}

// ── Card lists & detail ────────────────────────────────────

/**
 * `limit`/`offset` are appended to the §8.2 key so paged tabs don't overwrite
 * each other; the documented `qkSrs.cards(...)` prefix still matches them all.
 */
export function useSrsCards(params: SrsCardsParams = {}) {
  const {
    state = "all",
    q = "",
    sort = "queue",
    order = "asc",
    limit = 50,
    offset = 0,
  } = params;
  return useQuery({
    queryKey: [...qkSrs.cards(state, q, sort, order), limit, offset],
    queryFn: () => srsApi.cards({ state, q, sort, order, limit, offset }),
  });
}

export const useSrsCard = (id: number) =>
  useQuery({
    queryKey: qkSrs.card(id),
    queryFn: () => srsApi.card(id),
    enabled: !!id,
  });

export function useCreateCard() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: SrsCreateCard) => srsApi.createCard(body),
    onSuccess: (res, vars) => {
      if (res.queued) toast.success("Looking for a moment…", vars.lemma);
      else
        toast.success(
          "Added to Study",
          res.warning || res.card?.meaning_short || vars.lemma,
        );
      invalidateSrs(qc);
    },
    onError: (e) => {
      const conflict = e instanceof ApiError ? (e.body as SrsCreateCardConflict) : undefined;
      if (conflict?.card_id) toast.warning("Already in your deck", errMsg(e));
      else toast.error("Couldn't add the card", errMsg(e));
    },
  });
}

export function usePatchCard() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, patch }: { id: number; patch: SrsPatchCard }) =>
      srsApi.patchCard(id, patch),
    onSuccess: (res) => {
      toast.success("Saved", res.card.lemma);
      qc.invalidateQueries({ queryKey: qkSrs.card(res.card.id) });
      qc.invalidateQueries({ queryKey: qkSrsCardsAll });
    },
    onError: (e) => toast.error("Couldn't save", errMsg(e)),
  });
}

export function useDeleteCard() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => srsApi.deleteCard(id),
    onSuccess: () => {
      toast.success("Card deleted");
      invalidateSrs(qc);
    },
    onError: (e) => toast.error("Couldn't delete the card", errMsg(e)),
  });
}

export function useCardAction() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, action }: { id: number; action: SrsAction }) =>
      srsApi.action(id, action),
    onSuccess: (res) => {
      qc.setQueryData(qkSrs.card(res.card.id), (old: { card: SrsCard } | undefined) =>
        old ? { ...old, card: res.card } : old,
      );
      invalidateSrs(qc);
    },
    onError: (e) => toast.error("Couldn't apply that", errMsg(e)),
  });
}

export function useBulkAction() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ ids, action }: { ids: number[]; action: SrsAction }) =>
      srsApi.bulk(ids, action),
    onSuccess: () => invalidateSrs(qc),
    onError: (e) => toast.error("Bulk action failed", errMsg(e)),
  });
}

// ── Stack ──────────────────────────────────────────────────

function moveInList(
  list: SrsCardList | undefined,
  cardId: number,
  position: number,
): SrsCardList | undefined {
  if (!list?.items) return list;
  const from = list.items.findIndex((c) => c.id === cardId);
  if (from < 0) return list;
  const items = list.items.slice();
  const [row] = items.splice(from, 1);
  items.splice(Math.max(0, Math.min(items.length, position)), 0, row);
  let pos = 0;
  return {
    ...list,
    items: items.map((c) => (c.state === "new" ? { ...c, queue_pos: ++pos } : c)),
  };
}

/**
 * The only reorder primitive (§4.2): one card, absolute 0-based position.
 * Optimistic — `mutationKey: ["srs","move"]` is what the SSE branch checks so a
 * clip finishing mid-drag can't snap the dragged row back under the cursor.
 */
export function useMoveCard() {
  const qc = useQueryClient();
  return useMutation({
    mutationKey: ["srs", "move"],
    mutationFn: ({ card_id, position }: { card_id: number; position: number }) =>
      srsApi.move(card_id, position),
    onMutate: async ({ card_id, position }) => {
      await qc.cancelQueries({ queryKey: qkSrsCardsAll });
      const snapshots = qc.getQueriesData<SrsCardList>({ queryKey: qkSrsCardsAll });
      qc.setQueriesData<SrsCardList>({ queryKey: qkSrsCardsAll }, (old) =>
        moveInList(old, card_id, position),
      );
      return { snapshots };
    },
    onError: (e, _vars, ctx) => {
      for (const [key, data] of ctx?.snapshots ?? []) qc.setQueryData(key, data);
      toast.error("Couldn't move that card", errMsg(e));
    },
    onSettled: () => invalidateSrs(qc),
  });
}

export function useResortStack() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ by, keep_top }: { by: SrsResortBy; keep_top?: number }) =>
      srsApi.resort(by, keep_top ?? 0),
    onMutate: () => toast.loading("Re-sorting the stack…"),
    onSuccess: (res, _v, tid) => {
      toast.update(tid, {
        title: "Stack re-sorted",
        description: `${res.updated} card(s) reordered`,
        variant: "success",
      });
      invalidateSrs(qc);
    },
    onError: (e, _v, tid) =>
      toast.update(tid, { title: "Re-sort failed", description: errMsg(e), variant: "error" }),
  });
}

// ── Moment / clip operations ───────────────────────────────

export function useSwapMoment() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, body }: { id: number; body: SrsSwapMoment }) =>
      srsApi.swapMoment(id, body),
    onSuccess: (res) => {
      toast.success("Moment swapped", "The new clip is being cut");
      qc.invalidateQueries({ queryKey: qkSrs.card(res.card.id) });
      qc.invalidateQueries({ queryKey: qkSrsCardsAll });
    },
    onError: (e) => toast.error("Couldn't swap the moment", errMsg(e)),
  });
}

export function useRegenClip() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => srsApi.regenClip(id),
    onSuccess: (res) => {
      toast.success("Cutting a fresh clip…");
      qc.invalidateQueries({ queryKey: qkSrs.card(res.card.id) });
    },
    onError: (e) => toast.error("Couldn't regenerate the clip", errMsg(e)),
  });
}

export function useFindMoments() {
  return useMutation({
    mutationFn: (id: number) => srsApi.findMoments(id),
    onSuccess: () => toast.success("Looking for more moments…"),
    onError: (e) => toast.error("Couldn't start the search", errMsg(e)),
  });
}

// ── Generation & candidates ────────────────────────────────

export function useGenerateCards() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: SrsGenerateRequest = {}) => srsApi.generate(body),
    onMutate: () => toast.loading("Generating cards…", "Scoring words and judging moments"),
    onSuccess: (res, _v, tid) => {
      toast.update(tid, {
        title: res.run_id ? "Generation started" : "Nothing to generate",
        description: res.run_id ? `Run #${res.run_id}` : "The stack is already full",
        variant: res.run_id ? "success" : "default",
      });
      invalidateSrs(qc);
    },
    onError: (e, _v, tid) =>
      toast.update(tid, {
        title: "Couldn't generate",
        description: errMsg(e),
        variant: "error",
      }),
  });
}

export const useGeneration = (limit = 10) =>
  useQuery({ queryKey: qkSrs.generation, queryFn: () => srsApi.generation(limit) });

export function useCandidates(params: SrsCandidatesParams = {}) {
  const {
    status = "unjudged",
    q = "",
    sort = "score",
    limit = 50,
    offset = 0,
    refresh = false,
  } = params;
  return useQuery({
    queryKey: [...qkSrs.candidates(status, q, sort), limit, offset],
    queryFn: () => srsApi.candidates({ status, q, sort, limit, offset, refresh }),
  });
}

export function useWordAction() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ lemma, op }: { lemma: string; op: SrsWordOp }) =>
      srsApi.wordAction(lemma, op),
    onSuccess: (_res, { lemma, op }) => {
      const label =
        op === "skip"
          ? "Skipped"
          : op === "unskip"
            ? "Back in the pool"
            : op === "confirm-known"
              ? "Marked known"
              : "Judging…";
      toast.success(label, lemma);
      invalidateSrs(qc);
    },
    onError: (e) => toast.error("Couldn't update that word", errMsg(e)),
  });
}

// ── Import / settings / stats ──────────────────────────────

export function useImportCards() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (path?: string) => srsApi.importCards(path),
    onMutate: () => toast.loading("Importing the deck…"),
    onSuccess: (res, _v, tid) => {
      toast.update(tid, {
        title: "Deck imported",
        description: `${res.created} created · ${res.skipped_existing} already there · ${res.clip_jobs_queued} clip job(s) queued${
          res.errors.length ? ` · ${res.errors.length} error(s)` : ""
        }`,
        variant: res.errors.length ? "warning" : "success",
      });
      invalidateSrs(qc);
    },
    onError: (e, _v, tid) =>
      toast.update(tid, { title: "Import failed", description: errMsg(e), variant: "error" }),
  });
}

export const useSrsSettings = () =>
  useQuery({ queryKey: qkSrs.settings, queryFn: srsApi.settings });

export function useSaveSrsSettings() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (patch: SrsSettingsPatch) => srsApi.saveSettings(patch),
    onSuccess: (settings) => {
      qc.setQueryData(qkSrs.settings, settings);
      qc.invalidateQueries({ queryKey: qkSrs.summary });
    },
    onError: (e) => toast.error("Couldn't save the setting", errMsg(e)),
  });
}

export const useSrsStats = (days = 90) =>
  useQuery({ queryKey: qkSrs.stats(days), queryFn: () => srsApi.stats(days) });

// ── Presentation prefs (localStorage, never the server) ────

const PREFS_KEY = "srs.prefs";
const DEFAULT_PREFS: SrsPrefs = { ratingMode: "four", frontFurigana: "none" };
let prefsCache: SrsPrefs | null = null;
const prefsListeners = new Set<() => void>();

export function getSrsPrefs(): SrsPrefs {
  if (prefsCache) return prefsCache;
  let raw: Partial<SrsPrefs> = {};
  try {
    const stored = localStorage.getItem(PREFS_KEY);
    if (stored) raw = JSON.parse(stored) as Partial<SrsPrefs>;
  } catch {
    /* ignore (private mode / corrupt value) */
  }
  prefsCache = {
    ratingMode: raw.ratingMode === "two" ? "two" : "four",
    frontFurigana:
      raw.frontFurigana === "target" || raw.frontFurigana === "all" ? raw.frontFurigana : "none",
  };
  return prefsCache;
}

export function setSrsPrefs(patch: Partial<SrsPrefs>) {
  prefsCache = { ...getSrsPrefs(), ...patch };
  try {
    localStorage.setItem(PREFS_KEY, JSON.stringify(prefsCache));
  } catch {
    /* ignore */
  }
  prefsListeners.forEach((l) => l());
}

function subscribePrefs(cb: () => void) {
  prefsListeners.add(cb);
  return () => {
    prefsListeners.delete(cb);
  };
}

/** `const [prefs, setPrefs] = useSrsPrefs();` — toggles live in the `?` sheet. */
export function useSrsPrefs(): [SrsPrefs, (patch: Partial<SrsPrefs>) => void] {
  const prefs = useSyncExternalStore(subscribePrefs, getSrsPrefs, () => DEFAULT_PREFS);
  return [prefs, setSrsPrefs];
}

// ── Aliases: the short names used in the work-package briefs ───────────────

export {
  useResortStack as useResort,
  useGenerateCards as useGenerate,
  useSaveSrsSettings as useUpdateSrsSettings,
  useImportCards as useImportDeck,
};
