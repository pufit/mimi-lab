import { useMemo } from "react";
import { Link, useParams } from "react-router-dom";
import { ArrowLeft, ChevronLeft, ChevronRight, ScrollText, Sparkles } from "lucide-react";
import { useEpisodes, usePlayMoment, useTranscript } from "@/lib/hooks";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { ErrorState } from "@/components/ui/error-state";
import { EmptyState } from "@/components/ui/empty-state";
import { ComprehensionPill } from "@/components/comprehension";
import { Furigana } from "@/components/furigana";
import { cn, formatMs } from "@/lib/utils";
import type { TranscriptLine, TokenStatus } from "@/lib/types";

const TOKEN_CLS: Record<TokenStatus, string> = {
  UNKNOWN: "text-amber-400 underline decoration-amber-400/40 decoration-2 underline-offset-4",
  LEARNING: "text-sky-400",
  IGNORED: "opacity-50",
  KNOWN: "",
};

const LEGEND: { status: TokenStatus; label: string }[] = [
  { status: "UNKNOWN", label: "Unknown" },
  { status: "LEARNING", label: "Learning" },
  { status: "KNOWN", label: "Known" },
  { status: "IGNORED", label: "Ignored" },
];

export function TranscriptPage() {
  const { episodeId } = useParams();
  const epId = Number(episodeId);
  const { data, isLoading, isError, refetch } = useTranscript(epId);
  // Episode list of the same title → map ep_number±1 to episode ids for prev/next.
  const { data: episodes } = useEpisodes(data?.anilist_id ?? 0);
  const playMoment = usePlayMoment();

  const nav = useMemo(() => {
    if (!data || !episodes) return { prev: null as number | null, next: null as number | null };
    const byNumber = new Map(episodes.map((e) => [e.ep_number, e.id]));
    return {
      prev: byNumber.get(data.ep_number - 1) ?? null,
      next: byNumber.get(data.ep_number + 1) ?? null,
    };
  }, [data, episodes]);

  if (isLoading) return <TranscriptSkeleton />;
  if (isError || !data)
    return (
      <div className="animate-fade-in">
        <Link
          to="/"
          className="mb-4 inline-flex items-center gap-1.5 text-sm text-muted transition-colors hover:text-fg"
        >
          <ArrowLeft className="size-4" />
          Library
        </Link>
        <ErrorState
          message="Transcript unavailable — this episode may not have an ingested subtitle."
          onRetry={() => refetch()}
        />
      </div>
    );

  return (
    <div className="animate-fade-in">
      {/* sticky mini-header */}
      <div className="sticky top-14 z-30 -mx-5 mb-6 border-b border-border bg-bg/90 px-5 py-3 backdrop-blur-xl md:-mx-8 md:top-0 md:px-8">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
          <Link
            to={`/title/${data.anilist_id}`}
            className="inline-flex items-center gap-1.5 text-sm text-muted transition-colors hover:text-fg"
          >
            <ArrowLeft className="size-4" />
            <span className="max-w-56 truncate font-medium text-fg">{data.title}</span>
          </Link>
          <span className="text-sm font-semibold text-fg">E{data.ep_number}</span>
          <ComprehensionPill pct={data.comprehension_pct} size="sm" />
          <Badge
            variant={data.source === "migaku-local" ? "solid" : "outline"}
            title={
              data.source === "migaku-local"
                ? "Word statuses come from Migaku's own tokenizer"
                : "Tokenized locally with fugashi — statuses are a close approximation"
            }
          >
            <Sparkles className="size-3" />
            {data.source === "migaku-local" ? "Migaku" : "fugashi fallback"}
          </Badge>

          <div className="ml-auto flex items-center gap-1.5">
            <EpNavLink to={nav.prev} label={`E${data.ep_number - 1}`} dir="prev" />
            <EpNavLink to={nav.next} label={`E${data.ep_number + 1}`} dir="next" />
          </div>
        </div>
      </div>

      {/* legend */}
      <div className="mb-6 flex flex-wrap items-center gap-2">
        {LEGEND.map((l) => (
          <span
            key={l.status}
            className="inline-flex items-center gap-1.5 rounded-full border border-border bg-surface/60 px-3 py-1 text-xs text-muted"
          >
            <span className={cn("font-jp text-sm leading-none", TOKEN_CLS[l.status])}>語</span>
            {l.label}
          </span>
        ))}
        {data.has_video && (
          <span className="text-xs text-faint">· click a timestamp to play from that line</span>
        )}
      </div>

      {data.lines.length === 0 ? (
        <EmptyState
          icon={ScrollText}
          title="Empty transcript"
          description="This subtitle has no usable lines."
        />
      ) : (
        <div className="max-w-4xl space-y-1">
          {data.lines.map((line) => (
            <TranscriptRow
              key={line.line_id}
              line={line}
              hasVideo={data.has_video}
              onPlay={() => playMoment.mutate(line.line_id)}
            />
          ))}
        </div>
      )}
    </div>
  );
}

function EpNavLink({
  to,
  label,
  dir,
}: {
  to: number | null;
  label: string;
  dir: "prev" | "next";
}) {
  const inner = (
    <>
      {dir === "prev" && <ChevronLeft className="size-4" />}
      {label}
      {dir === "next" && <ChevronRight className="size-4" />}
    </>
  );
  const cls =
    "inline-flex h-8 items-center gap-1 rounded-lg px-2.5 text-xs font-medium transition-colors";
  if (to == null) {
    return (
      <span className={cn(cls, "cursor-not-allowed text-faint opacity-40")}>{inner}</span>
    );
  }
  return (
    <Link
      to={`/transcript/${to}`}
      className={cn(cls, "text-muted hover:bg-surface-hover hover:text-fg")}
      aria-label={`${dir === "prev" ? "Previous" : "Next"} episode transcript`}
    >
      {inner}
    </Link>
  );
}

function TranscriptRow({
  line,
  hasVideo,
  onPlay,
}: {
  line: TranscriptLine;
  hasVideo: boolean;
  onPlay: () => void;
}) {
  return (
    <div className="group flex gap-4 rounded-lg px-3 py-2 transition-colors hover:bg-surface/60">
      {/* timestamp */}
      <div className="w-14 shrink-0 pt-1 text-right">
        {hasVideo ? (
          <button
            onClick={onPlay}
            className="text-xs tabular-nums text-faint transition-colors hover:text-brand-bright"
            title="Play from this line in Migaku"
          >
            {formatMs(line.start_ms)}
          </button>
        ) : (
          <span className="select-none text-xs tabular-nums text-border-strong">
            {formatMs(line.start_ms)}
          </span>
        )}
      </div>

      <div className="min-w-0 flex-1">
        {line.tokens.length > 0 ? (
          <p className="font-jp text-lg leading-relaxed text-fg">
            {line.tokens.map((t, i) => (
              <span
                key={i}
                className={TOKEN_CLS[t.status] || undefined}
                title={
                  t.dict_form || t.reading
                    ? `${t.dict_form || t.surface}${t.reading ? ` (${t.reading})` : ""}`
                    : undefined
                }
              >
                {t.surface}
              </span>
            ))}
          </p>
        ) : (
          <Furigana
            furigana={line.text_furigana}
            text={line.text}
            className="text-lg leading-relaxed text-fg [&_rt]:text-brand-bright"
          />
        )}
        {line.translation && (
          <p className="mt-0.5 text-sm leading-relaxed text-faint">{line.translation}</p>
        )}
      </div>
    </div>
  );
}

function TranscriptSkeleton() {
  return (
    <div className="animate-fade-in">
      <Skeleton className="mb-6 h-12 w-full rounded-xl" />
      <Skeleton className="mb-6 h-8 w-96 rounded-full" />
      <div className="max-w-4xl space-y-3">
        {Array.from({ length: 12 }).map((_, i) => (
          <div key={i} className="flex gap-4">
            <Skeleton className="h-5 w-14 shrink-0" />
            <Skeleton className={cn("h-6", i % 3 === 0 ? "w-3/5" : i % 3 === 1 ? "w-4/5" : "w-2/3")} />
          </div>
        ))}
      </div>
    </div>
  );
}
