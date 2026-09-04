import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import type { SrsCard } from "@/lib/srs-types";

export interface WordChipProps {
  card: Pick<SrsCard, "lemma" | "reading" | "pos" | "freq_rank" | "gloss" | "meaning_short">;
  size?: "sm" | "md" | "lg";
  /** Show the frequency-rank badge (off in dense lists). */
  showRank?: boolean;
  /** Show the one-line meaning under the word. */
  showMeaning?: boolean;
  className?: string;
}

const SIZE: Record<NonNullable<WordChipProps["size"]>, string> = {
  sm: "text-base",
  md: "text-2xl",
  lg: "text-4xl md:text-5xl",
};

/**
 * The word itself: lemma with its reading above it, part of speech and
 * frequency rank beside it. Used on the card front, in card detail and as the
 * lead of a stack row.
 */
export function WordChip({
  card,
  size = "md",
  showRank = true,
  showMeaning = false,
  className,
}: WordChipProps) {
  const reading = card.reading && card.reading !== card.lemma ? card.reading : null;
  const meaning = card.meaning_short || card.gloss;
  return (
    <div className={cn("min-w-0", className)}>
      <div className="flex flex-wrap items-baseline gap-x-2.5 gap-y-1">
        <span className={cn("font-jp font-semibold leading-tight text-fg", SIZE[size])} lang="ja">
          {reading ? (
            <ruby>
              {card.lemma}
              <rt className="text-[0.4em] font-normal text-brand-bright">{reading}</rt>
            </ruby>
          ) : (
            card.lemma
          )}
        </span>
        {card.pos && (
          <span className="text-[0.7rem] font-medium uppercase tracking-wide text-faint">
            {card.pos}
          </span>
        )}
        {showRank && card.freq_rank != null && (
          <Badge variant="outline" className="tabular-nums" title="Frequency rank (JPDB)">
            #{card.freq_rank.toLocaleString()}
          </Badge>
        )}
      </div>
      {showMeaning && meaning && (
        <p className="mt-1 text-sm leading-relaxed text-muted">{meaning}</p>
      )}
    </div>
  );
}
