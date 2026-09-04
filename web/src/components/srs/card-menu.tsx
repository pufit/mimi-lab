import { useState } from "react";
import type { ReactNode } from "react";
import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import {
  ArrowDownToLine,
  Check,
  CircleSlash,
  Clock,
  MoreHorizontal,
  Pause,
  Pencil,
  Play,
  RotateCcw,
  Shuffle,
  Trash2,
  Undo2,
  Zap,
} from "lucide-react";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Kbd } from "@/components/ui/kbd";
import { useCardAction, useDeleteCard } from "@/lib/srs-hooks";
import { cn } from "@/lib/utils";
import type { SrsAction, SrsCard } from "@/lib/srs-types";

export interface CardMenuProps {
  card: SrsCard;
  onSwapMoment?: () => void;
  onEdit?: () => void;
  /** Called after a successful hard delete (navigate away, drop the row…). */
  onDeleted?: () => void;
  /**
   * Called after a successful state action taken from this menu. The review
   * session uses it to drop the card from its imperative queue and push its own
   * Undo entry — without it a mouse-path Park/Known only leaves the queue on the
   * next rating's 409.
   */
  onAfterAction?: (card: SrsCard, action: SrsAction) => void;
  /** Bubbles menu/dialog open state so a host page can disarm its hotkeys. */
  onOpenChange?: (open: boolean) => void;
  /**
   * Render the `Kbd` badges. Only the review session actually binds
   * D/B/S/K/M/E, so every other host leaves this off rather than advertising
   * keys that do nothing on its page.
   */
  showHotkeys?: boolean;
  align?: "start" | "end" | "center";
  trigger?: ReactNode;
  className?: string;
}

interface MenuAction {
  key: string;
  label: string;
  icon: typeof Zap;
  hotkey?: string;
  action?: SrsAction;
  onSelect?: () => void;
  danger?: boolean;
  /** Renders a separator above this item. */
  group?: boolean;
}

const ITEM_CLASS =
  "flex cursor-pointer select-none items-center gap-2.5 rounded-lg px-2.5 py-1.5 text-sm text-muted outline-none transition-colors data-[highlighted]:bg-surface-hover data-[highlighted]:text-fg";

/** The §4.2 action list for one card, filtered to what its state allows. */
export function cardMenuActions(
  card: SrsCard,
  handlers: { onSwapMoment?: () => void; onEdit?: () => void; onDelete?: () => void },
): MenuAction[] {
  const inStack = card.state === "new";
  const studying =
    card.state === "learning" || card.state === "review" || card.state === "relearning";
  const items: MenuAction[] = [];

  if (inStack) {
    items.push({ key: "study_next", label: "Study next", icon: Zap, action: "study_next" });
    items.push({
      key: "bottom",
      label: "Send to the bottom",
      icon: ArrowDownToLine,
      hotkey: "D",
      action: "bottom",
    });
  }
  if (studying || card.state === "new") {
    items.push({
      key: "bury",
      label: "Skip for today",
      icon: Clock,
      hotkey: "B",
      action: "bury",
      group: true,
    });
  }
  if (card.buried_until) {
    items.push({ key: "unbury", label: "Un-skip", icon: Undo2, action: "unbury", group: true });
  }
  if (card.state === "suspended") {
    items.push({ key: "resume", label: "Put back in play", icon: Play, action: "resume", group: true });
  } else if (card.state !== "rejected") {
    items.push({
      key: "suspend",
      label: "Park it",
      icon: Pause,
      hotkey: "S",
      action: "suspend",
      group: true,
    });
  }
  if (card.state === "rejected") {
    items.push({ key: "restore", label: "Back to the stack", icon: RotateCcw, action: "restore" });
  } else {
    items.push({ key: "reject", label: "Not worth it", icon: CircleSlash, action: "reject" });
  }
  if (card.state === "known") {
    items.push({
      key: "unknown",
      label: "Not known after all",
      icon: Undo2,
      action: "unknown",
      group: true,
    });
  } else {
    items.push({
      key: "known",
      label: "I already know it",
      icon: Check,
      hotkey: "K",
      action: "known",
      group: true,
    });
  }
  if (studying) {
    items.push({ key: "forget", label: "Reset progress", icon: RotateCcw, action: "forget" });
  }
  if (handlers.onSwapMoment) {
    items.push({
      key: "swap",
      label: "Swap the moment",
      icon: Shuffle,
      hotkey: "M",
      onSelect: handlers.onSwapMoment,
      group: true,
    });
  }
  if (handlers.onEdit) {
    items.push({ key: "edit", label: "Edit card", icon: Pencil, hotkey: "E", onSelect: handlers.onEdit });
  }
  if (handlers.onDelete) {
    items.push({
      key: "delete",
      label: "Delete card",
      icon: Trash2,
      onSelect: handlers.onDelete,
      danger: true,
      group: true,
    });
  }
  return items;
}

/**
 * Kebab menu with every §4.2 action the card's state allows. Actions go
 * straight to `POST /srs/cards/{id}/action`; edit / swap / delete are handed
 * back to the host page (which owns those dialogs).
 */
export function CardMenu({
  card,
  onSwapMoment,
  onEdit,
  onDeleted,
  onAfterAction,
  onOpenChange,
  showHotkeys = false,
  align = "end",
  trigger,
  className,
}: CardMenuProps) {
  const [confirmDelete, setConfirmDelete] = useState(false);
  const act = useCardAction();
  const del = useDeleteCard();

  const items = cardMenuActions(card, {
    onSwapMoment,
    onEdit,
    onDelete: () => setConfirmDelete(true),
  });

  const setDeleteOpen = (open: boolean) => {
    setConfirmDelete(open);
    onOpenChange?.(open);
  };

  return (
    <>
      <DropdownMenu.Root onOpenChange={onOpenChange}>
        <DropdownMenu.Trigger asChild>
          {trigger ?? (
            <button
              type="button"
              aria-label={`Actions for ${card.lemma}`}
              onClick={(e) => e.stopPropagation()}
              className={cn(
                "grid size-8 shrink-0 place-items-center rounded-lg text-faint transition-colors hover:bg-surface-hover hover:text-fg focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand/70",
                className,
              )}
            >
              <MoreHorizontal className="size-4" />
            </button>
          )}
        </DropdownMenu.Trigger>
        <DropdownMenu.Portal>
          <DropdownMenu.Content
            align={align}
            sideOffset={6}
            onClick={(e) => e.stopPropagation()}
            // Radix's default is to hand focus back to the trigger `<button>`.
            // The review session's global key handler ignores events targeting a
            // button, so one use of this menu would kill Space/1-4/R/Z for the
            // rest of the session. The host re-focuses its own container from
            // `onOpenChange(false)`.
            onCloseAutoFocus={(e) => e.preventDefault()}
            className="z-50 w-60 rounded-xl border border-border bg-bg-elevated/95 p-1.5 shadow-2xl shadow-black/60 backdrop-blur-xl"
          >
            {items.map((it) => (
              <div key={it.key}>
                {it.group && <DropdownMenu.Separator className="my-1 h-px bg-border" />}
                <DropdownMenu.Item
                  className={cn(ITEM_CLASS, it.danger && "text-comp-red data-[highlighted]:text-comp-red")}
                  onSelect={() => {
                    if (it.onSelect) it.onSelect();
                    else if (it.action) {
                      const action = it.action;
                      act.mutate(
                        { id: card.id, action },
                        { onSuccess: () => onAfterAction?.(card, action) },
                      );
                    }
                  }}
                >
                  <it.icon className="size-4 shrink-0 opacity-80" />
                  <span className="flex-1 truncate">{it.label}</span>
                  {showHotkeys && it.hotkey && <Kbd>{it.hotkey}</Kbd>}
                </DropdownMenu.Item>
              </div>
            ))}
          </DropdownMenu.Content>
        </DropdownMenu.Portal>
      </DropdownMenu.Root>

      <Dialog open={confirmDelete} onOpenChange={setDeleteOpen}>
        <DialogContent className="w-[min(28rem,calc(100vw-2rem))]">
          <DialogHeader>
            <DialogTitle>Delete {card.lemma}?</DialogTitle>
            <DialogDescription>
              The card, its clip and its review history are removed for good. The word goes back
              into the candidate pool — it can be generated again later. To keep the history but
              stop seeing the card, use <span className="text-fg">Park it</span> instead.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="ghost" onClick={() => setDeleteOpen(false)}>
              Cancel
            </Button>
            <Button
              variant="danger"
              loading={del.isPending}
              onClick={() =>
                del.mutate(card.id, {
                  onSuccess: () => {
                    setDeleteOpen(false);
                    onDeleted?.();
                  },
                })
              }
            >
              <Trash2 className="size-4" />
              Delete for good
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
