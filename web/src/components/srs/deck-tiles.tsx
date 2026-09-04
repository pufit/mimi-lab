import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import { Brain, CalendarCheck, Flame, Scissors, Sparkles, TrendingUp } from "lucide-react";
import { Progress } from "@/components/ui/progress";
import { cn } from "@/lib/utils";
import type { SrsSummary } from "@/lib/srs-types";

function Tile({
  icon: Icon,
  label,
  value,
  sub,
  accent,
  to,
  children,
}: {
  icon: typeof Brain;
  label: string;
  value: string;
  sub?: ReactNode;
  accent?: string;
  to?: string;
  children?: ReactNode;
}) {
  const body = (
    <div
      className={cn(
        "flex h-full flex-col rounded-xl border border-border bg-surface/60 p-4 transition-colors",
        to && "hover:border-border-strong hover:bg-surface-hover/60",
      )}
    >
      <div className="flex items-center gap-2">
        <Icon className="size-4" style={{ color: accent ?? "var(--color-brand-bright)" }} />
        <p className="text-[0.7rem] font-medium uppercase tracking-wide text-faint">{label}</p>
      </div>
      <p className="mt-2 text-3xl font-bold tabular-nums leading-none text-fg">{value}</p>
      {sub && <div className="mt-1.5 text-xs leading-snug text-muted">{sub}</div>}
      {children && <div className="mt-auto pt-3">{children}</div>}
    </div>
  );
  return to ? (
    <Link to={to} className="focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand/70 rounded-xl">
      {body}
    </Link>
  ) : (
    body
  );
}

/** The four numbers that decide whether you press Review (design §8.5). */
export function DeckTiles({ summary }: { summary: SrsSummary }) {
  const due = summary.due_learning + summary.due_review;
  const newLimit = Math.max(0, summary.new_today_limit);
  const newDone = summary.new_today_done;
  const clipsPending = summary.clips.pending;
  const delta = summary.known_week_delta;

  return (
    <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
      <Tile
        icon={CalendarCheck}
        label="Due now"
        value={String(due)}
        accent="var(--color-comp-green)"
        sub={
          due === 0 ? (
            summary.next_due_at ? (
              "Nothing due — come back later today"
            ) : (
              "Nothing due"
            )
          ) : (
            <>
              {summary.due_learning} learning · {summary.due_review} review
              {summary.review_cap_hit && (
                <span className="text-comp-amber"> · daily cap reached</span>
              )}
            </>
          )
        }
      />

      <Tile
        icon={Sparkles}
        label="New today"
        value={`${newDone} / ${newLimit}`}
        sub={
          <>
            {summary.new_available} ready in the stack
            {clipsPending > 0 && (
              <span className="mt-0.5 flex items-center gap-1 text-faint">
                <Scissors className="size-3" />
                {clipsPending} clip{clipsPending === 1 ? "" : "s"} still cutting
              </span>
            )}
          </>
        }
      >
        <Progress value={newLimit > 0 ? Math.min(newDone, newLimit) : 0} max={newLimit || 1} />
      </Tile>

      <Tile
        icon={Brain}
        label="Words you know"
        value={summary.known_total.toLocaleString()}
        accent="var(--color-comp-lime)"
        to="/study/stack"
        sub={
          delta > 0 ? (
            <span className="inline-flex items-center gap-1 text-comp-green">
              <TrendingUp className="size-3" />+{delta} this week
            </span>
          ) : (
            "No change this week"
          )
        }
      />

      <Tile
        icon={Flame}
        label="Streak"
        value={`${summary.streak_days} day${summary.streak_days === 1 ? "" : "s"}`}
        accent="var(--color-comp-amber)"
        sub={
          summary.reviewed_today > 0
            ? `${summary.reviewed_today} reviewed today · ${summary.again_today} again`
            : "Review one card to keep it alive"
        }
      />
    </div>
  );
}
