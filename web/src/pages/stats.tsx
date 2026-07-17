import { useMemo } from "react";
import { BarChart3, BookMarked, Clapperboard, Loader2, Sparkles, TrendingUp } from "lucide-react";
import { useStats } from "@/lib/hooks";
import { PageHeader } from "@/components/layout/page-header";
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { ErrorState } from "@/components/ui/error-state";
import { Skeleton } from "@/components/ui/skeleton";
import { LineChart, BarChart, StackedBar } from "@/components/charts";
import type { StatsResponse } from "@/lib/types";

const DAYS = 90;

export function StatsPage() {
  const { data, isLoading, isError, refetch } = useStats(DAYS);

  return (
    <div className="animate-fade-in">
      <PageHeader
        title="Stats"
        subtitle="Your immersion at a glance — vocabulary growth, comprehension trend, and watch history."
      />

      {isLoading && <StatsSkeleton />}
      {isError && <ErrorState onRetry={() => refetch()} />}
      {data && <StatsBody data={data} />}
    </div>
  );
}

function StatsBody({ data }: { data: StatsResponse }) {
  const known = data.known_series;
  const knownNow = known.length > 0 ? known[known.length - 1].known : 0;
  const knownDelta = known.length > 1 ? knownNow - known[0].known : 0;

  const knownPoints = useMemo(
    () => known.map((p) => ({ x: new Date(p.captured_at).getTime(), y: p.known })),
    [known],
  );
  const avgPoints = useMemo(
    () =>
      data.comprehension_series.map((p) => ({
        x: new Date(p.captured_at).getTime(),
        y: p.avg_pct,
      })),
    [data.comprehension_series],
  );
  const sweetPoints = useMemo(
    () =>
      data.comprehension_series.map((p) => ({
        x: new Date(p.captured_at).getTime(),
        y: p.sweet_count,
      })),
    [data.comprehension_series],
  );

  // last 30 days, zero-filled, so gaps read as "didn't watch" instead of vanishing
  const watchedBars = useMemo(() => {
    const byDay = new Map(data.watched_by_day.map((d) => [d.day.slice(0, 10), d.episodes]));
    const out: { label: string; value: number }[] = [];
    const today = new Date();
    for (let i = 29; i >= 0; i--) {
      const d = new Date(today);
      d.setDate(today.getDate() - i);
      const key = d.toISOString().slice(0, 10);
      out.push({
        label: d.toLocaleDateString(undefined, { month: "short", day: "numeric" }),
        value: byDay.get(key) ?? 0,
      });
    }
    return out;
  }, [data.watched_by_day]);

  const hasAnything =
    known.length > 0 || data.comprehension_series.length > 0 || data.watched_total > 0;

  if (!hasAnything) {
    return (
      <EmptyState
        icon={BarChart3}
        title="No history yet"
        description="Stats fill in as you sync known words, analyze comprehension, and watch episodes. Come back after a few sessions."
      />
    );
  }

  return (
    <div className="space-y-6">
      {/* top stat cards */}
      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <StatCard
          icon={BookMarked}
          label="Known words"
          value={knownNow.toLocaleString()}
          sub={
            knownDelta !== 0 ? (
              <span className="inline-flex items-center gap-1 text-comp-green">
                <TrendingUp className="size-3.5" />
                {knownDelta > 0 ? "+" : ""}
                {knownDelta.toLocaleString()} in {DAYS} days
              </span>
            ) : (
              "no change recorded yet"
            )
          }
        />
        <StatCard
          icon={Clapperboard}
          label="Watched"
          value={`${data.total_views.toLocaleString()} views`}
          sub={`${data.watched_total.toLocaleString()} episodes${
            data.rewatches > 0 ? ` · ${data.rewatches} rewatch${data.rewatches === 1 ? "" : "es"}` : ""
          } · ${(data.watched_minutes_total / 60).toLocaleString(undefined, {
            maximumFractionDigits: 1,
          })} h of immersion`}
        />
        <StatCard
          icon={Loader2}
          label="In progress"
          value={String(data.in_progress)}
          sub="shows currently being watched"
        />
        <StatCard
          icon={Sparkles}
          label="Avg comprehension"
          value={`${Math.round(data.now.avg_pct)}%`}
          sub={`across ${data.now.scored.toLocaleString()} scored episodes`}
        />
      </div>

      {/* band distribution */}
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-sm">Where your library sits right now</CardTitle>
          <CardDescription className="text-xs">
            Scored episodes by comprehension band — the sweet spot is where immersion pays off
            most.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <StackedBar
            segments={[
              { label: "Easy (95%+)", value: data.now.easy, color: "var(--color-comp-green)" },
              { label: "Sweet spot (80–95%)", value: data.now.sweet, color: "var(--color-comp-lime)" },
              { label: "Almost (65–80%)", value: data.now.almost, color: "var(--color-comp-amber)" },
              { label: "Hard (<65%)", value: data.now.hard, color: "var(--color-comp-red)" },
            ]}
          />
        </CardContent>
      </Card>

      {/* charts */}
      <div className="grid gap-6 lg:grid-cols-2">
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm">Known words over time</CardTitle>
          </CardHeader>
          <CardContent>
            <LineChart points={knownPoints} color="var(--color-comp-green)" />
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm">Comprehension over time</CardTitle>
            <CardDescription className="text-xs">
              Average % across scored episodes, and how many sit in the sweet spot — each on its
              own scale.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            <div>
              <p className="mb-1 text-[0.7rem] font-medium uppercase tracking-wide text-faint">
                Avg comprehension %
              </p>
              <LineChart
                points={avgPoints}
                color="var(--color-brand-bright)"
                yFmt={(y) => `${Math.round(y)}%`}
              />
            </div>
            <div>
              <p className="mb-1 text-[0.7rem] font-medium uppercase tracking-wide text-faint">
                Episodes in the sweet spot
              </p>
              <LineChart points={sweetPoints} color="var(--color-comp-lime)" />
            </div>
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-sm">Watched per day</CardTitle>
          <CardDescription className="text-xs">
            Episodes finished per day over the last 30 days.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <BarChart bars={watchedBars} />
        </CardContent>
      </Card>
    </div>
  );
}

function StatCard({
  icon: Icon,
  label,
  value,
  sub,
}: {
  icon: React.ComponentType<{ className?: string }>;
  label: string;
  value: string;
  sub?: React.ReactNode;
}) {
  return (
    <Card>
      <CardContent className="p-5">
        <p className="flex items-center gap-1.5 text-[0.7rem] font-medium uppercase tracking-wide text-faint">
          <Icon className="size-3.5 text-brand-bright" />
          {label}
        </p>
        <p className="mt-1.5 text-2xl font-bold tabular-nums tracking-tight text-fg">{value}</p>
        {sub && <p className="mt-1 text-xs text-muted">{sub}</p>}
      </CardContent>
    </Card>
  );
}

function StatsSkeleton() {
  return (
    <div className="space-y-6">
      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        {Array.from({ length: 4 }).map((_, i) => (
          <Skeleton key={i} className="h-28 rounded-xl" />
        ))}
      </div>
      <Skeleton className="h-24 w-full rounded-xl" />
      <div className="grid gap-6 lg:grid-cols-2">
        <Skeleton className="h-64 rounded-xl" />
        <Skeleton className="h-64 rounded-xl" />
      </div>
    </div>
  );
}
