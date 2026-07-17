import { useMemo, useState } from "react";
import { ListChecks, FileVideo, Check, Tv, Star, Search, Sparkles, Trash2 } from "lucide-react";
import {
  useQueue,
  useConfirmMatch,
  useDeleteQueueItem,
  useMatchSearch,
  useSuggestMatch,
} from "@/lib/hooks";
import { PageHeader } from "@/components/layout/page-header";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { EmptyState } from "@/components/ui/empty-state";
import { ErrorState } from "@/components/ui/error-state";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";
import type { MatchCandidate, MatchQueueItem } from "@/lib/types";

export function QueuePage() {
  const { data, isLoading, isError, refetch } = useQueue();

  return (
    <div className="animate-fade-in">
      <PageHeader
        title="Match Queue"
        subtitle="Files we couldn't match with confidence. Pick the right AniList title to link them."
      />

      {isLoading && (
        <div className="space-y-4">
          {Array.from({ length: 2 }).map((_, i) => (
            <Skeleton key={i} className="h-44 w-full rounded-2xl" />
          ))}
        </div>
      )}

      {isError && <ErrorState onRetry={() => refetch()} />}

      {data && data.length === 0 && (
        <EmptyState
          icon={Check}
          title="Queue is clear"
          description="Every downloaded file is matched to a title. New unmatched files will show up here automatically."
        />
      )}

      {data && data.length > 0 && (
        <div className="space-y-4">
          {data.map((item) => (
            <QueueItem key={item.id} item={item} />
          ))}
        </div>
      )}
    </div>
  );
}

function QueueItem({ item }: { item: MatchQueueItem }) {
  const confirm = useConfirmMatch();
  const remove = useDeleteQueueItem();
  const search = useMatchSearch();
  const suggest = useSuggestMatch();

  const [query, setQuery] = useState(item.title_guess ?? "");
  const [extra, setExtra] = useState<MatchCandidate[]>([]);
  const [matchedBy, setMatchedBy] = useState<string | null>(null);
  const [selected, setSelected] = useState<number | null>(item.candidates[0]?.anilist_id ?? null);

  // merge auto candidates with manual/Haiku results, de-duped by anilist_id
  const candidates = useMemo(() => {
    const seen = new Set<number>();
    const out: MatchCandidate[] = [];
    for (const c of [...extra, ...item.candidates]) {
      if (seen.has(c.anilist_id)) continue;
      seen.add(c.anilist_id);
      out.push(c);
    }
    return out;
  }, [extra, item.candidates]);

  const runSearch = () => {
    const q = query.trim();
    if (!q) return;
    search.mutate(
      { q, epNumber: item.ep_number },
      {
        onSuccess: (cands) => {
          setExtra(cands);
          setMatchedBy("manual");
          if (cands[0]) setSelected(cands[0].anilist_id);
        },
      },
    );
  };

  const runSuggest = () => {
    suggest.mutate(item.id, {
      onSuccess: (res) => {
        setQuery(res.suggested_title);
        setExtra(res.candidates);
        setMatchedBy(res.matched_by);
        if (res.candidates[0]) setSelected(res.candidates[0].anilist_id);
      },
    });
  };

  return (
    <div className="overflow-hidden rounded-2xl border border-border bg-surface/60">
      <div className="flex items-center gap-3 border-b border-border bg-bg-elevated/50 px-4 py-3">
        <FileVideo className="size-4 shrink-0 text-faint" />
        <p className="min-w-0 flex-1 truncate font-mono text-sm text-fg">{item.filename}</p>
        {item.ep_number != null && <Badge variant="default">EP {item.ep_number}</Badge>}
        <Button
          variant="ghost"
          size="icon-sm"
          onClick={() => remove.mutate(item.id)}
          disabled={remove.isPending}
          aria-label="Remove from queue"
          title="Remove from queue"
          className="text-faint hover:text-comp-red"
        >
          <Trash2 className="size-4" />
        </Button>
      </div>

      <div className="p-4">
        {/* manual resolve toolbar */}
        <div className="mb-3 flex flex-col gap-2 sm:flex-row sm:items-center">
          <div className="relative flex-1">
            <Search className="pointer-events-none absolute left-3 top-1/2 size-3.5 -translate-y-1/2 text-faint" />
            <Input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && runSearch()}
              placeholder="Search AniList by title…"
              className="h-9 pl-9 text-sm"
            />
          </div>
          <Button variant="secondary" size="sm" onClick={runSearch} loading={search.isPending}>
            <Search className="size-3.5" />
            Search
          </Button>
          <Button
            variant="ghost"
            size="sm"
            onClick={runSuggest}
            loading={suggest.isPending}
            title="Let Claude Haiku identify the title from the filename"
          >
            <Sparkles className="size-3.5 text-brand-bright" />
            Suggest
          </Button>
        </div>

        {matchedBy === "haiku" && (
          <p className="mb-2 inline-flex items-center gap-1 text-xs text-brand-bright">
            <Sparkles className="size-3" /> Haiku suggested “{query}”
          </p>
        )}

        {candidates.length === 0 ? (
          <p className="py-4 text-center text-sm text-muted">
            No candidates yet — search by title or hit{" "}
            <span className="text-brand-bright">Suggest</span> to identify it, or remove the item.
          </p>
        ) : (
          <>
            <p className="mb-3 flex items-center gap-2 text-xs uppercase tracking-wide text-faint">
              <ListChecks className="size-3.5" />
              {candidates.length} candidate{candidates.length === 1 ? "" : "s"} · pick one
            </p>
            <div className="grid gap-2.5 sm:grid-cols-2 xl:grid-cols-3">
              {candidates.map((c) => (
                <CandidateCard
                  key={c.anilist_id}
                  candidate={c}
                  selected={selected === c.anilist_id}
                  onSelect={() => setSelected(c.anilist_id)}
                />
              ))}
            </div>
            <div className="mt-4 flex justify-end">
              <Button
                variant="primary"
                disabled={selected == null}
                loading={confirm.isPending}
                onClick={() =>
                  selected != null && confirm.mutate({ queueId: item.id, anilistId: selected })
                }
              >
                <Check className="size-4" />
                Confirm match
              </Button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}

function CandidateCard({
  candidate,
  selected,
  onSelect,
}: {
  candidate: MatchCandidate;
  selected: boolean;
  onSelect: () => void;
}) {
  const name = candidate.english || candidate.romaji || `AniList #${candidate.anilist_id}`;
  return (
    <button
      onClick={onSelect}
      className={cn(
        "flex items-start gap-3 rounded-xl border p-2.5 text-left transition-all",
        selected
          ? "border-brand bg-brand/10 ring-1 ring-brand/40"
          : "border-border bg-bg-elevated/50 hover:border-border-strong",
      )}
    >
      <div className="relative h-20 w-14 shrink-0 overflow-hidden rounded-md border border-border bg-surface">
        {candidate.cover_url ? (
          <img src={candidate.cover_url} alt="" className="size-full object-cover" />
        ) : (
          <div className="grid size-full place-items-center">
            <Tv className="size-5 text-faint" />
          </div>
        )}
        {selected && (
          <div className="absolute inset-0 grid place-items-center bg-brand/40">
            <Check className="size-5 text-white" />
          </div>
        )}
      </div>
      <div className="min-w-0 flex-1">
        <p className="line-clamp-2 text-sm font-medium leading-snug text-fg">{name}</p>
        {candidate.romaji && candidate.romaji !== name && (
          <p className="mt-0.5 line-clamp-1 text-xs text-faint">{candidate.romaji}</p>
        )}
        <div className="mt-1.5 flex flex-wrap items-center gap-1.5 text-[0.7rem] text-faint">
          {candidate.format && <Badge variant="outline">{candidate.format}</Badge>}
          {candidate.year && <span>{candidate.year}</span>}
          {candidate.episodes != null && <span>· {candidate.episodes} eps</span>}
        </div>
        <div className="mt-1.5 flex items-center gap-1 text-[0.7rem]">
          <Star className="size-3 fill-comp-amber text-comp-amber" />
          <span className="font-medium text-muted">{candidate.score.toFixed(1)} match</span>
        </div>
      </div>
    </button>
  );
}
