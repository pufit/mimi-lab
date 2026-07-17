import { cn, compColor, compPillStyle, formatPct } from "@/lib/utils";

/**
 * Signature comprehension % pill — colored red→amber→lime→green.
 *
 * `variant="tint"` (default) is a translucent tinted chip for use on solid
 * (dark) surfaces. `variant="overlay"` sits on top of poster artwork: it keeps
 * the tier color on the text/dot but backs it with an opaque dark scrim so the
 * label stays legible even over bright areas of the image.
 */
export function ComprehensionPill({
  pct,
  className,
  size = "md",
  showLabel = false,
  variant = "tint",
}: {
  pct?: number | null;
  className?: string;
  size?: "sm" | "md";
  showLabel?: boolean;
  variant?: "tint" | "overlay";
}) {
  const overlay = variant === "overlay";
  const sizeCls = size === "sm" ? "px-1.5 py-0.5 text-[0.65rem]" : "px-2 py-1 text-xs";

  if (pct == null) {
    return (
      <span
        className={cn(
          "inline-flex items-center gap-1 rounded-md border font-semibold backdrop-blur-md",
          overlay
            ? "border-white/20 bg-black/65 text-white/75"
            : "border-border-strong bg-bg-elevated/80 text-faint",
          sizeCls,
          className,
        )}
        title="Comprehension not computed yet"
      >
        — %
      </span>
    );
  }
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded-md border font-semibold tabular-nums backdrop-blur-md",
        overlay ? "border-white/20 bg-black/65 shadow-sm shadow-black/40" : "",
        sizeCls,
        className,
      )}
      style={overlay ? { color: compColor(pct) } : compPillStyle(pct)}
      title={`${formatPct(pct)} comprehension`}
    >
      <span
        className="size-1.5 rounded-full"
        style={{ backgroundColor: compColor(pct) }}
      />
      {formatPct(pct)}
      {showLabel && <span className="font-normal opacity-70">known</span>}
    </span>
  );
}

/** A thin horizontal comprehension bar. */
export function ComprehensionBar({
  pct,
  className,
}: {
  pct?: number | null;
  className?: string;
}) {
  const value = pct ?? 0;
  return (
    <div className={cn("h-1.5 w-full overflow-hidden rounded-full bg-border", className)}>
      <div
        className="h-full rounded-full transition-all duration-500"
        style={{
          width: `${Math.max(2, Math.min(100, value))}%`,
          backgroundColor: pct == null ? "var(--color-faint)" : compColor(value),
        }}
      />
    </div>
  );
}
