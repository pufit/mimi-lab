// A new card is still a lesson — but it is no longer a freebie (design §3.3 /
// §8.3.2, revised 2026-09-03).
//
// It used to open fully revealed. It no longer does: every card, first exposure
// included, shows the sentence and its scene with the answer blurred, and the
// learner reveals it (the user: "on the card I don't want to see the answer right
// away"). What stays special about `state === "new"` is what happens *after*
// the reveal — two buttons, "Got it" (rating 3 → learning step 1, +10 m) and
// "Easy — skip ahead" (rating 4 → review, 8 d). Again/Hard are still not
// offered: there is nothing to recall yet, and an Again here would set
// D0 = 6.41 for mere unfamiliarity. Server side both are ordinary reviews.
//
// The surface itself is `ReviewCard lesson`; only the action bar lives here.

import { Button } from "@/components/ui/button";
import { Kbd } from "@/components/ui/kbd";
import { cn } from "@/lib/utils";

export interface LessonActionsProps {
  disabled: boolean;
  /** Rating 3 — the lesson's "I've read it, quiz me in 10 minutes". */
  onGotIt: () => void;
  /** Rating 4 — straight to review with I(S0(4)) = 8 d. */
  onAlreadyKnow: () => void;
  className?: string;
}

export function LessonActions({
  disabled,
  onGotIt,
  onAlreadyKnow,
  className,
}: LessonActionsProps) {
  return (
    <div
      className={cn(
        "sticky bottom-0 z-20 border-t border-border/70 bg-bg/90 px-3 pt-2.5 backdrop-blur-md",
        "pb-[max(0.625rem,env(safe-area-inset-bottom))]",
        className,
      )}
    >
      <div className="mx-auto flex max-w-4xl items-stretch gap-2">
        <Button
          variant="primary"
          size="lg"
          className="flex-[1.6]"
          disabled={disabled}
          onClick={onGotIt}
        >
          Got it
          <Kbd className="border-transparent bg-black/25 text-current">Enter</Kbd>
        </Button>
        <Button variant="ghost" size="lg" className="flex-1" disabled={disabled} onClick={onAlreadyKnow}>
          Easy — skip ahead
          <Kbd>4</Kbd>
        </Button>
      </div>
      <p className="mt-1.5 text-center text-[0.65rem] text-faint">
        First exposure — read it, then continue. K removes the word from the deck.
      </p>
    </div>
  );
}
