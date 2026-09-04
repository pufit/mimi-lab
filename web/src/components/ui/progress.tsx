import { cn } from "@/lib/utils";

export interface ProgressProps {
  /** Current value; clamped to [0, max]. Ignored when `indeterminate`. */
  value?: number | null;
  max?: number;
  indeterminate?: boolean;
  /** Bar colour — any CSS colour or `var(--color-…)` token. Defaults to brand. */
  accent?: string;
  className?: string;
  label?: string;
}

/** Thin brand bar. No Radix dependency — a div pair is the whole widget. */
export function Progress({
  value,
  max = 100,
  indeterminate = false,
  accent,
  className,
  label,
}: ProgressProps) {
  const safeMax = max > 0 ? max : 100;
  const v = Math.max(0, Math.min(safeMax, value ?? 0));
  const pct = indeterminate ? 100 : (v / safeMax) * 100;
  return (
    <div
      role="progressbar"
      aria-label={label}
      aria-valuemin={0}
      aria-valuemax={safeMax}
      aria-valuenow={indeterminate ? undefined : v}
      className={cn("h-1.5 w-full overflow-hidden rounded-full bg-surface", className)}
    >
      <div
        className={cn(
          "h-full rounded-full transition-[width] duration-300 ease-out",
          indeterminate && "animate-pulse",
        )}
        style={{
          width: `${pct}%`,
          backgroundColor: accent ?? "var(--color-brand-bright)",
        }}
      />
    </div>
  );
}
