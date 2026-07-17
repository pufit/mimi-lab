import { cn } from "@/lib/utils";

/**
 * Renders the backend's `text_furigana` field — HTML ruby markup
 * (`<ruby>漢<rt>かん</rt></ruby>`), already escaped server-side. We only allow
 * ruby/rt/rp tags through; everything else falls back to the plain `text`.
 */
const RUBY_ONLY = /^(?:[^<]|<\/?(?:ruby|rt|rp)>)*$/i;

export function Furigana({
  furigana,
  text,
  className,
}: {
  furigana?: string | null;
  text: string;
  className?: string;
}) {
  const safe = furigana && RUBY_ONLY.test(furigana);
  if (safe) {
    return (
      <span
        className={cn("font-jp", className)}
        // Content is server-escaped ruby markup; validated by RUBY_ONLY above.
        dangerouslySetInnerHTML={{ __html: furigana }}
      />
    );
  }
  return <span className={cn("font-jp", className)}>{text}</span>;
}
