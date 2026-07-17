// Shared download-state sets — the single source of truth for what a
// Transmission/import state means. title.tsx and acquire.tsx previously
// hand-maintained diverging copies of these.

/** Failure states — the download needs user attention (all retryable). */
export const FAILED_STATES = new Set(["error", "pp_failed", "lost"]);

/** The torrent data is fully fetched (postprocessing may still follow). */
export const DONE_STATES = new Set([
  "completed",
  "done",
  "seeding",
  "uploading",
  "postprocessed",
]);

/** No cancel makes sense anymore — done, failed, or already cancelled. */
export const TERMINAL_STATES = new Set([
  ...DONE_STATES,
  ...FAILED_STATES,
  "cancelled",
]);

/**
 * Nothing is in flight anymore. Unlike TERMINAL_STATES this excludes
 * 'completed', which for season packs means "torrent done, import running" —
 * pages that poll while work is happening should keep polling through it.
 */
export const SETTLED_STATES = new Set(
  [...TERMINAL_STATES].filter((s) => s !== "completed"),
);

export const isFailedState = (s?: string | null): boolean =>
  !!s && FAILED_STATES.has(s.toLowerCase());

/** Human label for a failure state. */
export function failedStateLabel(s: string): string {
  const v = s.toLowerCase();
  if (v === "pp_failed") return "Import failed";
  if (v === "lost") return "Lost";
  return "Error";
}
