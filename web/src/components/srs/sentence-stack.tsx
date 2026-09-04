// The sentences of the moment, stacked in the order they are heard, with the
// one that is playing lit up (clip navigation, 2026-09-03).
//
// Replaces the evidence-before / target / evidence-after trio on the card:
// every sentence is a row with a gutter mark. The row whose audio is playing
// gets a brand rail (three breathing bars while the media runs), a faint tint
// and full-contrast text; the other evidence rows stay dimmed and one size down
// — they are scene, not the sentence being recalled. Rows are clickable when a
// clock is attached: a click plays from that sentence.
//
// A single-cue moment renders the target line alone, exactly as before — there
// is nothing to navigate.

import type { MouseEvent } from "react";
import { TokenLine, stripBidi } from "@/components/srs/token-line";
import type { FuriganaMode } from "@/components/srs/token-line";
import { cn } from "@/lib/utils";
import type { Sentence } from "@/lib/srs-sentences";
import type { SrsCard } from "@/lib/srs-types";

export interface SentenceStackProps {
  card: Pick<SrsCard, "tokens" | "text" | "target_surface" | "text_furigana">;
  sentences: Sentence[];
  /** Index of the sentence playing; `-1` = no clock (no clip, no stream). */
  activeIndex: number;
  /** The media is running — animates the active row's gutter. */
  playing: boolean;
  furigana: FuriganaMode;
  /** Size of the target line (`text-xl` / `text-2xl`). */
  targetClassName?: string;
  /** Detail page: tint known/unknown tokens of the target line. */
  showStatus?: boolean;
  /** Play from a sentence (row click). Absent → rows are inert. */
  onSeek?: (index: number) => void;
  className?: string;
}

export function SentenceStack({
  card,
  sentences,
  activeIndex,
  playing,
  furigana,
  targetClassName,
  showStatus = false,
  onSeek,
  className,
}: SentenceStackProps) {
  const target = (
    <TokenLine
      tokens={card.tokens}
      text={card.text}
      target_surface={card.target_surface}
      furigana={furigana}
      furiganaHtml={card.text_furigana}
      showStatus={showStatus}
      className={targetClassName}
    />
  );

  if (sentences.length < 2) return <div className={className}>{target}</div>;

  const interactive = !!onSeek;

  return (
    <div
      className={cn("flex min-w-0 flex-col gap-0.5", className)}
      aria-label="Sentences of the moment"
    >
      {sentences.map((s, i) => {
        const active = i === activeIndex;
        const isTarget = s.role === "target";
        const onClick = interactive
          ? (e: MouseEvent) => {
              // On the front the whole card reveals on click — a sentence click
              // is a seek, not a reveal.
              e.stopPropagation();
              onSeek(i);
            }
          : undefined;
        return (
          <div
            key={s.key}
            role={interactive ? "button" : undefined}
            tabIndex={interactive ? -1 : undefined}
            onClick={onClick}
            aria-current={active ? "true" : undefined}
            title={
              interactive
                ? active
                  ? "Replay this sentence (↓)"
                  : "Play from this sentence"
                : undefined
            }
            className={cn(
              "group relative -mx-2 flex items-stretch gap-2.5 rounded-lg px-2 py-1 transition-colors duration-200",
              interactive && "cursor-pointer hover:bg-surface/70",
              active && "bg-brand/[0.07]",
            )}
          >
            <Gutter active={active} playing={active && playing} />
            <div className="min-w-0 flex-1">
              {isTarget ? (
                target
              ) : (
                <p
                  className={cn(
                    "font-jp text-sm leading-relaxed transition-colors duration-200",
                    active ? "text-fg/90" : "text-faint",
                  )}
                  lang="ja"
                >
                  {stripBidi(s.text)}
                </p>
              )}
            </div>
          </div>
        );
      })}
    </div>
  );
}

/** The mark before a sentence: a thin rail; brand when active; bars while live. */
function Gutter({ active, playing }: { active: boolean; playing: boolean }) {
  return (
    <span className="flex w-3 shrink-0 items-center justify-center" aria-hidden>
      {playing ? (
        <span className="flex h-3.5 items-end gap-px">
          <i className="eq-bar block h-full w-[3px] rounded-full bg-brand-bright" />
          <i className="eq-bar block h-full w-[3px] rounded-full bg-brand-bright" />
          <i className="eq-bar block h-full w-[3px] rounded-full bg-brand-bright" />
        </span>
      ) : (
        <span
          className={cn(
            "block h-full min-h-4 w-0.5 rounded-full transition-colors duration-200",
            active ? "bg-brand" : "bg-border/70",
          )}
        />
      )}
    </span>
  );
}
