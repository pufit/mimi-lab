import { memo } from "react";
import type { CSSProperties, DragEvent, KeyboardEvent } from "react";
import { Link } from "react-router-dom";
import { Check, Clock, Film, GripVertical, RotateCcw, Scissors, Zap } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { CardMenu } from "./card-menu";
import { cn } from "@/lib/utils";
import type { SrsCard } from "@/lib/srs-types";

export type StackTab = "stack" | "studying" | "known" | "parked";

/** "due in 3 d" / "due now" — `formatRelative` only speaks about the past. */
export function dueLabel(iso: string | null): string {
  if (!iso) return "not scheduled";
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return iso;
  const ms = t - Date.now();
  if (ms <= 0) return "due now";
  const min = Math.round(ms / 60000);
  if (min < 60) return `due in ${min} min`;
  const hr = Math.round(min / 60);
  if (hr < 36) return `due in ${hr} h`;
  const day = Math.round(hr / 24);
  if (day < 60) return `due in ${day} d`;
  return `due in ${Math.round(day / 30)} mo`;
}

const PARK_REASON: Record<string, string> = {
  user: "you parked it",
  demoted: "returned too often",
  max_demotions: "returned too often",
  migaku_ignored: "ignored in Migaku",
  migaku: "ignored in Migaku",
  rejected: "not worth it",
  not_worth_it: "not worth it",
  no_source: "the episode is gone",
};

/** One chip per row — the rest of the numbers live on the card page. */
function StatusChip({ card, tab }: { card: SrsCard; tab: StackTab }) {
  if (tab === "stack") {
    if (card.study_now)
      return (
        <Badge variant="solid" title="Jumps the queue in your next session">
          <Zap className="size-3" />
          study next
        </Badge>
      );
    if (card.demoted_count > 0)
      return (
        <Badge variant="warning" title="Came back from review after being missed">
          <RotateCcw className="size-3" />
          returned ×{card.demoted_count}
        </Badge>
      );
    if (card.clip.status === "pending")
      return (
        <Badge variant="outline" title="The clip is still being cut">
          <Scissors className="size-3" />
          clip pending
        </Badge>
      );
    if (card.clip.status !== "ready")
      return (
        <Badge variant="outline" title={card.clip.error ?? undefined}>
          <Film className="size-3" />
          {card.clip.status === "no_source" ? "no video" : "no clip"}
        </Badge>
      );
    if (card.buried_until)
      return (
        <Badge variant="outline">
          <Clock className="size-3" />
          skipped today
        </Badge>
      );
    return null;
  }

  if (tab === "studying") {
    if (card.state === "learning" || card.state === "relearning")
      return (
        <Badge variant="warning">
          {card.state === "relearning" ? "relearning" : "learning"}
        </Badge>
      );
    return <Badge variant="default">{dueLabel(card.due_at)}</Badge>;
  }

  if (tab === "known") {
    const src =
      card.known_source === "migaku"
        ? "from Migaku"
        : card.known_source === "user"
          ? "you said so"
          : "learned here";
    return (
      <Badge variant="success" title={card.known_at ? `since ${card.known_at.slice(0, 10)}` : undefined}>
        <Check className="size-3" />
        {src}
      </Badge>
    );
  }

  const reason =
    card.state === "rejected"
      ? "not worth it"
      : card.demoted_count > 0 && card.suspend_reason !== "user"
        ? `returned ${card.demoted_count}×`
        : (card.suspend_reason && PARK_REASON[card.suspend_reason]) ||
          card.suspend_reason ||
          "you parked it";
  return <Badge variant={card.state === "rejected" ? "danger" : "warning"}>{reason}</Badge>;
}

export interface StackRowProps {
  card: SrsCard;
  tab: StackTab;
  /** 0-based position in the list; shown (1-based) on the Stack tab. */
  index: number;
  selected: boolean;
  onSelect: (id: number, shiftKey: boolean) => void;
  /** Drag-and-drop is only wired on the Stack tab, above `md`. */
  dnd?: boolean;
  insert?: "above" | "below" | null;
  dragging?: boolean;
  onDragStart?: (index: number) => void;
  onDragOver?: (index: number, where: "above" | "below") => void;
  onDrop?: (index: number) => void;
  onDragEnd?: () => void;
  /** Alt+↑ / Alt+↓ on a focused row. */
  onNudge?: (index: number, delta: number) => void;
  onSwapMoment?: (card: SrsCard) => void;
  onEdit?: (card: SrsCard) => void;
  onDeleted?: () => void;
  /** Dashed rule under the last "study next" row. */
  separator?: boolean;
}

// Keeps a 1,000-row list cheap: rows outside the viewport are not laid out.
const ROW_STYLE = {
  contentVisibility: "auto",
  containIntrinsicSize: "68px",
} as CSSProperties;

function StackRowInner({
  card,
  tab,
  index,
  selected,
  onSelect,
  dnd = false,
  insert = null,
  dragging = false,
  onDragStart,
  onDragOver,
  onDrop,
  onDragEnd,
  onNudge,
  onSwapMoment,
  onEdit,
  onDeleted,
  separator = false,
}: StackRowProps) {
  const handleDragOver = (e: DragEvent<HTMLDivElement>) => {
    if (!dnd) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    const box = e.currentTarget.getBoundingClientRect();
    onDragOver?.(index, e.clientY < box.top + box.height / 2 ? "above" : "below");
  };

  const handleKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    if (!onNudge || !e.altKey) return;
    if (e.key === "ArrowUp") {
      e.preventDefault();
      onNudge(index, -1);
    } else if (e.key === "ArrowDown") {
      e.preventDefault();
      onNudge(index, 1);
    }
  };

  return (
    <div
      style={ROW_STYLE}
      draggable={dnd}
      onDragStart={(e) => {
        if (!dnd) return;
        e.dataTransfer.effectAllowed = "move";
        e.dataTransfer.setData("text/plain", String(card.id));
        onDragStart?.(index);
      }}
      onDragOver={handleDragOver}
      onDrop={(e) => {
        if (!dnd) return;
        e.preventDefault();
        onDrop?.(index);
      }}
      onDragEnd={onDragEnd}
      onKeyDown={handleKeyDown}
      tabIndex={0}
      className={cn(
        "group relative flex items-center gap-3 border-b border-border/50 px-3 py-2 outline-none transition-colors last:border-0",
        "hover:bg-surface-hover/50 focus-visible:bg-surface-hover/60 focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-brand/60",
        selected && "bg-brand/5",
        dragging && "opacity-40",
        separator && "border-b-2 border-dashed border-brand/30",
      )}
    >
      {insert === "above" && (
        <span className="pointer-events-none absolute inset-x-0 -top-px h-0.5 bg-brand-bright" />
      )}
      {insert === "below" && (
        <span className="pointer-events-none absolute inset-x-0 -bottom-px h-0.5 bg-brand-bright" />
      )}

      <input
        type="checkbox"
        checked={selected}
        onChange={() => undefined}
        onClick={(e) => {
          e.stopPropagation();
          onSelect(card.id, e.shiftKey);
        }}
        aria-label={`Select ${card.lemma}`}
        className="size-4 shrink-0 accent-[var(--color-brand)]"
      />

      {dnd && (
        <GripVertical
          className="size-4 shrink-0 cursor-grab text-faint opacity-40 transition-opacity group-hover:opacity-100"
          aria-hidden
        />
      )}

      {tab === "stack" && (
        <span className="w-8 shrink-0 text-right text-xs tabular-nums text-faint">
          {card.queue_pos ?? index + 1}
        </span>
      )}

      <Link
        to={`/study/cards/${card.id}`}
        className="flex min-w-0 flex-1 items-center gap-3 outline-none"
      >
        <span className="relative hidden aspect-video w-16 shrink-0 overflow-hidden rounded-md border border-border bg-bg-elevated sm:block">
          {card.clip.poster_url ? (
            <img src={card.clip.poster_url} alt="" loading="lazy" className="size-full object-cover" />
          ) : (
            <span className="grid size-full place-items-center">
              <Film className="size-3.5 text-faint" />
            </span>
          )}
        </span>

        <span className="min-w-0 flex-1">
          <span className="flex flex-wrap items-baseline gap-x-2">
            <span className="font-jp text-base font-medium leading-tight text-fg" lang="ja">
              {card.lemma}
            </span>
            {card.reading && card.reading !== card.lemma && (
              <span className="font-jp text-xs text-faint" lang="ja">
                〔{card.reading}〕
              </span>
            )}
          </span>
          <span className="mt-0.5 flex min-w-0 items-center gap-2 text-xs text-muted">
            <span className="min-w-0 truncate">{card.meaning_short ?? card.gloss ?? "—"}</span>
          </span>
        </span>

        <span className="hidden min-w-0 max-w-40 shrink-0 truncate text-[0.7rem] text-faint lg:block">
          {card.show_title ?? ""}
          {card.ep_number != null && ` · E${card.ep_number}`}
        </span>
      </Link>

      <div className="flex shrink-0 items-center gap-2">
        <StatusChip card={card} tab={tab} />
        <CardMenu
          card={card}
          onSwapMoment={onSwapMoment ? () => onSwapMoment(card) : undefined}
          onEdit={onEdit ? () => onEdit(card) : undefined}
          onDeleted={onDeleted}
        />
      </div>
    </div>
  );
}

export const StackRow = memo(StackRowInner);
