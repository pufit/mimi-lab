import { useEffect, useMemo, useState } from "react";
import { Save } from "lucide-react";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { usePatchCard } from "@/lib/srs-hooks";
import { cn } from "@/lib/utils";
import { stripBidi } from "./token-line";
import type { SrsCard, SrsPatchCard } from "@/lib/srs-types";

export interface EditCardDialogProps {
  card: SrsCard;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /**
   * The saved card. Query consumers refresh through the cache, but the review
   * session holds its queue imperatively and would otherwise keep showing the
   * pre-edit copy until the next refill.
   */
  onSaved?: (card: SrsCard) => void;
}

type Field = keyof SrsPatchCard;

const FIELDS: { key: Field; label: string; hint?: string; multiline?: boolean; jp?: boolean }[] = [
  { key: "reading", label: "Reading", jp: true },
  { key: "meaning_short", label: "Meaning (short)", hint: "The one line you see on the back" },
  { key: "meaning_full", label: "Meaning (full)", multiline: true },
  { key: "gloss", label: "Dictionary gloss", multiline: true },
  { key: "target_surface", label: "Highlighted word", hint: "Must appear in the sentence", jp: true },
  { key: "translation", label: "Translation of the line", multiline: true },
  { key: "usage_note", label: "Usage note", multiline: true },
  { key: "notes", label: "Your notes", multiline: true },
];

const LABEL = "mb-1 block text-[0.7rem] font-medium uppercase tracking-wide text-faint";
const AREA =
  "w-full rounded-lg border border-border-strong bg-bg-elevated px-3.5 py-2 text-sm text-fg shadow-sm transition-colors placeholder:text-faint focus-visible:border-brand focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand/40";

/** Only the §7.2 PATCH fields — nothing here touches scheduling. */
export function EditCardDialog({ card, open, onOpenChange, onSaved }: EditCardDialogProps) {
  const patch = usePatchCard();
  const initial = useMemo<Record<Field, string>>(
    () => ({
      reading: card.reading ?? "",
      gloss: card.gloss ?? "",
      meaning_short: card.meaning_short ?? "",
      meaning_full: card.meaning_full ?? "",
      usage_note: card.usage_note ?? "",
      notes: card.notes ?? "",
      target_surface: card.target_surface ?? "",
      translation: card.translation ?? "",
    }),
    [card],
  );
  const [values, setValues] = useState(initial);
  useEffect(() => {
    if (open) setValues(initial);
  }, [open, initial]);

  const cleanText = stripBidi(card.text);
  const surface = stripBidi(values.target_surface ?? "");
  const surfaceBad = surface.length > 0 && !cleanText.includes(surface);
  const dirty = (Object.keys(initial) as Field[]).some((k) => values[k] !== initial[k]);

  const save = () => {
    if (surfaceBad) return;
    // Same value type for every key, but a union-keyed write confuses TS —
    // build a plain record and hand it over as the patch body.
    const body: Record<string, string | null> = {};
    for (const k of Object.keys(initial) as Field[]) {
      if (values[k] !== initial[k]) body[k] = values[k].trim() === "" ? null : values[k];
    }
    if (Object.keys(body).length === 0) {
      onOpenChange(false);
      return;
    }
    patch.mutate(
      { id: card.id, patch: body as SrsPatchCard },
      {
        onSuccess: (res) => {
          onSaved?.(res.card);
          onOpenChange(false);
        },
      },
    );
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      {/* The form is taller than a 900 px viewport, so the *body* scrolls and
          the Cancel/Save row stays a flex child pinned at the bottom — with the
          default single scroll box the footer sat below the fold on load. */}
      <DialogContent className="flex w-[min(42rem,calc(100vw-2rem))] flex-col overflow-y-hidden">
        <DialogHeader className="shrink-0">
          <DialogTitle>
            Edit <span className="font-jp text-brand-bright">{card.lemma}</span>
          </DialogTitle>
          <DialogDescription>
            Wording only — scheduling, state and the clip are untouched. Empty a field to clear it.
          </DialogDescription>
        </DialogHeader>

        <div className="-mx-1 min-h-0 flex-1 overflow-y-auto px-1">
        <div className="rounded-xl border border-border bg-surface/40 px-3 py-2.5">
          <p className="mb-1 text-[0.7rem] font-medium uppercase tracking-wide text-faint">
            The sentence
          </p>
          <p className="font-jp text-sm leading-relaxed text-fg" lang="ja">
            {cleanText}
          </p>
        </div>

        <div className="mt-4 grid gap-4 sm:grid-cols-2">
          {FIELDS.map((f) => {
            const isSurface = f.key === "target_surface";
            return (
              <div key={f.key} className={cn(f.multiline && "sm:col-span-2")}>
                <label className={LABEL} htmlFor={`edit-${f.key}`}>
                  {f.label}
                </label>
                {f.multiline ? (
                  <textarea
                    id={`edit-${f.key}`}
                    rows={2}
                    value={values[f.key] ?? ""}
                    onChange={(e) => setValues((v) => ({ ...v, [f.key]: e.target.value }))}
                    className={cn(AREA, f.jp && "font-jp")}
                  />
                ) : (
                  <Input
                    id={`edit-${f.key}`}
                    value={values[f.key] ?? ""}
                    onChange={(e) => setValues((v) => ({ ...v, [f.key]: e.target.value }))}
                    className={cn(f.jp && "font-jp", isSurface && surfaceBad && "border-comp-red")}
                  />
                )}
                {isSurface && surfaceBad ? (
                  <p className="mt-1 text-[0.7rem] text-comp-red">
                    “{surface}” isn&apos;t in the sentence above.
                  </p>
                ) : (
                  f.hint && <p className="mt-1 text-[0.7rem] text-faint">{f.hint}</p>
                )}
              </div>
            );
          })}
        </div>
        </div>

        <DialogFooter className="shrink-0">
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button
            variant="primary"
            onClick={save}
            loading={patch.isPending}
            disabled={surfaceBad || !dirty}
          >
            <Save className="size-4" />
            Save changes
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
