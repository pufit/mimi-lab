// The card surface of a review session (design §8.3.3/§8.3.4).
//
// Only the *text column* lives here: the clip is mounted once by the page (one
// persistent <video> for the whole session, §8.3 audio contract), so this
// component may re-render per card without ever touching the media element.
//
// The front carries exactly one state cue — the kind badge (`KindBadge`: new
// word / learning / relearning / review), so a review is never mistaken for a
// new card mid-session (the user, 2026-09-03) — and no fail dots: "one miss from
// demotion" on the front biases the learner toward Hard instead of Again and
// corrupts both the FSRS input and the demotion rule. `↩ ×n` is a stack/detail
// affordance.

import { useEffect, useState } from "react";
import type { ReactNode, Ref } from "react";
import {
  Film,
  GraduationCap,
  Pencil,
  Play,
  Repeat,
  RotateCcw,
  Sparkles,
  Volume2,
} from "lucide-react";
import { ClipPlayer, clipSource } from "@/components/srs/clip-player";
import type { ClipPlayerHandle } from "@/components/srs/clip-player";
import { SentenceStack } from "@/components/srs/sentence-stack";
import { SentenceTransport } from "@/components/srs/sentence-transport";
import { WordChip } from "@/components/srs/word-chip";
import { ContextLines } from "@/components/srs/context-lines";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Kbd } from "@/components/ui/kbd";
import { formatDuration, momentDurationMs } from "@/lib/srs-extend";
import { cn, formatMs } from "@/lib/utils";
import type { Sentence } from "@/lib/srs-sentences";
import type { SrsCard } from "@/lib/srs-types";
import type { ClipElement } from "@/components/srs/use-clip-element";
import type { SentencePlayback } from "@/components/srs/use-sentence-playback";

export type FuriganaMode = "none" | "target" | "all";

// ── Clip stage (mounted once per session by the page) ───────

export interface ClipStageProps {
  card: SrsCard;
  clip: ClipElement;
  onWatchScene?: () => void;
  /**
   * Handle on the *fallback* player (episode stream, used while the clip is
   * still cutting) so R can replay that too, not just the cut clip.
   */
  fallbackRef?: Ref<ClipPlayerHandle>;
  /**
   * Clip navigation: the moment's sentences and the clock over them. Draws the
   * ←/→/↓ strip under the frame when the moment has more than one sentence.
   */
  sentences?: Sentence[];
  nav?: SentencePlayback;
  className?: string;
}

/**
 * The dark letterboxed stage. When the clip is `ready` the session's persistent
 * `<video>` is (re-)appended into the mount box; every other status falls back
 * to WP-C's `ClipPlayer`, which renders the poster or a dark frame.
 */
export function ClipStage({
  card,
  clip,
  onWatchScene,
  fallbackRef,
  sentences,
  nav,
  className,
}: ClipStageProps) {
  const ready = card.clip.status === "ready" && !!card.clip.video_url;
  const extended = ready && card.extend.length > 0;

  // A click on the picture pauses / resumes. The glyph that marks a pause waits
  // a beat: a card switch pauses-then-plays within a few ms and must not blink.
  const pausedByUser = ready && clip.paused && !clip.ended && !clip.needsGesture;
  const [showPaused, setShowPaused] = useState(false);
  useEffect(() => {
    if (!pausedByUser) {
      setShowPaused(false);
      return;
    }
    const t = setTimeout(() => setShowPaused(true), 150);
    return () => clearTimeout(t);
  }, [pausedByUser]);

  return (
    <div className={cn("relative w-full min-w-0", className)}>
      <div
        className={cn(
          "relative overflow-hidden rounded-2xl border border-border bg-black",
          !ready && "hidden",
        )}
      >
        <div
          ref={clip.mount}
          onClick={clip.togglePlay}
          role="button"
          tabIndex={-1}
          aria-label={
            showPaused ? "Resume the clip" : clip.ended ? "Play the clip again" : "Pause the clip"
          }
          title={showPaused ? "Resume" : clip.ended ? "Play again" : "Pause"}
          className="min-h-[10rem] w-full cursor-pointer"
        />

        {showPaused && (
          <div className="pointer-events-none absolute inset-0 grid place-items-center">
            <span className="grid size-12 place-items-center rounded-full border border-white/15 bg-black/55 text-white/90 shadow-lg backdrop-blur-[2px]">
              <Play className="size-5 translate-x-0.5" />
            </span>
          </div>
        )}

        {clip.needsGesture && (
          <button
            type="button"
            onClick={() => clip.replay()}
            className="absolute inset-0 grid place-items-center bg-black/55 backdrop-blur-[2px]"
            aria-label="Play the clip"
          >
            <span className="grid size-14 place-items-center rounded-full border border-white/20 bg-black/60">
              <Play className="size-6 translate-x-0.5 text-white" />
            </span>
          </button>
        )}

        {!clip.needsGesture && clip.ended && (
          <button
            type="button"
            onClick={() => clip.replay()}
            className="absolute bottom-2 right-2 grid size-9 place-items-center rounded-full border border-white/15 bg-black/55 text-white/80 transition-colors hover:text-white"
            aria-label="Replay the clip (R)"
            title="Replay (R)"
          >
            <RotateCcw className="size-4" />
          </button>
        )}

        {clip.audioOnly && (
          <div className="pointer-events-none absolute inset-0 grid place-items-center">
            <span className="flex items-center gap-2 rounded-lg bg-black/70 px-3 py-1.5 text-xs text-white/80">
              <Volume2 className="size-4" /> audio only
            </span>
          </div>
        )}

        {/* An extended moment plays for noticeably longer than one cue — say so,
            so a long clip does not read as a stuck player. */}
        {extended && (
          <span className="pointer-events-none absolute bottom-2 left-2 rounded-md bg-black/60 px-2 py-1 text-[0.65rem] font-medium tabular-nums text-white/75 backdrop-blur-sm">
            <ClipDuration card={card} />
          </span>
        )}
      </div>

      {/* No clip file yet (or it failed) → stream the episode itself, pre-seeked
          to the *moment window* (evidence lines included), stopping at its end. */}
      {!ready && (
        <ClipPlayer
          ref={fallbackRef}
          clip={card.clip}
          source={clipSource(card)}
          controls
          className="max-h-[42vh] rounded-2xl"
        />
      )}

      {sentences && nav && (
        <SentenceTransport
          sentences={sentences}
          activeIndex={nav.activeIndex}
          canNext={nav.canNext}
          onPrev={nav.prev}
          onNext={nav.next}
          onReplay={nav.replay}
          onSeek={nav.seekTo}
          className="mt-2 px-0.5"
        />
      )}

      {!ready && card.source_available && onWatchScene && (
        <div className="mt-2 flex justify-center">
          <Button variant="secondary" size="sm" onClick={onWatchScene}>
            <Film className="size-3.5" />
            Watch scene
            <Kbd>W</Kbd>
          </Button>
        </div>
      )}
    </div>
  );
}

// ── Meta row ────────────────────────────────────────────────

export function MetaRow({
  card,
  prefix,
  className,
}: {
  card: SrsCard;
  prefix?: string;
  className?: string;
}) {
  const bits = [
    prefix,
    card.show_title ?? undefined,
    card.ep_number != null ? `E${card.ep_number}` : undefined,
    formatMs(card.start_ms),
  ].filter(Boolean) as string[];
  return (
    <p className={cn("text-xs text-faint", className)}>{bits.join(" · ")}</p>
  );
}

// ── Kind badge (why this card is here) ──────────────────────

/**
 * The one state cue the front carries: a first exposure, a learning step coming
 * back, or a scheduled review. Colours follow the stack page — brand for new,
 * amber for learning, green for review — so a review can never be mistaken for
 * a new card mid-session (the user, 2026-09-03). Never the fail count: that would
 * bias the rating.
 */
export function KindBadge({ card, className }: { card: SrsCard; className?: string }) {
  switch (card.state) {
    case "new":
      return (
        <Badge variant="solid" className={className} title="First exposure">
          <Sparkles className="size-3" />
          New word
        </Badge>
      );
    case "learning":
    case "relearning":
      return (
        <Badge
          variant="warning"
          className={className}
          title={
            card.state === "relearning"
              ? "Missed in review — learning it again"
              : "A learning step coming back"
          }
        >
          <GraduationCap className="size-3" />
          {card.state === "relearning" ? "Relearning" : "Learning"}
        </Badge>
      );
    case "review":
      return (
        <Badge variant="success" className={className} title="A scheduled review">
          <Repeat className="size-3" />
          Review
        </Badge>
      );
    default:
      return null;
  }
}

// ── Word block (the back's headline) ────────────────────────

export function WordBlock({
  card,
  onEdit,
  className,
}: {
  card: SrsCard;
  onEdit?: () => void;
  className?: string;
}) {
  const [expanded, setExpanded] = useState(false);
  const full = card.meaning_full ?? card.gloss;

  return (
    <div
      className={cn(
        "rounded-2xl border border-border-strong bg-surface/50 p-4",
        className,
      )}
    >
      <div className="flex items-start gap-3">
        <div className="min-w-0 flex-1">
          <WordChip card={card} size="md" showRank={false} />

          {card.meaning_short && (
            <p className="mt-1.5 text-lg font-medium leading-snug text-fg">{card.meaning_short}</p>
          )}

          {full && (
            <button
              type="button"
              onClick={() => setExpanded((v) => !v)}
              className={cn(
                "mt-1 w-full text-left text-sm leading-relaxed text-muted transition-colors hover:text-fg",
                !expanded && "truncate",
              )}
              title={expanded ? "Collapse" : "Expand"}
            >
              {full}
            </button>
          )}
        </div>

        <div className="flex shrink-0 flex-col items-end gap-1.5">
          {onEdit && (
            <Button variant="ghost" size="icon-sm" onClick={onEdit} title="Edit card (E)">
              <Pencil className="size-3.5" />
            </Button>
          )}
          {card.freq_rank != null && (
            <Badge variant="outline" className="tabular-nums">
              #{card.freq_rank.toLocaleString()}
            </Badge>
          )}
        </div>
      </div>

      {card.why_clear && (
        <p className="mt-3 border-t border-border/60 pt-2.5 text-xs italic leading-relaxed text-muted">
          Why it&rsquo;s clear: {card.why_clear}
        </p>
      )}
      {card.usage_note && (
        <p className="mt-1.5 text-xs leading-relaxed text-faint">{card.usage_note}</p>
      )}
    </div>
  );
}

// ── Translation ─────────────────────────────────────────────

export function TranslationLine({ card, className }: { card: SrsCard; className?: string }) {
  if (!card.translation) return null;
  return (
    <div className={cn("animate-fade-in", className)}>
      <p className="text-base leading-relaxed text-muted">{card.translation}</p>
      {card.translation_source === "mt" && (
        <Badge variant="outline" className="mt-1.5">
          machine translation
        </Badge>
      )}
    </div>
  );
}

// ── The card surface ────────────────────────────────────────

export interface ReviewCardProps {
  card: SrsCard;
  phase: "front" | "back";
  furigana: FuriganaMode;
  /** Back only: T toggles the sentence translation off and on. */
  showTranslation: boolean;
  contextOpen: boolean;
  onContextOpenChange: (open: boolean) => void;
  onReveal: () => void;
  onWatchScene?: () => void;
  onPlayExtended?: () => void;
  onEdit?: () => void;
  /**
   * Clip navigation: the moment's sentences in playback order, which one the
   * clip is on (`-1` = no clock), whether it is running, and the seek a row
   * click performs (absent → rows are inert).
   */
  sentences: Sentence[];
  activeSentence: number;
  playing: boolean;
  onSeekSentence?: (index: number) => void;
  className?: string;
}

/**
 * The card surface.
 *
 * Front: the target sentence with its evidence lines — every sentence of the
 * moment as a row, the one the clip is playing lit up (`SentenceStack`). Back:
 * the same, plus the answer. The answer panel is *always* mounted and simply
 * blurred until the reveal — including on a brand-new card, which used to open
 * with everything legible (the user, 2026-09-03: "on the card I don't want to see
 * the answer right away"). A new card only differs in its label and in the
 * buttons the page puts under it once the blur lifts.
 */
export function ReviewCard({
  card,
  phase,
  furigana,
  showTranslation,
  contextOpen,
  onContextOpenChange,
  onReveal,
  onWatchScene,
  onPlayExtended,
  onEdit,
  sentences,
  activeSentence,
  playing,
  onSeekSentence,
  className,
}: ReviewCardProps) {
  const back = phase === "back";
  // No clip → the sentence carries the card, so it gets more room.
  const big = card.clip.status !== "ready";
  const activeLineId = activeSentence >= 0 ? (sentences[activeSentence]?.line_id ?? null) : null;

  return (
    // Tap/click anywhere on the front reveals (§8.3.4, mobile §8.10).
    <div
      className={cn("flex min-h-0 flex-col gap-4", !back && "cursor-pointer select-none", className)}
      onClick={back ? undefined : onReveal}
    >
      {/* Why this card is here — new word / learning step / review — then where
          the moment comes from. Same row for every kind, so the eye learns one
          place to look. */}
      <div className="flex items-center gap-2.5">
        <KindBadge card={card} className="px-2.5 py-1 text-xs" />
        <MetaRow card={card} />
      </div>

      <SentenceStack
        card={card}
        sentences={sentences}
        activeIndex={activeSentence}
        playing={playing}
        furigana={back ? "all" : furigana}
        targetClassName={big ? "text-2xl" : "text-xl"}
        onSeek={onSeekSentence}
      />

      <AnswerPanel revealed={back} onReveal={onReveal}>
        <WordBlock card={card} onEdit={back ? onEdit : undefined} />
        {showTranslation && <TranslationLine card={card} />}
        <ContextLines
          lines={card.context}
          open={contextOpen}
          onOpenChange={onContextOpenChange}
          onWatchScene={onWatchScene}
          onPlayExtended={onPlayExtended}
          sourceAvailable={card.source_available}
          hasExtend={card.extend.length > 0}
          activeLineId={activeLineId}
        />
      </AnswerPanel>
    </div>
  );
}

// ── The blur ────────────────────────────────────────────────

export interface AnswerPanelProps {
  revealed: boolean;
  onReveal: () => void;
  children: ReactNode;
  className?: string;
}

/**
 * Everything that answers the card, hidden behind a blur until the learner asks
 * for it.
 *
 * The content is mounted either way — swapping it in on reveal would reflow the
 * page under the rating bar and make the crossfade jump — but while it is
 * blurred it takes no clicks, no selection and no focus (`inert`), so Tab and a
 * stray tap cannot pull an answer out of it. The blur transition lives in
 * `index.css` behind `prefers-reduced-motion: no-preference`.
 */
export function AnswerPanel({ revealed, onReveal, children, className }: AnswerPanelProps) {
  return (
    <div className={cn("relative", className)}>
      <div
        className={cn(
          "answer-veil flex flex-col gap-4",
          !revealed && "pointer-events-none select-none blur-[7px]",
        )}
        // `inert` also hides the subtree from the accessibility tree, which is
        // what we want: a screen reader must not read the answer out either.
        inert={!revealed}
      >
        {children}
      </div>

      {!revealed && (
        <button
          type="button"
          onClick={onReveal}
          className="absolute inset-0 grid place-items-center rounded-2xl focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand/60"
          aria-label="Reveal the answer"
        >
          <span className="flex items-center gap-2 rounded-full border border-border-strong bg-bg/85 px-3.5 py-1.5 text-xs font-medium text-muted shadow-sm backdrop-blur-sm">
            <Kbd>Enter</Kbd>
            reveal
          </span>
        </button>
      )}
    </div>
  );
}

// ── Clip length ─────────────────────────────────────────────

/** "4.2s" — how much of the scene the moment covers (evidence widens it). */
export function ClipDuration({ card, className }: { card: SrsCard; className?: string }) {
  const ms = momentDurationMs(card);
  if (ms == null) return null;
  return <span className={className}>{formatDuration(ms)}</span>;
}
