// The neighbouring lines the meaning leans on (evidence amendment §2).
//
// When `why_clear` explains the word with something said *around* the target
// sentence, that dialogue is part of the moment: the clip covers it and the
// card front shows it. Japanese only, dimmed and one size down — it is scene,
// not the sentence being recalled, and its translation stays behind the reveal
// with the rest of the answer.

import { stripBidi } from "@/components/srs/token-line";
import { cn } from "@/lib/utils";
import type { SrsLineRef } from "@/lib/srs-types";

export interface EvidenceLinesProps {
  lines: SrsLineRef[];
  /** Which side of the target these sit on — only affects the connector rule. */
  side: "before" | "after";
  className?: string;
}

export function EvidenceLines({ lines, side, className }: EvidenceLinesProps) {
  if (lines.length === 0) return null;
  return (
    <div
      className={cn("space-y-0.5 border-l border-border/60 pl-2.5", className)}
      aria-label={side === "before" ? "Lines before this one" : "Lines after this one"}
    >
      {lines.map((line, i) => (
        <p
          key={line.line_id ?? `${line.idx ?? i}-${line.start_ms}`}
          className="font-jp text-sm leading-relaxed text-faint"
          lang="ja"
        >
          {stripBidi(line.text)}
        </p>
      ))}
    </div>
  );
}
