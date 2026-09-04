// The `?` sheet: the §8.4 keyboard map plus the two presentation prefs.
//
// While this dialog is mounted the page passes `enabled={false}` to
// `useHotkeys` — Radix closes on Escape without stopping propagation, so a
// window-level Esc would otherwise also leave the session.

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Kbd } from "@/components/ui/kbd";
import { cn } from "@/lib/utils";
import type { SrsPrefs } from "@/lib/srs-types";

const ROWS: { keys: string[]; what: string }[] = [
  { keys: ["Enter"], what: "Reveal the answer · then Good (Got it on a new card)" },
  { keys: ["Space"], what: "Pause / resume the clip (a finished clip starts over)" },
  { keys: ["1", "2", "3", "4"], what: "Again · Hard · Good · Easy (3/4 on a new card)" },
  { keys: ["←", "→", "↓"], what: "The clip: previous sentence · next sentence · replay this sentence" },
  { keys: ["R", "A"], what: "Replay the whole clip · replay audio only" },
  { keys: ["F", "T", "C"], what: "Furigana cycle · translation on/off · scene context" },
  { keys: ["W", "X"], what: "Watch the scene · play the whole moment" },
  { keys: ["K", "S", "B"], what: "Mark known · suspend · bury (skip today) — each undoable with Z" },
  { keys: ["D"], what: "Not now — send this new card to the bottom of the stack (undoable with Z)" },
  { keys: ["E", "M"], what: "Edit the card · swap the moment" },
  { keys: ["Z"], what: "Undo the last action, else the last rating" },
  { keys: ["?", "Esc"], what: "This sheet · leave the session" },
];

export interface ShortcutsSheetProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  prefs: SrsPrefs;
  setPrefs: (patch: Partial<SrsPrefs>) => void;
}

function Toggle({
  label,
  options,
  value,
  onChange,
}: {
  label: string;
  options: { value: string; label: string }[];
  value: string;
  onChange: (v: string) => void;
}) {
  return (
    <div className="flex items-center justify-between gap-3">
      <span className="text-sm text-muted">{label}</span>
      <div className="flex rounded-lg border border-border-strong bg-surface p-0.5">
        {options.map((o) => (
          <button
            key={o.value}
            type="button"
            onClick={() => onChange(o.value)}
            className={cn(
              "rounded-md px-2.5 py-1 text-xs font-medium transition-colors",
              o.value === value ? "bg-brand text-white" : "text-muted hover:text-fg",
            )}
          >
            {o.label}
          </button>
        ))}
      </div>
    </div>
  );
}

export function ShortcutsSheet({ open, onOpenChange, prefs, setPrefs }: ShortcutsSheetProps) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="w-[min(34rem,calc(100vw-2rem))]">
        <DialogHeader>
          <DialogTitle>Review shortcuts</DialogTitle>
          <DialogDescription>
            Every card, new ones included, opens with the answer blurred. Rating keys arm 200 ms
            after the reveal, and a held key never counts twice.
          </DialogDescription>
        </DialogHeader>

        <div className="divide-y divide-border/60">
          {ROWS.map((row) => (
            <div key={row.what} className="flex items-start gap-3 py-2">
              <div className="flex w-32 shrink-0 flex-wrap gap-1">
                {row.keys.map((k) => (
                  <Kbd key={k}>{k}</Kbd>
                ))}
              </div>
              <p className="text-sm leading-relaxed text-muted">{row.what}</p>
            </div>
          ))}
        </div>

        <div className="mt-5 space-y-3 rounded-xl border border-border bg-surface/40 p-3">
          <Toggle
            label="Rating buttons"
            value={prefs.ratingMode}
            options={[
              { value: "four", label: "Four" },
              { value: "two", label: "Again / Good" },
            ]}
            onChange={(v) => setPrefs({ ratingMode: v === "two" ? "two" : "four" })}
          />
          <Toggle
            label="Furigana on the front"
            value={prefs.frontFurigana}
            options={[
              { value: "none", label: "None" },
              { value: "target", label: "Target" },
              { value: "all", label: "All" },
            ]}
            onChange={(v) =>
              setPrefs({
                frontFurigana: v === "target" || v === "all" ? v : "none",
              })
            }
          />
        </div>
      </DialogContent>
    </Dialog>
  );
}
