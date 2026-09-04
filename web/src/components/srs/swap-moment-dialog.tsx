import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { ChevronDown, Play, Search, Sparkles, ThumbsDown } from "lucide-react";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { MomentPlayer } from "@/components/moment-player";
import { EvidenceLines } from "@/components/srs/evidence-lines";
import { useFindMoments, useSrsCard, useSwapMoment } from "@/lib/srs-hooks";
import { formatDuration, momentDurationMs, splitEvidence } from "@/lib/srs-extend";
import { cn } from "@/lib/utils";
import { stripBidi } from "./token-line";
import type { Moment } from "@/lib/types";
import type { SrsCard, SrsLineRef, SrsMoment } from "@/lib/srs-types";

export interface SwapMomentDialogProps {
  card: SrsCard;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Pass the already-loaded `SrsCardDetail.moments` to skip a second fetch. */
  moments?: SrsMoment[];
  /** The card after the swap — lets an imperative holder (the review session's
   * queue) show the new moment instead of the pre-swap copy. */
  onSaved?: (card: SrsCard) => void;
}

function momentToPlayable(m: SrsMoment, card: SrsCard): Moment {
  return {
    line_id: m.line_id ?? 0,
    anilist_id: card.anilist_id ?? 0,
    episode_id: m.episode_id,
    title: m.show_title,
    ep_number: m.ep_number,
    text: m.text,
    text_furigana: m.text_furigana,
    translation: m.translation,
    // the moment's whole span (same-cue half + evidence lines), unpadded — the
    // player adds its own lead/tail
    start_ms: m.window_start_ms ?? m.start_ms,
    end_ms: m.window_end_ms ?? m.end_ms,
  };
}

/** Evidence lines of a moment split around its own line (like `splitEvidence`
 * does for a card): `idx` when both sides carry one, else time. */
function momentEvidence(m: SrsMoment): { before: SrsLineRef[]; after: SrsLineRef[] } {
  const ev = (m.extend ?? []).filter((l) => l.role === "evidence");
  const isBefore = (l: SrsLineRef) =>
    l.idx != null && m.idx != null ? l.idx < m.idx : l.start_ms < m.start_ms;
  const byTime = (a: SrsLineRef, b: SrsLineRef) => a.start_ms - b.start_ms || (a.idx ?? 0) - (b.idx ?? 0);
  return {
    before: ev.filter(isBefore).sort(byTime),
    after: ev.filter((l) => !isBefore(l)).sort(byTime),
  };
}

/**
 * "This sentence doesn't show the word well" → pick a different one.
 * Accepted moments first; rejected ones are collapsed with the judge's note so
 * the reason a moment was dropped is visible but not in the way.
 */
export function SwapMomentDialog({
  card, open, onOpenChange, moments, onSaved,
}: SwapMomentDialogProps) {
  const detail = useSrsCard(open && !moments ? card.id : 0);
  const swap = useSwapMoment();
  const find = useFindMoments();
  const [showRejected, setShowRejected] = useState(false);
  const [preview, setPreview] = useState<Moment | null>(null);

  const all = moments ?? detail.data?.moments ?? [];
  const loading = !moments && detail.isLoading;
  const evidence = useMemo(() => splitEvidence(card), [card]);
  const clipMs = momentDurationMs(card);

  const { accepted, rejected, usedByCards } = useMemo(() => {
    // a moment another card of this word already shows is not a choice — it
    // is that card (multi-card words); listed separately with a link
    const others = all.filter((m) => !m.is_primary && m.used_by_card_id == null);
    return {
      accepted: others.filter((m) => m.accepted !== false),
      rejected: others.filter((m) => m.accepted === false),
      usedByCards: all.filter((m) => !m.is_primary && m.used_by_card_id != null),
    };
  }, [all]);

  const use = (m: SrsMoment) =>
    swap.mutate(
      { id: card.id, body: { moment_id: m.id } },
      {
        onSuccess: (res) => {
          onSaved?.(res.card);
          onOpenChange(false);
        },
      },
    );

  return (
    <>
      <Dialog open={open} onOpenChange={onOpenChange}>
        <DialogContent className="w-[min(46rem,calc(100vw-2rem))]">
          <DialogHeader>
            <DialogTitle>
              Another moment for <span className="font-jp text-brand-bright">{card.lemma}</span>
            </DialogTitle>
            <DialogDescription>
              Pick the line that makes the meaning unmistakable. A new clip is cut in the
              background; the old one is kept until the new one is ready.
            </DialogDescription>
          </DialogHeader>

          <div className="mb-3 rounded-xl border border-brand/30 bg-brand/5 p-3">
            <p className="mb-1 flex items-baseline gap-2 text-[0.7rem] font-medium uppercase tracking-wide text-faint">
              Current moment
              {clipMs != null && (
                <span className="normal-case tracking-normal">{formatDuration(clipMs)}</span>
              )}
            </p>
            {/* The lines the meaning leans on are part of this moment — a swap
                replaces them too, so they belong in the "before" picture. */}
            <EvidenceLines lines={evidence.before} side="before" className="mb-1" />
            <p className="font-jp text-sm leading-relaxed text-fg" lang="ja">
              {stripBidi(card.text)}
            </p>
            <EvidenceLines lines={evidence.after} side="after" className="mt-1" />
            {card.translation && (
              <p className="mt-1 text-xs leading-relaxed text-muted">{card.translation}</p>
            )}
          </div>

          {loading && (
            <div className="space-y-2">
              {Array.from({ length: 3 }).map((_, i) => (
                <Skeleton key={i} className="h-20 w-full rounded-xl" />
              ))}
            </div>
          )}

          {!loading && accepted.length === 0 && rejected.length === 0 && (
            <p className="rounded-xl border border-dashed border-border-strong px-4 py-8 text-center text-sm text-muted">
              No other moment has been found for this word yet.
            </p>
          )}

          {usedByCards.length > 0 && (
            <div className="mb-3 space-y-1">
              <p className="text-[0.7rem] font-medium uppercase tracking-wide text-faint">
                Already cards for this word
              </p>
              {usedByCards.map((m) => (
                <Link
                  key={m.id}
                  to={`/study/cards/${m.used_by_card_id}`}
                  className="flex items-baseline gap-2 rounded-lg border border-border/60 px-3 py-1.5 text-sm transition-colors hover:border-border-strong"
                >
                  <span className="min-w-0 truncate font-jp text-fg" lang="ja">
                    {stripBidi(m.text)}
                  </span>
                  <span className="ml-auto shrink-0 text-[0.7rem] text-faint">
                    {m.show_title}
                    {m.ep_number != null ? ` · E${m.ep_number}` : ""} · card #{m.used_by_card_id}
                    {m.used_by_state === "rejected" ? " (rejected)" : ""}
                  </span>
                </Link>
              ))}
            </div>
          )}

          <div className="space-y-2">
            {accepted.map((m) => (
              <MomentOption
                key={m.id}
                moment={m}
                busy={swap.isPending}
                onUse={() => use(m)}
                onPreview={() => setPreview(momentToPlayable(m, card))}
              />
            ))}
          </div>

          {rejected.length > 0 && (
            <div className="mt-3">
              <button
                type="button"
                onClick={() => setShowRejected((v) => !v)}
                className="flex items-center gap-1.5 text-xs font-medium text-faint transition-colors hover:text-fg"
              >
                <ChevronDown className={cn("size-3.5 transition-transform", showRejected && "rotate-180")} />
                {rejected.length} moment{rejected.length === 1 ? "" : "s"} the judge turned down
              </button>
              {showRejected && (
                <div className="mt-2 space-y-2 opacity-80">
                  {rejected.map((m) => (
                    <MomentOption
                      key={m.id}
                      moment={m}
                      busy={swap.isPending}
                      onUse={() => use(m)}
                      onPreview={() => setPreview(momentToPlayable(m, card))}
                    />
                  ))}
                </div>
              )}
            </div>
          )}

          <div className="mt-4 flex items-center justify-between gap-2 border-t border-border pt-3">
            <p className="text-xs text-faint">
              Looking at {all.length} known moment{all.length === 1 ? "" : "s"} for this word.
            </p>
            <Button
              variant="secondary"
              size="sm"
              loading={find.isPending}
              onClick={() => find.mutate(card.id)}
            >
              <Search className="size-3.5" />
              Find more moments
            </Button>
          </div>
        </DialogContent>
      </Dialog>

      {preview && <MomentPlayer moment={preview} onClose={() => setPreview(null)} />}
    </>
  );
}

function MomentOption({
  moment,
  busy,
  onUse,
  onPreview,
}: {
  moment: SrsMoment;
  busy: boolean;
  onUse: () => void;
  onPreview: () => void;
}) {
  const ev = momentEvidence(moment);
  const spanMs =
    moment.window_start_ms != null && moment.window_end_ms != null
      ? moment.window_end_ms - moment.window_start_ms
      : null;
  return (
    <div className="rounded-xl border border-border bg-surface/50 p-3 transition-colors hover:border-border-strong">
      <div className="flex items-start gap-3">
        <div className="min-w-0 flex-1">
          {/* the same picture as the current moment: the lines this moment
              would carry, dimmed around its sentence */}
          <EvidenceLines lines={ev.before} side="before" className="mb-1" />
          <p className="font-jp text-sm leading-relaxed text-fg" lang="ja">
            {stripBidi(moment.text)}
          </p>
          <EvidenceLines lines={ev.after} side="after" className="mt-1" />
          {moment.translation && (
            <p className="mt-1 text-xs leading-relaxed text-muted">{moment.translation}</p>
          )}
          <div className="mt-1.5 flex flex-wrap items-center gap-1.5 text-[0.7rem] text-faint">
            {moment.show_title && <span className="truncate">{moment.show_title}</span>}
            {moment.ep_number != null && <span>· E{moment.ep_number}</span>}
            {spanMs != null && ev.before.length + ev.after.length > 0 && (
              <span title="Clip span with its context lines">· {formatDuration(spanMs)}</span>
            )}
            {moment.clarity != null && (
              <Badge
                variant={moment.clarity >= 0.75 ? "success" : "warning"}
                title="How clearly this line shows the word, judged 0–1"
              >
                <Sparkles className="size-3" />
                {moment.clarity.toFixed(2)}
              </Badge>
            )}
            {moment.other_unknowns != null && moment.other_unknowns > 0 && (
              <Badge variant="outline">+{moment.other_unknowns} unknown</Badge>
            )}
            {moment.accepted === false && (
              <Badge variant="danger">
                <ThumbsDown className="size-3" />
                turned down
              </Badge>
            )}
            {moment.translation_source === "mt" && <Badge variant="outline">machine translation</Badge>}
          </div>
          {moment.note && (
            <p className="mt-1.5 text-[0.7rem] leading-snug text-faint">“{moment.note}”</p>
          )}
        </div>
        <div className="flex shrink-0 flex-col gap-1.5">
          <Button variant="primary" size="sm" onClick={onUse} disabled={busy}>
            Use this
          </Button>
          <Button
            variant="ghost"
            size="sm"
            onClick={onPreview}
            disabled={!moment.has_video}
            title={moment.has_video ? "Watch this line" : "That episode isn't on disk"}
          >
            <Play className="size-3.5" />
            Preview
          </Button>
        </div>
      </div>
    </div>
  );
}
