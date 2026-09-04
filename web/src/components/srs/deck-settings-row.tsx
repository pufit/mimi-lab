import { useEffect, useState } from "react";
import type { ReactNode } from "react";
import { Eye, Minus, Plus, RotateCcw } from "lucide-react";
import { useSaveSrsSettings } from "@/lib/srs-hooks";
import { toast } from "@/components/ui/toast";
import { cn } from "@/lib/utils";
import type { SrsSettings } from "@/lib/srs-types";

const CHIP =
  "inline-flex h-8 min-w-8 items-center justify-center rounded-lg px-2.5 text-xs font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand/70";

function Control({
  label,
  hint,
  icon: Icon,
  children,
}: {
  label: string;
  hint: string;
  icon: typeof Eye;
  children: ReactNode;
}) {
  return (
    <div className="flex min-w-0 flex-1 flex-col gap-2 px-4 py-3">
      <div className="flex items-center gap-1.5">
        <Icon className="size-3.5 text-faint" />
        <p className="text-[0.7rem] font-medium uppercase tracking-wide text-faint">{label}</p>
      </div>
      {children}
      <p className="text-[0.7rem] leading-snug text-faint">{hint}</p>
    </div>
  );
}

/**
 * The only three server settings (design §8.5/§9.5), saved the moment they
 * change. No gear, no dialog: they belong next to the numbers they change.
 */
export function DeckSettingsRow({
  settings,
  className,
}: {
  settings: SrsSettings;
  className?: string;
}) {
  const save = useSaveSrsSettings();
  const [local, setLocal] = useState(settings);
  useEffect(() => setLocal(settings), [settings]);

  const put = (patch: Partial<SrsSettings>, message: string) => {
    setLocal((s) => ({ ...s, ...patch }));
    save.mutate(patch, { onSuccess: () => toast.success("Saved", message) });
  };

  const step = (delta: number) => {
    const next = Math.max(0, Math.min(50, local.new_per_day + delta));
    if (next === local.new_per_day) return;
    put({ new_per_day: next }, `${next} new card${next === 1 ? "" : "s"} a day`);
  };

  return (
    <div
      className={cn(
        "flex flex-col divide-y divide-border rounded-xl border border-border bg-surface/40 sm:flex-row sm:divide-x sm:divide-y-0",
        className,
      )}
    >
      <Control label="New cards per day" hint="How many unseen words enter a session." icon={Plus}>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => step(-1)}
            disabled={local.new_per_day <= 0}
            aria-label="One fewer new card per day"
            className={cn(CHIP, "border border-border-strong bg-surface text-muted hover:text-fg disabled:opacity-40")}
          >
            <Minus className="size-3.5" />
          </button>
          <span className="w-10 text-center text-lg font-semibold tabular-nums text-fg">
            {local.new_per_day}
          </span>
          <button
            type="button"
            onClick={() => step(1)}
            disabled={local.new_per_day >= 50}
            aria-label="One more new card per day"
            className={cn(CHIP, "border border-border-strong bg-surface text-muted hover:text-fg disabled:opacity-40")}
          >
            <Plus className="size-3.5" />
          </button>
        </div>
      </Control>

      <Control
        label="Return to stack after"
        hint="Missing a word this many times puts it back in the stack with a fresh moment."
        icon={RotateCcw}
      >
        <div className="flex flex-wrap items-center gap-1">
          {[0, 1, 2, 3, 4, 5].map((n) => (
            <button
              key={n}
              type="button"
              onClick={() =>
                put(
                  { demote_after_fails: n },
                  n === 0 ? "Words stay in review" : `Return after ${n} miss${n === 1 ? "" : "es"}`,
                )
              }
              aria-pressed={local.demote_after_fails === n}
              className={cn(
                CHIP,
                local.demote_after_fails === n
                  ? "bg-brand/20 text-brand-bright ring-1 ring-brand/50"
                  : "border border-border-strong bg-surface text-muted hover:text-fg",
              )}
            >
              {n === 0 ? "Off" : n}
            </button>
          ))}
        </div>
      </Control>

      <Control
        label="Moments from"
        hint="Only-watched avoids spoilers; any episode gives better sentences."
        icon={Eye}
      >
        <div className="flex flex-wrap items-center gap-1">
          {([
            { v: "any", label: "Any episode" },
            { v: "watched_only", label: "Only watched" },
          ] as const).map((opt) => (
            <button
              key={opt.v}
              type="button"
              onClick={() => put({ moment_source: opt.v }, opt.label)}
              aria-pressed={local.moment_source === opt.v}
              className={cn(
                CHIP,
                local.moment_source === opt.v
                  ? "bg-brand/20 text-brand-bright ring-1 ring-brand/50"
                  : "border border-border-strong bg-surface text-muted hover:text-fg",
              )}
            >
              {opt.label}
            </button>
          ))}
        </div>
      </Control>
    </div>
  );
}
