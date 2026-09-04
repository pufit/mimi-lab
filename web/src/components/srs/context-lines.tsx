// Collapsed scene context on the back of a card (design §8.3.4).
//
// The surrounding dialogue with translations, the target line highlighted, plus
// the two playback affordances that need the episode file on disk.

import { ChevronRight, Film, StepForward } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Kbd } from "@/components/ui/kbd";
import { cn, formatMs } from "@/lib/utils";
import type { SrsContextLine } from "@/lib/srs-types";

export interface ContextLinesProps {
  lines: SrsContextLine[];
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** W — the episode plays from this line in `MomentPlayer`. */
  onWatchScene?: () => void;
  /** X — same, but through the end of the continuation lines. */
  onPlayExtended?: () => void;
  /** Episode video on disk right now. */
  sourceAvailable: boolean;
  hasExtend: boolean;
  /** The line whose audio is playing in the clip right now (ringed). */
  activeLineId?: number | null;
  className?: string;
}

export function ContextLines({
  lines,
  open,
  onOpenChange,
  onWatchScene,
  onPlayExtended,
  sourceAvailable,
  hasExtend,
  activeLineId = null,
  className,
}: ContextLinesProps) {
  const hasLines = lines.length > 0;
  if (!hasLines && !sourceAvailable) return null;

  return (
    <div className={cn("rounded-xl border border-border/70 bg-surface/30", className)}>
      <button
        type="button"
        onClick={() => onOpenChange(!open)}
        className="flex w-full items-center gap-2 px-3 py-2 text-left text-xs font-medium text-muted transition-colors hover:text-fg"
        aria-expanded={open}
      >
        <ChevronRight className={cn("size-3.5 transition-transform", open && "rotate-90")} />
        Scene context
        <Kbd className="ml-auto">C</Kbd>
      </button>

      {open && (
        <div className="space-y-2 border-t border-border/70 px-3 py-2.5">
          {hasLines ? (
            lines.map((line, i) => (
              <div
                key={line.line_id ?? `${line.idx}-${i}`}
                className={cn(
                  "rounded-lg px-2 py-1.5 transition-shadow",
                  line.is_target
                    ? "border border-brand/40 bg-brand/10"
                    : "border border-transparent",
                  activeLineId != null &&
                    line.line_id === activeLineId &&
                    "ring-1 ring-brand/50",
                )}
              >
                <div className="flex items-baseline gap-2">
                  <span className="shrink-0 font-mono text-[0.65rem] text-faint tabular-nums">
                    {formatMs(line.start_ms)}
                  </span>
                  <p
                    className={cn(
                      "font-jp text-sm leading-relaxed",
                      line.is_target ? "text-fg" : "text-muted",
                    )}
                  >
                    {line.text}
                  </p>
                </div>
                {line.translation && (
                  <p className="mt-0.5 pl-[3.25rem] text-xs leading-relaxed text-faint">
                    {line.translation}
                  </p>
                )}
              </div>
            ))
          ) : (
            <p className="px-2 py-1 text-xs text-faint">No surrounding lines were captured.</p>
          )}

          {sourceAvailable && (
            <div className="flex flex-wrap items-center gap-2 pt-1">
              {onWatchScene && (
                <Button variant="secondary" size="sm" onClick={onWatchScene}>
                  <Film className="size-3.5" />
                  Watch scene
                  <Kbd>W</Kbd>
                </Button>
              )}
              {hasExtend && onPlayExtended && (
                <Button variant="ghost" size="sm" onClick={onPlayExtended}>
                  <StepForward className="size-3.5" />
                  Play extended
                  <Kbd>X</Kbd>
                </Button>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
