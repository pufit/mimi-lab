import { useMemo, useState } from "react";
import { BarChart3 } from "lucide-react";
import { PageHeader } from "@/components/layout/page-header";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { ErrorState } from "@/components/ui/error-state";
import { EmptyState } from "@/components/ui/empty-state";
import { BarChart, LineChart, StackedBar } from "@/components/charts";
import { StudyTabs } from "@/components/srs/study-tabs";
import { errMsg, useSrsStats } from "@/lib/srs-hooks";
import { cn } from "@/lib/utils";
import type { SrsState } from "@/lib/srs-types";

const RANGES = [30, 90, 365];

const STATE_LABEL: Record<SrsState, string> = {
  new: "In the stack",
  learning: "Learning",
  review: "In review",
  relearning: "Relearning",
  known: "Known",
  suspended: "Parked",
  rejected: "Turned down",
};

const STATE_COLOR: Record<SrsState, string> = {
  new: "var(--color-brand-dim)",
  learning: "var(--color-comp-amber)",
  review: "var(--color-brand-bright)",
  relearning: "var(--color-comp-red)",
  known: "var(--color-comp-green)",
  suspended: "var(--color-faint)",
  rejected: "var(--color-border-strong)",
};

function Tile({
  label,
  value,
  hint,
  tone,
}: {
  label: string;
  value: string;
  hint?: string;
  tone?: string;
}) {
  return (
    <Card>
      <CardContent className="p-4">
        <p className="text-[0.7rem] font-medium uppercase tracking-wide text-faint">{label}</p>
        <p className={cn("mt-1.5 text-2xl font-bold tabular-nums text-fg")} style={{ color: tone }}>
          {value}
        </p>
        {hint && <p className="mt-1 text-xs leading-snug text-muted">{hint}</p>}
      </CardContent>
    </Card>
  );
}

const pct = (v: number | null) => (v == null ? "—" : `${Math.round(v * 100)}%`);

export function StudyStatsPage() {
  const [days, setDays] = useState(90);
  const { data, isLoading, isError, error, refetch } = useSrsStats(days);

  const reviewPoints = useMemo(
    () =>
      (data?.days ?? []).map((d) => ({
        x: new Date(`${d.day}T00:00:00Z`).getTime(),
        y: d.reviews,
      })),
    [data],
  );

  const forecastBars = useMemo(
    () => (data?.forecast ?? []).map((f) => ({ label: f.day.slice(5), value: f.due })),
    [data],
  );

  const intervalBars = useMemo(
    () => (data?.intervals ?? []).map((i) => ({ label: i.bucket, value: i.count })),
    [data],
  );

  const ratingSegments = useMemo(() => {
    const acc = { again: 0, hard: 0, good: 0, easy: 0 };
    for (const d of data?.days ?? []) {
      acc.again += d.again;
      acc.hard += d.hard;
      acc.good += d.good;
      acc.easy += d.easy;
    }
    return [
      { label: "Again", value: acc.again, color: "var(--color-comp-red)" },
      { label: "Hard", value: acc.hard, color: "var(--color-comp-amber)" },
      { label: "Good", value: acc.good, color: "var(--color-comp-green)" },
      { label: "Easy", value: acc.easy, color: "var(--color-brand-bright)" },
    ];
  }, [data]);

  const stateSegments = useMemo(
    () =>
      (Object.keys(STATE_LABEL) as SrsState[])
        .map((s) => ({
          label: STATE_LABEL[s],
          value: data?.states?.[s] ?? 0,
          color: STATE_COLOR[s],
        }))
        .filter((s) => s.value > 0),
    [data],
  );

  const totalReviews = (data?.days ?? []).reduce((n, d) => n + d.reviews, 0);
  const demotions = (data?.days ?? []).reduce((n, d) => n + d.demotions, 0);

  return (
    <div className="animate-fade-in">
      <PageHeader
        title="Study stats"
        subtitle="How the deck is actually going."
        actions={
          <div className="flex items-center gap-1 rounded-lg border border-border bg-bg-elevated/60 p-1">
            {RANGES.map((r) => (
              <button
                key={r}
                type="button"
                onClick={() => setDays(r)}
                className={cn(
                  "rounded-md px-3 py-1 text-xs font-medium transition-colors",
                  days === r ? "bg-brand/20 text-brand-bright" : "text-muted hover:text-fg",
                )}
              >
                {r === 365 ? "1 y" : `${r} d`}
              </button>
            ))}
          </div>
        }
      />

      <StudyTabs />

      {isLoading && (
        <div className="space-y-4">
          <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
            {Array.from({ length: 4 }).map((_, i) => (
              <Skeleton key={i} className="h-24 w-full rounded-xl" />
            ))}
          </div>
          <Skeleton className="h-52 w-full rounded-xl" />
        </div>
      )}

      {isError && <ErrorState message={errMsg(error)} onRetry={() => refetch()} />}

      {data && totalReviews === 0 && (
        <EmptyState
          icon={BarChart3}
          title="No reviews yet"
          description="Numbers appear after your first session — retention needs a week or so before it means anything."
        />
      )}

      {data && totalReviews > 0 && (
        <div className="space-y-5">
          <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
            <Tile
              label="Retention (7 d)"
              value={pct(data.retention_7d)}
              hint="Share of due cards you got right this week"
              tone="var(--color-comp-green)"
            />
            <Tile
              label="Retention (30 d)"
              value={pct(data.retention_30d)}
              hint="The target is around 90%"
            />
            <Tile
              label="Seconds per card"
              value={
                data.avg_time_per_card_ms == null
                  ? "—"
                  : (data.avg_time_per_card_ms / 1000).toFixed(1)
              }
              hint={`${totalReviews.toLocaleString()} reviews in the last ${days} days`}
            />
            <Tile
              label="Words known"
              value={data.known_total.toLocaleString()}
              hint={`${data.streak_days} day streak · ${demotions} returned to the stack`}
              tone="var(--color-comp-lime)"
            />
          </div>

          <Card>
            <CardHeader className="pb-2">
              <CardTitle>Reviews per day</CardTitle>
            </CardHeader>
            <CardContent>
              <LineChart
                points={reviewPoints}
                yMin={0}
                xFmt={(x) => new Date(x).toLocaleDateString(undefined, { month: "short", day: "numeric" })}
              />
            </CardContent>
          </Card>

          <div className="grid gap-4 lg:grid-cols-2">
            <Card>
              <CardHeader className="pb-2">
                <CardTitle>How it went</CardTitle>
              </CardHeader>
              <CardContent>
                <StackedBar segments={ratingSegments} />
              </CardContent>
            </Card>
            <Card>
              <CardHeader className="pb-2">
                <CardTitle>Where your cards are</CardTitle>
              </CardHeader>
              <CardContent>
                <StackedBar segments={stateSegments} />
              </CardContent>
            </Card>
          </div>

          <div className="grid gap-4 lg:grid-cols-2">
            <Card>
              <CardHeader className="pb-2">
                <CardTitle>Coming due</CardTitle>
              </CardHeader>
              <CardContent>
                <BarChart bars={forecastBars} />
                <p className="mt-2 text-xs text-faint">Next 30 study days</p>
              </CardContent>
            </Card>
            <Card>
              <CardHeader className="pb-2">
                <CardTitle>Interval spread</CardTitle>
              </CardHeader>
              <CardContent>
                <BarChart bars={intervalBars} color="var(--color-brand-dim)" />
                <p className="mt-2 text-xs text-faint">
                  How far apart your cards are scheduled — the right-hand bars are words you have
                  nearly finished with.
                </p>
              </CardContent>
            </Card>
          </div>
        </div>
      )}
    </div>
  );
}
