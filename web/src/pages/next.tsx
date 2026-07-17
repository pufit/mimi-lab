import { useState } from "react";
import { Link } from "react-router-dom";
import { Play, Sparkles, Target, History } from "lucide-react";
import { useSweetSpot, usePlay } from "@/lib/hooks";
import { PageHeader } from "@/components/layout/page-header";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/empty-state";
import { ErrorState } from "@/components/ui/error-state";
import { Skeleton } from "@/components/ui/skeleton";
import { ComprehensionPill } from "@/components/comprehension";
import { cn } from "@/lib/utils";
import type { SweetSpotItem } from "@/lib/types";

// The i+1 "comprehensible input" bands. The sweet spot (80–95%) is where
// immersion is most effective — you can follow along while still learning.
const BANDS = [
  { key: "sweet", label: "Sweet spot", lo: 80, hi: 95, hint: "80–95% — ideal for immersion" },
  { key: "almost", label: "Almost there", lo: 65, hi: 80, hint: "65–80% — a stretch, worth prepping" },
  { key: "easy", label: "Easy wins", lo: 95, hi: 101, hint: "95%+ — effortless watching" },
] as const;

const LIMIT = 80;

export function NextPage() {
  const [bandKey, setBandKey] = useState<(typeof BANDS)[number]["key"]>("sweet");
  const [includeWatched, setIncludeWatched] = useState(false);

  // one query per band so every tab shows a live count (results are cached)
  const queries = {
    sweet: useSweetSpot(BANDS[0].lo, BANDS[0].hi, LIMIT, includeWatched),
    almost: useSweetSpot(BANDS[1].lo, BANDS[1].hi, LIMIT, includeWatched),
    easy: useSweetSpot(BANDS[2].lo, BANDS[2].hi, LIMIT, includeWatched),
  } as const;

  const band = BANDS.find((b) => b.key === bandKey) ?? BANDS[0];
  const { data, isLoading, isError, refetch } = queries[bandKey];

  return (
    <div className="animate-fade-in">
      <PageHeader
        title="What to watch next"
        subtitle="Shows ranked by how much of their next unwatched episode you already understand."
      />

      {/* band selector */}
      <div className="mb-6 flex flex-wrap items-center gap-2">
        {BANDS.map((b) => {
          const count = queries[b.key].data?.length;
          return (
            <button
              key={b.key}
              onClick={() => setBandKey(b.key)}
              title={b.hint}
              className={cn(
                "inline-flex items-center gap-1.5 rounded-full border px-3.5 py-1.5 text-sm font-medium transition",
                b.key === band.key
                  ? "border-brand bg-brand/15 text-brand-bright"
                  : "border-border-strong bg-bg-elevated/60 text-muted hover:text-fg",
              )}
            >
              {b.label}
              {count != null && (
                <span className="rounded-full bg-bg-elevated px-1.5 py-0.5 text-[0.65rem] tabular-nums opacity-80">
                  {count}
                </span>
              )}
            </button>
          );
        })}
        <span className="self-center pl-1 text-xs text-faint">{band.hint}</span>

        <label className="ml-auto flex cursor-pointer select-none items-center gap-2 text-xs text-muted">
          <input
            type="checkbox"
            checked={includeWatched}
            onChange={(e) => setIncludeWatched(e.target.checked)}
            className="size-4 accent-[var(--color-brand)]"
          />
          <History className="size-3.5 text-faint" />
          Include watched (rewatch)
        </label>
      </div>

      {isError ? (
        <ErrorState message="Couldn't load recommendations." onRetry={() => refetch()} />
      ) : isLoading ? (
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {Array.from({ length: 6 }).map((_, i) => (
            <Skeleton key={i} className="h-28 rounded-2xl" />
          ))}
        </div>
      ) : !data || data.length === 0 ? (
        <EmptyState
          icon={Target}
          title={`Nothing in "${band.label}" right now`}
          description={
            includeWatched
              ? "No shows sit in this comprehension band. Analyze more of your library (Library → Analyze comprehension) so episodes can be ranked, or try another band."
              : "Every episode in this band is watched, or nothing is scored yet. Try another band, flip on rewatch, or analyze more of your library."
          }
        />
      ) : (
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {data.map((it) => (
            <SweetSpotCard key={it.episode_id} item={it} />
          ))}
        </div>
      )}
    </div>
  );
}

function SweetSpotCard({ item }: { item: SweetSpotItem }) {
  // per-card mutation instance — one card spinning must not spin them all
  const play = usePlay();
  const more = (item.band_episodes ?? 1) - 1;

  return (
    <div className="flex gap-3 rounded-2xl border border-border-strong bg-surface/40 p-3 transition hover:border-brand/40">
      <Link to={`/title/${item.anilist_id}`} className="shrink-0">
        {item.cover_url ? (
          <img
            src={item.cover_url}
            alt=""
            className="h-24 w-16 rounded-lg object-cover"
            loading="lazy"
          />
        ) : (
          <div className="grid h-24 w-16 place-items-center rounded-lg bg-bg-elevated text-faint">
            <Sparkles className="size-5" />
          </div>
        )}
      </Link>

      <div className="flex min-w-0 flex-1 flex-col">
        <Link
          to={`/title/${item.anilist_id}`}
          className="line-clamp-2 text-sm font-semibold text-fg hover:text-brand-bright"
        >
          {item.title}
        </Link>
        <div className="mt-0.5 text-xs text-muted">
          Episode {item.ep_number}
          {item.watched && <span className="text-faint"> · rewatch</span>}
        </div>
        {more > 0 && (
          <div className="mt-0.5 text-[0.7rem] text-brand-bright">
            +{more} more at this level
          </div>
        )}

        <div className="mt-2 flex flex-wrap items-center gap-2">
          <ComprehensionPill pct={item.comprehension_pct} size="sm" />
          {item.new_word_count != null && (
            <span className="text-[0.7rem] text-faint">
              {item.new_word_count} new word{item.new_word_count === 1 ? "" : "s"}
            </span>
          )}
        </div>

        <div className="mt-auto pt-2">
          {item.has_video ? (
            <Button
              size="sm"
              loading={play.isPending}
              onClick={() => play.mutate({ episodeId: item.episode_id })}
            >
              <Play className="size-3.5" /> Play in Migaku
            </Button>
          ) : (
            <Link
              to={`/title/${item.anilist_id}`}
              className="text-xs font-medium text-brand-bright hover:underline"
            >
              Not downloaded — get it →
            </Link>
          )}
        </div>
      </div>
    </div>
  );
}
