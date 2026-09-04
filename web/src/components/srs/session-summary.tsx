// End of the session (design §8.3.8) — and the empty state when there was
// nothing to answer in the first place.

import { CheckCircle2, Layers, Loader2, Plus } from "lucide-react";
import { StackedBar } from "@/components/charts";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/empty-state";
import { Kbd } from "@/components/ui/kbd";
import { Progress } from "@/components/ui/progress";
import { cn, formatMs } from "@/lib/utils";
import type { SrsRating } from "@/lib/srs-types";

export interface SessionStats {
  reviewed: number;
  again: number;
  newLearned: number;
  timeMs: number;
  becameKnown: number;
  ratings: Record<SrsRating, number>;
}

export const EMPTY_STATS: SessionStats = {
  reviewed: 0,
  again: 0,
  newLearned: 0,
  timeMs: 0,
  becameKnown: 0,
  ratings: { 1: 0, 2: 0, 3: 0, 4: 0 },
};

/** "in 6 h" for a future ISO timestamp; `null` when there is nothing due. */
export function dueIn(iso: string | null | undefined): string | null {
  if (!iso) return null;
  const ts = Date.parse(iso);
  if (Number.isNaN(ts)) return null;
  const min = Math.round((ts - Date.now()) / 60000);
  if (min <= 0) return "now";
  if (min < 60) return `in ${min} min`;
  const hours = Math.round(min / 60);
  if (hours < 24) return `in ${hours} h`;
  const days = Math.round(hours / 24);
  return `in ${days} d`;
}

function Stat({ value, label }: { value: string; label: string }) {
  return (
    <div className="rounded-xl border border-border bg-surface/40 px-3 py-2.5 text-center">
      <div className="text-xl font-semibold tabular-nums text-fg">{value}</div>
      <div className="mt-0.5 text-[0.7rem] text-muted">{label}</div>
    </div>
  );
}

export interface SessionSummaryProps {
  stats: SessionStats;
  streakDays: number | null;
  nextDueAt: string | null;
  /** The stack still has `new` cards → "+5 more new" is worth offering. */
  canAddNew: boolean;
  loadingMore: boolean;
  onAddNew: () => void;
  onBackToDeck: () => void;
  onManageStack: () => void;
  className?: string;
}

export function SessionSummary({
  stats,
  streakDays,
  nextDueAt,
  canAddNew,
  loadingMore,
  onAddNew,
  onBackToDeck,
  onManageStack,
  className,
}: SessionSummaryProps) {
  const againPct = stats.reviewed > 0 ? Math.round((stats.again / stats.reviewed) * 100) : 0;
  const next = dueIn(nextDueAt);

  const actions = (
    <div className="flex flex-wrap items-center justify-center gap-2">
      {canAddNew && (
        <Button variant="secondary" onClick={onAddNew} disabled={loadingMore}>
          {loadingMore ? <Loader2 className="size-4 animate-spin" /> : <Plus className="size-4" />}
          +5 more new
        </Button>
      )}
      <Button variant="primary" onClick={onBackToDeck}>
        Back to deck
        <Kbd className="border-transparent bg-black/25 text-current">Enter</Kbd>
      </Button>
      <Button variant="ghost" onClick={onManageStack}>
        <Layers className="size-4" />
        Manage stack
      </Button>
    </div>
  );

  if (stats.reviewed === 0) {
    return (
      <div className={cn("mx-auto w-full max-w-2xl px-4 py-10", className)}>
        <EmptyState
          icon={CheckCircle2}
          title="All caught up"
          description={
            next
              ? `Nothing is due right now — the next card comes back ${next}.`
              : "Nothing is due right now. Add new cards from the stack whenever you want more."
          }
          action={actions}
        />
      </div>
    );
  }

  return (
    <div className={cn("mx-auto w-full max-w-2xl space-y-5 px-4 py-8 animate-fade-in", className)}>
      <div className="text-center">
        <h1 className="text-2xl font-semibold tracking-tight text-fg">Session done</h1>
        <p className="mt-1 text-sm text-muted">
          {next ? `Next card ${next}.` : "Nothing else is due today."}
        </p>
      </div>

      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        <Stat value={String(stats.reviewed)} label="reviewed" />
        <Stat value={`${againPct}%`} label="again" />
        <Stat value={String(stats.newLearned)} label="new learned" />
        <Stat value={formatMs(stats.timeMs)} label="time" />
      </div>

      <div className="rounded-xl border border-border bg-surface/30 p-3">
        <div className="mb-2 flex items-center justify-between text-xs text-muted">
          <span>Again rate</span>
          <span className="tabular-nums">
            {stats.again} of {stats.reviewed}
          </span>
        </div>
        <Progress
          value={againPct}
          max={100}
          accent={againPct > 30 ? "var(--color-comp-amber)" : "var(--color-comp-green)"}
        />
      </div>

      <div className="rounded-xl border border-border bg-surface/30 p-3">
        <p className="mb-1.5 text-xs text-muted">This session&rsquo;s ratings</p>
        {/* The four-bar chart only labelled its first and last column, so the two
            tallest bars were unidentifiable. This is the stats page's "How it
            went" widget — every category is named and carries its count. */}
        <StackedBar
          segments={[
            { label: "Again", value: stats.ratings[1], color: "var(--color-comp-red)" },
            { label: "Hard", value: stats.ratings[2], color: "var(--color-comp-amber)" },
            { label: "Good", value: stats.ratings[3], color: "var(--color-comp-green)" },
            { label: "Easy", value: stats.ratings[4], color: "var(--color-brand-bright)" },
          ]}
        />
      </div>

      <div className="flex flex-wrap items-center justify-center gap-3 text-xs text-muted">
        {streakDays != null && (
          <span>
            <span className="font-semibold text-fg tabular-nums">{streakDays}</span> day streak
          </span>
        )}
        {stats.becameKnown > 0 && (
          <span className="text-comp-green">
            ✓ {stats.becameKnown} now count{stats.becameKnown === 1 ? "s" : ""} as known
          </span>
        )}
      </div>

      {actions}
    </div>
  );
}
