import { useState } from "react";
import { Link } from "react-router-dom";
import { ArrowRight, Film, Scissors, Zap } from "lucide-react";
import { Skeleton } from "@/components/ui/skeleton";
import { useSrsCards } from "@/lib/srs-hooks";
import { cn } from "@/lib/utils";
import type { SrsCard } from "@/lib/srs-types";

/**
 * The next few cards, as pictures. Hovering plays the clip **muted** (a hover
 * is not a user activation, so audio is impossible anyway); clicking opens the
 * card, where it plays with sound.
 */
export function StackRail({ total, className }: { total: number; className?: string }) {
  const { data, isLoading } = useSrsCards({ state: "new", sort: "queue", limit: 8 });
  const cards = data?.items ?? [];

  return (
    <section className={cn("min-w-0", className)}>
      <div className="mb-3 flex items-center justify-between gap-3">
        <h2 className="text-sm font-semibold tracking-tight text-fg">
          Next up in your stack
          <span className="ml-2 text-xs font-normal text-faint">
            {total.toLocaleString()} card{total === 1 ? "" : "s"} waiting
          </span>
        </h2>
        <Link
          to="/study/stack"
          className="inline-flex items-center gap-1 text-xs font-medium text-muted transition-colors hover:text-brand-bright"
        >
          Manage stack
          <ArrowRight className="size-3.5" />
        </Link>
      </div>

      {isLoading && (
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4 xl:grid-cols-8">
          {Array.from({ length: 8 }).map((_, i) => (
            <Skeleton key={i} className="aspect-video w-full rounded-lg" />
          ))}
        </div>
      )}

      {!isLoading && cards.length === 0 && (
        <p className="rounded-xl border border-dashed border-border-strong px-4 py-8 text-center text-sm text-muted">
          The stack is empty — generate more cards or import the curated deck.
        </p>
      )}

      {cards.length > 0 && (
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4 xl:grid-cols-8">
          {cards.map((c) => (
            <RailTile key={c.id} card={c} />
          ))}
        </div>
      )}
    </section>
  );
}

function RailTile({ card }: { card: SrsCard }) {
  const [hover, setHover] = useState(false);
  const ready = card.clip.status === "ready" && !!card.clip.video_url;

  return (
    <Link
      to={`/study/cards/${card.id}`}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      onFocus={() => setHover(true)}
      onBlur={() => setHover(false)}
      className="group block min-w-0 focus-visible:outline-none"
      title={card.meaning_short ?? card.gloss ?? card.lemma}
    >
      <div className="relative aspect-video w-full overflow-hidden rounded-lg border border-border bg-bg-elevated transition-colors group-hover:border-brand/60 group-focus-visible:border-brand">
        {card.clip.poster_url ? (
          <img
            src={card.clip.poster_url}
            alt=""
            loading="lazy"
            className="size-full object-cover"
          />
        ) : (
          <div className="grid size-full place-items-center bg-black/60">
            {card.clip.status === "pending" ? (
              <Scissors className="size-4 animate-pulse text-faint" />
            ) : (
              <Film className="size-4 text-faint" />
            )}
          </div>
        )}
        {hover && ready && (
          <video
            src={card.clip.video_url!}
            poster={card.clip.poster_url ?? undefined}
            muted
            loop
            autoPlay
            playsInline
            className="absolute inset-0 size-full object-cover"
          />
        )}
        {card.study_now && (
          <span className="absolute left-1.5 top-1.5 grid size-5 place-items-center rounded-md bg-brand/90 text-white shadow" title="Study next">
            <Zap className="size-3" />
          </span>
        )}
        {card.queue_pos != null && (
          <span className="absolute right-1.5 top-1.5 rounded-md bg-black/70 px-1.5 py-0.5 text-[0.6rem] font-semibold tabular-nums text-muted backdrop-blur-sm">
            {card.queue_pos}
          </span>
        )}
      </div>
      <p className="mt-1.5 truncate font-jp text-sm font-medium text-fg" lang="ja">
        {card.lemma}
      </p>
      <p className="truncate text-[0.7rem] text-faint">
        {card.meaning_short ?? card.gloss ?? "—"}
      </p>
    </Link>
  );
}
