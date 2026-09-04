import { CircleAlert, Download, HardDrive, KeyRound, Sparkles, Wand2 } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Progress } from "@/components/ui/progress";
import { useGeneration, useGenerateCards, useImportCards } from "@/lib/srs-hooks";
import { cn, formatBytes, formatRelative } from "@/lib/utils";
import type { SrsGenerationRun, SrsSummary } from "@/lib/srs-types";

function runTone(state: string): "success" | "warning" | "danger" | "default" {
  if (state === "done" || state === "finished") return "success";
  if (state === "error" || state === "failed") return "danger";
  if (state === "cancelled" || state === "stale") return "warning";
  return "default";
}

function RunRow({ run }: { run: SrsGenerationRun }) {
  const live = run.finished_at == null;
  const planned = Math.max(run.batches_planned, 0);
  return (
    <div className="flex flex-col gap-1 border-b border-border/50 py-2.5 last:border-0">
      <div className="flex flex-wrap items-center gap-2">
        <Badge variant={runTone(run.state)}>{run.state}</Badge>
        <span className="text-xs text-muted">
          {run.cards_created} card{run.cards_created === 1 ? "" : "s"} from {run.moments_judged}{" "}
          moment{run.moments_judged === 1 ? "" : "s"}
        </span>
        <span className="text-[0.7rem] text-faint">· {run.trigger}</span>
        <span className="ml-auto text-[0.7rem] text-faint">{formatRelative(run.started_at)}</span>
      </div>
      {live && planned > 0 && (
        <div className="flex items-center gap-2">
          <Progress value={run.batches_done} max={planned} className="flex-1" />
          <span className="text-[0.7rem] tabular-nums text-faint">
            {run.batches_done}/{planned}
          </span>
        </div>
      )}
      <p className="text-[0.7rem] text-faint">
        {run.accepted} accepted · {run.rejected} turned down · {run.llm_calls} call
        {run.llm_calls === 1 ? "" : "s"}
        {run.llm_in_tokens + run.llm_out_tokens > 0 &&
          ` · ${((run.llm_in_tokens + run.llm_out_tokens) / 1000).toFixed(1)}k tokens`}
      </p>
      {run.error && <p className="text-[0.7rem] text-comp-red">{run.error}</p>}
    </div>
  );
}

/**
 * Where cards come from: the last generation runs, whether the judge can run
 * at all, how much disk the clips take — and, on an empty deck, the one-click
 * import of the curated starter deck.
 */
export function GenerationPanel({
  summary,
  className,
}: {
  summary: SrsSummary;
  className?: string;
}) {
  const { data } = useGeneration(6);
  const gen = data ?? summary.generation;
  const generate = useGenerateCards();
  const importDeck = useImportCards();
  const deckEmpty = Object.values(summary.states).every((n) => n === 0);

  return (
    <Card className={cn("flex flex-col", className)}>
      <CardHeader className="flex-row items-center justify-between gap-3 pb-3">
        <CardTitle className="flex items-center gap-2">
          <Wand2 className="size-4 text-brand-bright" />
          Where cards come from
        </CardTitle>
        <Button
          variant="secondary"
          size="sm"
          onClick={() => generate.mutate({})}
          loading={generate.isPending || gen.running}
          disabled={!gen.llm_available}
          title={
            gen.llm_available
              ? "Score words, judge their moments and add the good ones to the stack"
              : "Needs an Anthropic API key"
          }
        >
          <Sparkles className="size-3.5" />
          {gen.running ? "Generating…" : "Generate more"}
        </Button>
      </CardHeader>
      <CardContent className="flex flex-1 flex-col gap-3">
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1.5 text-xs text-muted">
          <span className="inline-flex items-center gap-1.5">
            <KeyRound className={cn("size-3.5", gen.llm_available ? "text-comp-green" : "text-comp-amber")} />
            {gen.llm_available ? gen.judge_model_id : "No Anthropic API key"}
          </span>
          <span className="inline-flex items-center gap-1.5">
            <HardDrive className="size-3.5 text-faint" />
            {formatBytes(summary.clips.bytes)} of clips
            {summary.clips.pending > 0 && (
              <span className="text-faint"> · {summary.clips.pending} cutting</span>
            )}
            {summary.clips.failed > 0 && (
              <span className="text-comp-amber"> · {summary.clips.failed} failed</span>
            )}
          </span>
          <span className="text-faint">
            {gen.candidates_ready} candidate{gen.candidates_ready === 1 ? "" : "s"} ready
            {gen.probably_known > 0 && ` · ${gen.probably_known} probably known`}
          </span>
          {gen.census_at && (
            <span className="text-faint">· words counted {formatRelative(gen.census_at)}</span>
          )}
        </div>

        {!gen.llm_available && (
          <p className="flex items-start gap-2 rounded-lg border border-comp-amber/30 bg-comp-amber/10 px-3 py-2 text-xs leading-snug text-comp-amber">
            <CircleAlert className="mt-0.5 size-3.5 shrink-0" />
            Automatic cards need an Anthropic API key in <code>.env</code> — until then, add cards
            from Moments.
          </p>
        )}

        {gen.runs.length > 0 ? (
          <div className="-my-1">
            {gen.runs.slice(0, 5).map((r) => (
              <RunRow key={r.id} run={r} />
            ))}
          </div>
        ) : (
          <p className="rounded-lg border border-dashed border-border-strong px-3 py-6 text-center text-xs text-faint">
            No generation runs yet.
          </p>
        )}

        {deckEmpty && (
          <div className="mt-auto flex flex-wrap items-center gap-3 rounded-lg border border-brand/30 bg-brand/5 px-3 py-2.5">
            <p className="min-w-0 flex-1 text-xs leading-snug text-muted">
              A hand-curated starter deck is waiting in{" "}
              <code className="text-faint">data/srs-curation/cards.json</code>.
            </p>
            <Button
              variant="primary"
              size="sm"
              onClick={() => importDeck.mutate(undefined)}
              loading={importDeck.isPending}
            >
              <Download className="size-3.5" />
              Import curated deck
            </Button>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
