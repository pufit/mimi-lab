// Typed client for every §7.2 endpoint. Thin: no caching, no toasts — those
// live in `srs-hooks.ts`. Errors surface as `ApiError` with the parsed JSON
// body attached (`ApiError.body`), which is how the 409 conflict payloads
// (`SrsReviewConflict`, `SrsCreateCardConflict`) reach the caller.

import { req } from "./api";
import type {
  SrsAction,
  SrsBulkResult,
  SrsCandidates,
  SrsCard,
  SrsCardDetail,
  SrsCardEnvelope,
  SrsCardList,
  SrsClipQueued,
  SrsCreateCard,
  SrsCreateCardResult,
  SrsGenerateRequest,
  SrsGenerateResult,
  SrsGeneration,
  SrsImportReport,
  SrsPatchCard,
  SrsQueue,
  SrsQueued,
  SrsResortBy,
  SrsResortResult,
  SrsReviewRequest,
  SrsReviewResult,
  SrsSettings,
  SrsSettingsPatch,
  SrsStats,
  SrsSummary,
  SrsSwapMoment,
  SrsUndoRequest,
  SrsUndoResult,
  SrsWordActionResult,
} from "./srs-types";

export interface SrsCardsParams {
  /** all | new | studying | learning | review | relearning | known | suspended | rejected | parked */
  state?: string;
  q?: string;
  /** queue | due | created | lemma | score | rank */
  sort?: string;
  order?: "asc" | "desc";
  /** 0 = no limit (the Stack tab always loads the whole stack) */
  limit?: number;
  offset?: number;
}

export interface SrsCandidatesParams {
  /** unjudged | filtered | pending | accepted | rejected | probably_known | error | all */
  status?: string;
  q?: string;
  /** score | rank | occ */
  sort?: string;
  limit?: number;
  offset?: number;
  /** 1 enqueues `srs_census` before answering */
  refresh?: boolean;
}

/** `POST /srs/words/{lemma}/{op}` */
export type SrsWordOp = "skip" | "unskip" | "confirm-known" | "judge";

function qs(params: Record<string, string | number | boolean | undefined | null>): string {
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null) continue;
    sp.set(k, typeof v === "boolean" ? (v ? "1" : "0") : String(v));
  }
  const s = sp.toString();
  return s ? `?${s}` : "";
}

export const srsApi = {
  // ── Deck / queue ─────────────────────────────────────────
  summary: () => req<SrsSummary>("/srs/summary"),
  queue: (limit = 20, extraNew = 0) =>
    req<SrsQueue>(`/srs/queue${qs({ limit, extra_new: extraNew })}`),

  // ── Review ───────────────────────────────────────────────
  review: (body: SrsReviewRequest) =>
    req<SrsReviewResult>("/srs/review", { method: "POST", json: body }),
  undo: (body: SrsUndoRequest = {}) =>
    req<SrsUndoResult>("/srs/review/undo", { method: "POST", json: body }),

  // ── Cards ────────────────────────────────────────────────
  cards: (params: SrsCardsParams = {}) =>
    req<SrsCardList>(
      `/srs/cards${qs({
        state: params.state,
        q: params.q,
        sort: params.sort,
        order: params.order,
        limit: params.limit,
        offset: params.offset,
      })}`,
    ),
  card: (id: number) => req<SrsCardDetail>(`/srs/cards/${id}`),
  createCard: (body: SrsCreateCard) =>
    req<SrsCreateCardResult>("/srs/cards", { method: "POST", json: body }),
  patchCard: (id: number, body: SrsPatchCard) =>
    req<SrsCardEnvelope>(`/srs/cards/${id}`, { method: "PATCH", json: body }),
  deleteCard: (id: number) => req<void>(`/srs/cards/${id}`, { method: "DELETE" }),
  action: (id: number, action: SrsAction) =>
    req<SrsCardEnvelope>(`/srs/cards/${id}/action`, { method: "POST", json: { action } }),
  bulk: (ids: number[], action: SrsAction) =>
    req<SrsBulkResult>("/srs/cards/bulk", {
      method: "POST",
      json: { card_ids: ids, action },
    }),
  swapMoment: (id: number, body: SrsSwapMoment) =>
    req<SrsCardEnvelope>(`/srs/cards/${id}/moment`, { method: "POST", json: body }),
  regenClip: (id: number) =>
    req<SrsClipQueued>(`/srs/cards/${id}/clip`, { method: "POST" }),
  findMoments: (id: number) =>
    req<SrsQueued>(`/srs/cards/${id}/find-moments`, { method: "POST" }),

  // ── Stack ────────────────────────────────────────────────
  move: (cardId: number, position: number) =>
    req<SrsCardEnvelope>("/srs/stack/move", {
      method: "POST",
      json: { card_id: cardId, position },
    }),
  resort: (by: SrsResortBy, keepTop = 0) =>
    req<SrsResortResult>("/srs/stack/resort", {
      method: "POST",
      json: { by, keep_top: keepTop },
    }),

  // ── Generation ───────────────────────────────────────────
  generate: (body: SrsGenerateRequest = {}) =>
    req<SrsGenerateResult>("/srs/generate", { method: "POST", json: body }),
  generation: (limit = 10) => req<SrsGeneration>(`/srs/generation${qs({ limit })}`),
  candidates: (params: SrsCandidatesParams = {}) =>
    req<SrsCandidates>(
      `/srs/candidates${qs({
        status: params.status,
        q: params.q,
        sort: params.sort,
        limit: params.limit,
        offset: params.offset,
        refresh: params.refresh,
      })}`,
    ),
  wordAction: (lemma: string, op: SrsWordOp) =>
    req<SrsWordActionResult>(`/srs/words/${encodeURIComponent(lemma)}/${op}`, {
      method: "POST",
    }),

  // ── Import / settings / stats ────────────────────────────
  importCards: (path?: string) =>
    req<SrsImportReport>("/srs/import", { method: "POST", json: path ? { path } : {} }),
  settings: () => req<SrsSettings>("/srs/settings"),
  saveSettings: (patch: SrsSettingsPatch) =>
    req<SrsSettings>("/srs/settings", { method: "PUT", json: patch }),
  stats: (days = 90) => req<SrsStats>(`/srs/stats${qs({ days })}`),
};

/** Re-exported so consumers can write `SrsCard` without a second import line. */
export type { SrsCard };
