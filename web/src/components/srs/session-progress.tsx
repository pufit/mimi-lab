// The thin strip on top of the review session (design §8.3).
//
// Chips only — never "N / total". Learn-ahead re-entries and refills make a
// total grow while you work, which reads as punishment; the server's counts
// only ever decrease.

import { Keyboard, X } from "lucide-react";
import type { ReactNode } from "react";
import { Button } from "@/components/ui/button";
import { Kbd } from "@/components/ui/kbd";
import { cn } from "@/lib/utils";
import type { SrsQueueCounts } from "@/lib/srs-types";

export interface SessionProgressProps {
  counts: SrsQueueCounts | null;
  /**
   * Cards rated in *this session*, the same derived number the summary card
   * shows. The server's `reviewed_today` includes everything rated earlier in
   * the day and does not come back down after an Undo, so driving the chip from
   * it produced three different "reviewed" numbers on one screen.
   */
  reviewed: number;
  /** A card is on screen right now (→ never claim there is nothing to do). */
  serving?: boolean;
  /** Cards that crossed the 21-day known threshold in this session. */
  becameKnown: number;
  onExit: () => void;
  onShortcuts: () => void;
  /** Session kebab (K/S/B/E/M for touch). */
  menu?: ReactNode;
  className?: string;
}

function Chip({
  value,
  label,
  accent,
}: {
  value: number;
  label: string;
  accent?: string;
}) {
  return (
    <span className="inline-flex items-baseline gap-1 whitespace-nowrap">
      <span className={cn("text-sm font-semibold tabular-nums", accent ?? "text-fg")}>{value}</span>
      <span className="text-xs text-muted">{label}</span>
    </span>
  );
}

export function SessionProgress({
  counts,
  reviewed,
  serving = false,
  becameKnown,
  onExit,
  onShortcuts,
  menu,
  className,
}: SessionProgressProps) {
  const empty =
    !!counts && counts.learning_due === 0 && counts.review_due === 0 && counts.new_left_today === 0;

  return (
    <div
      className={cn(
        "flex items-center gap-3 border-b border-border/70 bg-bg/80 px-4 py-2.5 backdrop-blur-md",
        className,
      )}
    >
      <div className="flex min-w-0 flex-1 flex-wrap items-baseline gap-x-3 gap-y-1">
        {counts ? (
          <>
            {empty && serving ? (
              // Every bucket is empty but a card is on screen: the queue is
              // running ahead of the schedule. Saying "0 · 0 · 0" here tells the
              // user there is nothing to do while they are doing it.
              <span className="whitespace-nowrap text-xs font-medium text-brand-bright">
                learning ahead
              </span>
            ) : (
              <>
                <Chip value={counts.learning_due} label="learning" accent="text-brand-bright" />
                <span className="text-faint">·</span>
                <Chip value={counts.review_due} label="due" />
                <span className="text-faint">·</span>
                <Chip value={counts.new_left_today} label="new left" />
              </>
            )}
            <span className="text-faint">·</span>
            <Chip value={reviewed} label="reviewed" accent="text-muted" />
            {counts.clips_pending > 0 && (
              <>
                <span className="text-faint">·</span>
                <span className="whitespace-nowrap text-xs text-faint">
                  {counts.clips_pending} clip{counts.clips_pending === 1 ? "" : "s"} still cutting
                </span>
              </>
            )}
          </>
        ) : (
          <span className="text-xs text-faint">Loading the queue…</span>
        )}
        {becameKnown > 0 && (
          <span className="inline-flex items-center gap-1 rounded-md border border-comp-green/30 bg-comp-green/15 px-2 py-0.5 text-[0.7rem] font-medium leading-none text-comp-green">
            ✓ {becameKnown} counts as known
          </span>
        )}
      </div>

      {menu}
      <Button
        variant="ghost"
        size="icon-sm"
        onClick={onShortcuts}
        aria-label="Keyboard shortcuts"
        title="Keyboard shortcuts (?)"
      >
        <Keyboard className="size-4" />
      </Button>
      <Button variant="ghost" size="sm" onClick={onExit} aria-label="Leave the session">
        <X className="size-4" />
        <Kbd>Esc</Kbd>
      </Button>
    </div>
  );
}
