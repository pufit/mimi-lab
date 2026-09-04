import { Furigana } from "@/components/furigana";
import { cn } from "@/lib/utils";
import type { SrsToken } from "@/lib/srs-types";

/**
 * Invisible bidi controls (U+200E/U+200F, U+202A–U+202E, U+2066–U+2069) wrap
 * some subtitle cues (a real subtitle cue). They break substring matching
 * and highlight offsets, so strip them before comparing or rendering
 * (amendments §B2.1).
 */
const BIDI_RE = /[\u200E\u200F\u202A-\u202E\u2066-\u2069]/g;

export function stripBidi(s: string): string {
  return s.replace(BIDI_RE, "");
}

export type FuriganaMode = "none" | "target" | "all";

export interface TokenLineProps {
  tokens: SrsToken[];
  text: string;
  target_surface?: string | null;
  furigana?: FuriganaMode;
  /** Server ruby markup (`card.text_furigana`) — used only in the no-token fallback. */
  furiganaHtml?: string | null;
  /** Tint KNOWN/IGNORED vs UNKNOWN tokens (card detail; off during review). */
  showStatus?: boolean;
  className?: string;
}

const TARGET_CLASS =
  "text-brand-bright font-semibold underline decoration-2 decoration-brand/60 underline-offset-6";

/**
 * The Japanese line of a card.
 *
 * With tokens it renders one `<span>` per token, readings as `<ruby><rt>`
 * according to `furigana`, and the target token highlighted. Without tokens it
 * falls back to `<Furigana>` (server ruby) or a plain bidi-stripped substring
 * highlight of `target_surface`.
 */
export function TokenLine({
  tokens,
  text,
  target_surface,
  furigana = "none",
  furiganaHtml,
  showStatus = false,
  className,
}: TokenLineProps) {
  const base = cn("font-jp text-lg leading-relaxed text-fg [&_rt]:text-[0.5em] [&_rt]:text-faint", className);

  if (tokens.length > 0) {
    return (
      <p className={base} lang="ja">
        {tokens.map((t, i) => {
          const surface = stripBidi(t.surface);
          if (!surface) return null;
          const showRuby =
            !!t.reading &&
            t.reading !== surface &&
            (furigana === "all" || (furigana === "target" && t.is_target));
          return (
            <span
              key={`${i}-${surface}`}
              className={cn(
                t.is_target
                  ? TARGET_CLASS
                  : showStatus && t.status === "UNKNOWN"
                    ? "text-fg"
                    : showStatus
                      ? "text-muted"
                      : undefined,
              )}
              title={showStatus && !t.is_target ? t.status.toLowerCase() : undefined}
            >
              {showRuby ? (
                <ruby>
                  {surface}
                  <rt>{t.reading}</rt>
                </ruby>
              ) : (
                surface
              )}
            </span>
          );
        })}
      </p>
    );
  }

  const clean = stripBidi(text);
  const needle = target_surface ? stripBidi(target_surface) : "";
  const at = needle ? clean.indexOf(needle) : -1;

  // No tokens and no place to highlight → the server's ruby markup is strictly
  // better than plain text when furigana is wanted.
  if (at < 0) {
    if (furigana !== "none" && furiganaHtml) {
      return (
        <p className={base} lang="ja">
          <Furigana furigana={furiganaHtml} text={clean} />
        </p>
      );
    }
    return (
      <p className={base} lang="ja">
        {clean}
      </p>
    );
  }

  return (
    <p className={base} lang="ja">
      {clean.slice(0, at)}
      <span className={TARGET_CLASS}>{needle}</span>
      {clean.slice(at + needle.length)}
    </p>
  );
}
