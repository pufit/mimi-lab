// The moment is more than the target cue (design §8.3.3, evidence amendment).
//
// `card.extend` holds every extra dialogue line the moment includes, each with
// a `role`:
//
//   * `evidence`      — `why_clear` leans on it, so the learner has to *see* it:
//                       Japanese only, dimmed, before/after the target line.
//   * `continuation`  — the sentence spills into the next cue; it only widens
//                       the clip window, it is not part of the front.
//
// Rows written before the evidence pass carry neither `role` nor `idx`; back
// then `extend` only ever held the continuation cue, so a missing role reads as
// `continuation` and those cards render exactly as they did yesterday.

import type { SrsCard, SrsLineRef, SrsLineRole } from "@/lib/srs-types";

/** The card fields the window/evidence helpers need — keeps callers loose. */
export type ExtendCard = Pick<SrsCard, "start_ms" | "end_ms" | "extend" | "clip">;

export function lineRole(line: SrsLineRef): SrsLineRole {
  return line.role === "evidence" ? "evidence" : "continuation";
}

function byTime(a: SrsLineRef, b: SrsLineRef): number {
  return a.start_ms - b.start_ms || (a.idx ?? 0) - (b.idx ?? 0);
}

export interface EvidenceSplit {
  /** Evidence lines spoken before the target, chronological. */
  before: SrsLineRef[];
  /** Evidence lines spoken after the target, chronological. */
  after: SrsLineRef[];
  /** `before.length + after.length` — cheap "does this card need the block?". */
  count: number;
}

const EMPTY: EvidenceSplit = { before: [], after: [], count: 0 };

/**
 * Evidence lines split around the target sentence.
 *
 * `idx` decides the side when the card and the line both carry one (a re-timed
 * cue can otherwise sort wrong); `start_ms` is the fallback for older rows.
 */
export function splitEvidence(
  card: Pick<SrsCard, "start_ms" | "extend"> & { context?: SrsCard["context"] },
): EvidenceSplit {
  const evidence = card.extend.filter((l) => lineRole(l) === "evidence");
  if (evidence.length === 0) return EMPTY;

  const targetIdx = card.context?.find((c) => c.is_target)?.idx;
  const isBefore = (l: SrsLineRef) =>
    targetIdx != null && l.idx != null ? l.idx < targetIdx : l.start_ms < card.start_ms;

  const before = evidence.filter(isBefore).sort(byTime);
  const after = evidence.filter((l) => !isBefore(l)).sort(byTime);
  return { before, after, count: before.length + after.length };
}

export interface MomentWindow {
  start_ms: number;
  end_ms: number;
  /**
   * The window came from the cut clip, so it already carries the lead/tail pad
   * — a player must not add its own on top.
   */
  padded: boolean;
}

/**
 * The span the moment actually covers.
 *
 * Prefers the window the clip was cut with (`clip.start_ms/end_ms`, which the
 * backend clamps and caps); falls back to the union of the target line and
 * every `extend` line, which is what that window is derived from.
 */
export function momentWindow(card: ExtendCard): MomentWindow {
  const clipStart = card.clip.start_ms;
  const clipEnd = card.clip.end_ms;
  if (clipStart != null && clipEnd != null && clipEnd > clipStart) {
    return { start_ms: clipStart, end_ms: clipEnd, padded: true };
  }
  let start = card.start_ms;
  let end = card.end_ms;
  for (const l of card.extend) {
    if (l.start_ms < start) start = l.start_ms;
    if (l.end_ms > end) end = l.end_ms;
  }
  return { start_ms: start, end_ms: end, padded: false };
}

/** Length of the moment in ms, or `null` when the window is degenerate. */
export function momentDurationMs(card: ExtendCard): number | null {
  const w = momentWindow(card);
  const ms = w.end_ms - w.start_ms;
  return ms > 0 ? ms : null;
}

/** `4.2s` / `1:04` — a duration, not a timestamp (`formatMs` is the latter). */
export function formatDuration(ms: number): string {
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`;
  const total = Math.round(ms / 1000);
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`;
}
