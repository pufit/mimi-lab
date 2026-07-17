import { useComprehension } from "@/lib/hooks";
import { Skeleton } from "@/components/ui/skeleton";
import { ComprehensionBar, ComprehensionPill } from "@/components/comprehension";
import { ratingLabel } from "@/lib/utils";
import { BookOpen, Sparkles } from "lucide-react";
import type { NewWord } from "@/lib/types";

export function ComprehensionPanel({ episodeId, open }: { episodeId: number; open: boolean }) {
  const { data, isLoading, isError, error } = useComprehension(episodeId, open);

  if (isLoading) {
    return (
      <div className="space-y-4 p-5">
        <Skeleton className="h-16 w-full" />
        <Skeleton className="h-32 w-full" />
      </div>
    );
  }

  if (isError) {
    return (
      <p className="p-5 text-sm text-muted">
        Comprehension unavailable
        {error instanceof Error ? ` — ${error.message}` : ""}. This needs an ingested subtitle.
      </p>
    );
  }

  if (!data) return null;

  return (
    <div className="space-y-5 p-5">
      {/* headline stats */}
      <div className="flex flex-wrap items-center gap-x-6 gap-y-3">
        <div className="flex items-center gap-3">
          <ComprehensionPill pct={data.comprehension_pct} size="md" />
          {data.rating && (
            <span className="text-sm text-muted">{ratingLabel(data.rating)}</span>
          )}
        </div>
        <Stat label="Known tokens" value={`${data.known_tokens} / ${data.total_tokens}`} />
        <Stat label="New unique words" value={String(data.unknown_unique)} />
        <span className="ml-auto inline-flex items-center gap-1.5 rounded-md border border-border bg-bg-elevated px-2 py-1 text-[0.7rem] font-medium text-faint">
          <Sparkles className="size-3" />
          {data.source === "exact" ? "Migaku-exact" : "Aligned preview"}
        </span>
      </div>

      <ComprehensionBar pct={data.comprehension_pct} />

      {/* new words */}
      {data.new_words.length > 0 ? (
        <div>
          <div className="mb-2.5 flex items-center gap-2">
            <BookOpen className="size-4 text-brand-bright" />
            <h4 className="text-sm font-semibold text-fg">Study these first</h4>
            <span className="text-xs text-faint">{data.new_words.length} words</span>
          </div>
          <div className="grid gap-1.5 sm:grid-cols-2">
            {data.new_words.slice(0, 30).map((w, i) => (
              <NewWordRow key={`${w.lemma}-${i}`} word={w} />
            ))}
          </div>
        </div>
      ) : (
        <p className="text-sm text-muted">No new words — you know everything in this episode.</p>
      )}
    </div>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <p className="text-[0.7rem] uppercase tracking-wide text-faint">{label}</p>
      <p className="text-sm font-semibold tabular-nums text-fg">{value}</p>
    </div>
  );
}

function NewWordRow({ word }: { word: NewWord }) {
  return (
    <div className="flex items-center gap-3 rounded-lg border border-border bg-bg-elevated/60 px-3 py-2">
      <div className="min-w-0 flex-1">
        <div className="flex items-baseline gap-2">
          <span className="font-jp text-base font-medium text-fg">{word.lemma}</span>
          {word.reading && word.reading !== word.lemma && (
            <span className="font-jp text-xs text-brand-bright">{word.reading}</span>
          )}
          {word.count > 1 && (
            <span className="text-[0.65rem] text-faint">×{word.count}</span>
          )}
        </div>
        {word.gloss && <p className="line-clamp-1 text-xs text-muted">{word.gloss}</p>}
      </div>
      {word.freq_rank != null && (
        <span
          className="shrink-0 rounded-md border border-border bg-surface px-1.5 py-0.5 text-[0.65rem] font-medium tabular-nums text-faint"
          title="Frequency rank"
        >
          #{word.freq_rank.toLocaleString()}
        </span>
      )}
    </div>
  );
}
