import { Link } from "react-router-dom";
import { ArrowRight, Sparkles, SkipForward } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { useCandidates, useWordAction } from "@/lib/srs-hooks";
import { ScoreBar, whyText } from "./candidates-table";
import { cn } from "@/lib/utils";

/**
 * The Deck's small window onto the candidate pool: the five words most likely
 * to become cards next, in plain language, with the two actions that matter.
 */
export function CandidatesPreview({ className }: { className?: string }) {
  const { data, isLoading } = useCandidates({ status: "unjudged", sort: "score", limit: 5 });
  const word = useWordAction();
  const items = data?.items ?? [];
  const maxScore = items.reduce((m, c) => Math.max(m, c.score), 0);

  return (
    <Card className={cn("flex flex-col", className)}>
      <CardHeader className="flex-row items-center justify-between gap-3 pb-3">
        <CardTitle className="flex items-center gap-2">
          <Sparkles className="size-4 text-brand-bright" />
          Up next
        </CardTitle>
        <Link
          to="/study/stack?tab=upnext"
          className="inline-flex items-center gap-1 text-xs font-medium text-muted transition-colors hover:text-brand-bright"
        >
          All candidates
          <ArrowRight className="size-3.5" />
        </Link>
      </CardHeader>
      <CardContent className="flex flex-1 flex-col gap-2.5">
        {isLoading &&
          Array.from({ length: 4 }).map((_, i) => <Skeleton key={i} className="h-12 w-full rounded-lg" />)}

        {!isLoading && items.length === 0 && (
          <p className="rounded-lg border border-dashed border-border-strong px-3 py-6 text-center text-xs text-faint">
            No words are queued for judging — recount the corpus from the Stack&apos;s Up next tab.
          </p>
        )}

        {items.map((c) => (
          <div key={c.lemma} className="flex items-start gap-3">
            <div className="min-w-0 flex-1">
              <div className="flex min-w-0 flex-wrap items-baseline gap-x-2 gap-y-1">
                <Link
                  to={`/moments?q=${encodeURIComponent(c.lemma)}`}
                  className="font-jp text-sm font-medium text-fg transition-colors hover:text-brand-bright"
                  lang="ja"
                >
                  {c.lemma}
                </Link>
                {c.gloss && (
                  <span className="min-w-0 flex-1 basis-0 truncate text-xs text-muted">{c.gloss}</span>
                )}
                <ScoreBar score={c.score} max={maxScore} />
              </div>
              <p className="mt-0.5 text-[0.7rem] leading-snug text-faint">{whyText(c)}</p>
            </div>
            <div className="flex shrink-0 items-center gap-1">
              <button
                type="button"
                onClick={() => word.mutate({ lemma: c.lemma, op: "skip" })}
                disabled={word.isPending}
                title="Not interested in this word"
                className="grid size-7 place-items-center rounded-lg text-faint transition-colors hover:bg-surface-hover hover:text-fg disabled:opacity-50"
              >
                <SkipForward className="size-3.5" />
              </button>
              <Button
                variant="ghost"
                size="sm"
                onClick={() => word.mutate({ lemma: c.lemma, op: "judge" })}
                disabled={word.isPending}
                title="Look for a clear moment and make a card now"
              >
                Judge now
              </Button>
            </div>
          </div>
        ))}
      </CardContent>
    </Card>
  );
}
