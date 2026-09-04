// The sticky rating bar (design §8.3.4).
//
// Four grades are the FSRS contract: 1 Again · 2 Hard · 3 Good · 4 Easy, with
// the server's interval previews on the buttons. `ratingMode === "two"` hides
// Hard/Easy (the FSRS math is unchanged; the server never hears about it).
// When this Again would reach `demote_after_fails`, the button carries the
// honest consequence label — this is the moment the information is actionable.

import { Kbd } from "@/components/ui/kbd";
import { cn } from "@/lib/utils";
import type { SrsIntervalPreview, SrsRating } from "@/lib/srs-types";

export interface RatingBarProps {
  preview: SrsIntervalPreview | null | undefined;
  ratingMode: "four" | "two";
  /** Mutation in flight, or the 200 ms arm delay has not elapsed. */
  disabled: boolean;
  /** `fail_count + 1 >= demote_after_fails` (and the rule is on). */
  againWarns: boolean;
  /** The grade whose button is flashing after a click/keypress. */
  flash: SrsRating | null;
  onRate: (rating: SrsRating) => void;
  className?: string;
}

interface Grade {
  rating: SrsRating;
  label: string;
  key: string;
  interval: string;
  className: string;
  wide?: boolean;
}

const BASE =
  "relative flex min-w-0 flex-1 flex-col items-center justify-center gap-0.5 rounded-xl border px-2 py-2.5 text-sm font-semibold transition-all duration-150 active:scale-[0.98] disabled:pointer-events-none disabled:opacity-40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand/70";

export function RatingBar({
  preview,
  ratingMode,
  disabled,
  againWarns,
  flash,
  onRate,
  className,
}: RatingBarProps) {
  const grades: Grade[] = [
    {
      rating: 1,
      label: againWarns ? "Again → back to the stack" : "Again",
      key: "1",
      interval: preview?.again ?? "",
      className:
        "border-comp-red/40 bg-comp-red/15 text-comp-red hover:bg-comp-red/25 hover:border-comp-red/60",
    },
    {
      rating: 2,
      label: "Hard",
      key: "2",
      interval: preview?.hard ?? "",
      className:
        "border-comp-amber/35 bg-comp-amber/10 text-comp-amber hover:bg-comp-amber/20 hover:border-comp-amber/55",
    },
    {
      rating: 3,
      label: "Good",
      key: "3",
      interval: preview?.good ?? "",
      className:
        "border-transparent bg-brand text-white shadow-lg shadow-brand/25 hover:bg-brand-bright",
      wide: true,
    },
    {
      rating: 4,
      label: "Easy",
      key: "4",
      interval: preview?.easy ?? "",
      className:
        "border-comp-green/35 bg-comp-green/10 text-comp-green hover:bg-comp-green/20 hover:border-comp-green/55",
    },
  ];
  const shown = ratingMode === "two" ? grades.filter((g) => g.rating === 1 || g.rating === 3) : grades;

  return (
    <div
      className={cn(
        "sticky bottom-0 z-20 border-t border-border/70 bg-bg/90 px-3 pt-2.5 backdrop-blur-md",
        "pb-[max(0.625rem,env(safe-area-inset-bottom))]",
        className,
      )}
    >
      <div className="mx-auto flex max-w-4xl items-stretch gap-2">
        {shown.map((g) => (
          <button
            key={g.rating}
            type="button"
            disabled={disabled}
            onClick={() => onRate(g.rating)}
            className={cn(
              BASE,
              g.className,
              g.wide && "flex-[1.6]",
              flash === g.rating && "rating-flash",
            )}
            aria-keyshortcuts={g.rating === 3 ? "3 Enter" : g.key}
          >
            <span className="flex items-center gap-1.5 text-center leading-tight">
              <Kbd className="border-transparent bg-black/25 text-current">{g.key}</Kbd>
              <span className="truncate">{g.label}</span>
            </span>
            {g.interval && (
              <span className="text-[0.7rem] font-medium opacity-80 tabular-nums">{g.interval}</span>
            )}
          </button>
        ))}
      </div>
      <p className="mt-1.5 text-center text-[0.65rem] text-faint">
        Enter · Good{ratingMode === "two" ? "" : " · 1–4 grade"} · Space · pause / play · Z undo · ? shortcuts
      </p>
    </div>
  );
}
