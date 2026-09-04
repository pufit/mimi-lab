import { useEffect, useMemo } from "react";
import { useNavigate } from "react-router-dom";
import { GraduationCap, Play, Sparkles } from "lucide-react";
import { PageHeader } from "@/components/layout/page-header";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { ErrorState } from "@/components/ui/error-state";
import { Skeleton } from "@/components/ui/skeleton";
import { Kbd } from "@/components/ui/kbd";
import { BarChart } from "@/components/charts";
import { StudyTabs } from "@/components/srs/study-tabs";
import { DeckTiles } from "@/components/srs/deck-tiles";
import { DeckSettingsRow } from "@/components/srs/deck-settings-row";
import { StackRail } from "@/components/srs/stack-rail";
import { CandidatesPreview } from "@/components/srs/candidates-preview";
import { GenerationPanel } from "@/components/srs/generation-panel";
import {
  errMsg,
  useGenerateCards,
  useImportCards,
  useSrsStats,
  useSrsSummary,
} from "@/lib/srs-hooks";

/** Deck (design §8.5): today's numbers, the three settings, what's coming. */
export function StudyPage() {
  const navigate = useNavigate();
  const { data: summary, isLoading, isError, error, refetch } = useSrsSummary();
  const stats = useSrsStats(30);
  const generate = useGenerateCards();
  const importDeck = useImportCards();

  const due = summary ? summary.due_learning + summary.due_review : 0;
  const newLeft = summary
    ? Math.max(0, Math.min(summary.new_today_limit - summary.new_today_done, summary.new_available))
    : 0;
  const sessionSize = due + newLeft;
  const gen = summary?.generation;
  const deckEmpty = summary ? Object.values(summary.states).every((n) => n === 0) : false;

  // Enter starts a session from anywhere on the page (§8.5).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Enter" || e.metaKey || e.ctrlKey || e.altKey) return;
      const t = e.target as HTMLElement | null;
      if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable)) return;
      if (sessionSize === 0) return;
      e.preventDefault();
      navigate("/study/review");
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [navigate, sessionSize]);

  const activity = useMemo(
    () =>
      (stats.data?.days ?? []).slice(-30).map((d) => ({
        label: d.day.slice(5),
        value: d.reviews,
      })),
    [stats.data],
  );

  const subtitle = !summary
    ? "Your own deck, cut from the shows you watch."
    : sessionSize > 0
      ? `${due} due · ${newLeft} new left today`
      : summary.new_stack_total > 0
        ? "All caught up — the stack is waiting for tomorrow."
        : "All caught up.";

  return (
    <div className="animate-fade-in">
      <PageHeader
        title="Study"
        subtitle={subtitle}
        actions={
          <>
            <Button
              variant="play"
              size="lg"
              disabled={sessionSize === 0}
              onClick={() => navigate("/study/review")}
              title={sessionSize === 0 ? "Nothing to review right now" : "Start reviewing"}
            >
              <Play className="size-4 fill-current" />
              {sessionSize > 0 ? `Review ${sessionSize}` : "Nothing due"}
              {sessionSize > 0 && <Kbd className="ml-1 border-white/30 bg-white/15 text-white">↵</Kbd>}
            </Button>
            <Button
              variant="ghost"
              onClick={() => generate.mutate({})}
              loading={generate.isPending || !!gen?.running}
              disabled={!gen?.llm_available}
              title={gen?.llm_available ? "Add more cards to the stack" : "Needs an Anthropic API key"}
            >
              <Sparkles className="size-4" />
              {gen?.running
                ? `Generating ${gen.runs[0]?.batches_done ?? 0}/${gen.runs[0]?.batches_planned ?? 0}`
                : "Generate more"}
            </Button>
          </>
        }
      />

      <StudyTabs />

      {isLoading && (
        <div className="space-y-4">
          <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
            {Array.from({ length: 4 }).map((_, i) => (
              <Skeleton key={i} className="h-28 w-full rounded-xl" />
            ))}
          </div>
          <Skeleton className="h-24 w-full rounded-xl" />
          <Skeleton className="h-40 w-full rounded-xl" />
        </div>
      )}

      {isError && <ErrorState message={errMsg(error)} onRetry={() => refetch()} />}

      {summary && (
        <div className="space-y-6">
          <DeckTiles summary={summary} />

          <DeckSettingsRow settings={summary.settings} />

          {deckEmpty ? (
            <EmptyState
              icon={GraduationCap}
              title="No cards yet"
              description="A hand-curated starter deck is ready to import — about a thousand words taken from the shows you have already watched. Clips are cut in the background, so you can start studying right away."
              action={
                <Button
                  variant="primary"
                  size="lg"
                  loading={importDeck.isPending}
                  onClick={() => importDeck.mutate(undefined)}
                >
                  Import the curated deck
                </Button>
              }
            />
          ) : (
            <StackRail total={summary.new_stack_total} />
          )}

          <div className="grid gap-4 lg:grid-cols-2">
            <CandidatesPreview />
            <Card>
              <CardHeader className="pb-2">
                <CardTitle>Activity</CardTitle>
              </CardHeader>
              <CardContent>
                {stats.isLoading ? (
                  <Skeleton className="h-40 w-full rounded-lg" />
                ) : (
                  <BarChart bars={activity} />
                )}
                <p className="mt-2 text-xs text-faint">
                  Reviews per day, last 30 days
                  {summary.time_today_ms > 0 &&
                    ` · ${Math.round(summary.time_today_ms / 60000)} min today`}
                </p>
              </CardContent>
            </Card>
          </div>

          <GenerationPanel summary={summary} />
        </div>
      )}
    </div>
  );
}
