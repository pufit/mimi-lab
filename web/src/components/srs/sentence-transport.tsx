// The strip under the clip: where you are in the moment, and the three keys
// that move you (clip navigation, 2026-09-03).
//
//   [←]  ▬▬▬▬ ▬▬▬▬▬▬▬▬▬▬▬▬ ▬▬▬▬  [→]   [↓]  2 / 3
//
// One block per sentence, width ∝ duration; the block that is playing is solid
// brand, the target sentence is tinted so its place in the moment is visible
// even when another line is playing, evidence lines are neutral. The three
// keycaps ARE the buttons — clicking ← does what pressing ← does — so the keys
// need no separate legend. Hidden for single-cue moments.

import { Kbd } from "@/components/ui/kbd";
import { stripBidi } from "@/components/srs/token-line";
import { cn } from "@/lib/utils";
import type { Sentence } from "@/lib/srs-sentences";

export interface SentenceTransportProps {
  sentences: Sentence[];
  activeIndex: number;
  canNext: boolean;
  onPrev: () => void;
  onNext: () => void;
  onReplay: () => void;
  onSeek: (index: number) => void;
  className?: string;
}

/** A block never collapses below this, however short the cue. */
const MIN_BLOCK_MS = 600;

export function SentenceTransport({
  sentences,
  activeIndex,
  canNext,
  onPrev,
  onNext,
  onReplay,
  onSeek,
  className,
}: SentenceTransportProps) {
  const n = sentences.length;
  if (n < 2) return null;

  return (
    <div className={cn("flex items-center gap-1.5", className)} aria-label="Clip navigation">
      <KeyButton label="Previous sentence" shortcut="ArrowLeft" onClick={onPrev}>
        ←
      </KeyButton>

      <div className="flex h-7 min-w-0 flex-1 items-center gap-1 px-0.5" role="list">
        {sentences.map((s, i) => {
          const active = i === activeIndex;
          const isTarget = s.role === "target";
          return (
            <button
              key={s.key}
              type="button"
              role="listitem"
              onClick={() => onSeek(i)}
              aria-current={active ? "true" : undefined}
              aria-label={`Sentence ${i + 1} of ${n}${isTarget ? " (the card's sentence)" : ""}`}
              title={`${i + 1} / ${n} · ${stripBidi(s.text)}`}
              style={{ flexGrow: Math.max(s.end_ms - s.start_ms, MIN_BLOCK_MS) }}
              className="group relative h-7 min-w-5 basis-0 rounded-md focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand/60"
            >
              <span
                className={cn(
                  "absolute inset-x-0 top-1/2 h-1.5 -translate-y-1/2 rounded-full transition-colors duration-200",
                  active
                    ? "bg-brand shadow-[0_0_10px_var(--color-brand)]"
                    : isTarget
                      ? "bg-brand/35 group-hover:bg-brand/55"
                      : "bg-border-strong group-hover:bg-faint",
                )}
              />
            </button>
          );
        })}
      </div>

      <KeyButton label="Next sentence" shortcut="ArrowRight" onClick={onNext} disabled={!canNext}>
        →
      </KeyButton>

      <span className="mx-0.5 h-4 w-px bg-border" aria-hidden />

      <KeyButton label="Replay this sentence" shortcut="ArrowDown" onClick={onReplay}>
        ↓
      </KeyButton>

      <span className="min-w-[2.75rem] text-right text-xs tabular-nums text-faint">
        {activeIndex >= 0 ? activeIndex + 1 : "–"} / {n}
      </span>
    </div>
  );
}

function KeyButton({
  label,
  shortcut,
  onClick,
  disabled = false,
  children,
}: {
  label: string;
  shortcut: string;
  onClick: () => void;
  disabled?: boolean;
  children: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      aria-label={label}
      aria-keyshortcuts={shortcut}
      title={`${label} (${children})`}
      className="group rounded-[0.4rem] transition-transform focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand/60 active:scale-95 disabled:pointer-events-none disabled:opacity-35"
    >
      <Kbd className="h-7 min-w-7 text-sm text-fg transition-colors group-hover:border-faint group-hover:bg-surface-hover">
        {children}
      </Kbd>
    </button>
  );
}
