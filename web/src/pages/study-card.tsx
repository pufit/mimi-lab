import { useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import {
  ArrowLeft,
  Check,
  CircleSlash,
  ExternalLink,
  FileText,
  Pencil,
  Play,
  RotateCcw,
  Scissors,
  Shuffle,
  Trash2,
  Zap,
} from "lucide-react";
import { PageHeader } from "@/components/layout/page-header";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { ErrorState } from "@/components/ui/error-state";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { MomentPlayer } from "@/components/moment-player";
import { StudyTabs } from "@/components/srs/study-tabs";
import { ClipPlayer, clipSource, toMoment } from "@/components/srs/clip-player";
import type { ClipPlayerHandle } from "@/components/srs/clip-player";
import { SentenceStack } from "@/components/srs/sentence-stack";
import { SentenceTransport } from "@/components/srs/sentence-transport";
import { stripBidi } from "@/components/srs/token-line";
import { clipClockOrigin, useSentencePlayback } from "@/components/srs/use-sentence-playback";
import type { SentenceMedia } from "@/components/srs/use-sentence-playback";
import { WordChip } from "@/components/srs/word-chip";
import { CardMenu } from "@/components/srs/card-menu";
import { SwapMomentDialog } from "@/components/srs/swap-moment-dialog";
import { EditCardDialog } from "@/components/srs/edit-card-dialog";
import { dueLabel } from "@/components/srs/stack-row";
import { usePlayMoment } from "@/lib/hooks";
import {
  errMsg,
  useCardAction,
  useDeleteCard,
  useRegenClip,
  useSrsCard,
  useSrsSummary,
} from "@/lib/srs-hooks";
import {
  formatDuration,
  momentDurationMs,
  momentWindow,
  splitEvidence,
} from "@/lib/srs-extend";
import { momentSentences } from "@/lib/srs-sentences";
import { cn, formatMs, formatRelative } from "@/lib/utils";
import type { Moment } from "@/lib/types";
import type { SrsRating, SrsReview } from "@/lib/srs-types";

const RATING_LABEL: Record<SrsRating, string> = {
  1: "Again",
  2: "Hard",
  3: "Good",
  4: "Easy",
};

const RATING_TONE: Record<SrsRating, string> = {
  1: "text-comp-red",
  2: "text-comp-amber",
  3: "text-comp-green",
  4: "text-brand-bright",
};

const STATE_LABEL: Record<string, string> = {
  new: "In the stack",
  learning: "Learning",
  review: "In review",
  relearning: "Relearning",
  known: "Known",
  suspended: "Parked",
  rejected: "Not worth it",
};

function Row({ label, value, hint }: { label: string; value: ReactNode; hint?: string }) {
  return (
    <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-0.5 border-b border-border/40 py-2 last:border-0">
      <span className="text-xs text-faint">{label}</span>
      <span className="text-sm font-medium text-fg">{value}</span>
      {hint && <p className="w-full text-[0.7rem] leading-snug text-faint">{hint}</p>}
    </div>
  );
}

/** "Stability 6.9 d ≈ 90 % chance you still know it a week from now." */
function stabilityHint(stability: number | null): string | undefined {
  if (stability == null) return undefined;
  const week = Math.round(100 * Math.pow(0.9, 7 / Math.max(0.1, stability)));
  return `About a ${week}% chance you still remember it a week from now.`;
}

export function CardDetailPage() {
  const { id } = useParams<{ id: string }>();
  const cardId = Number(id);
  const navigate = useNavigate();
  const { data, isLoading, isError, error, refetch } = useSrsCard(cardId);
  const summary = useSrsSummary();
  const act = useCardAction();
  const regen = useRegenClip();
  const del = useDeleteCard();
  const playMigaku = usePlayMoment();

  const [swap, setSwap] = useState(false);
  const [edit, setEdit] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [watching, setWatching] = useState<Moment | null>(null);

  const card = data?.card;
  const failLimit = summary.data?.settings.demote_after_fails ?? 2;

  /** The whole moment — evidence lines before the target, continuation after. */
  const extended = useMemo(() => {
    if (!card || card.extend.length === 0) return null;
    return momentWindow(card);
  }, [card]);
  const evidence = useMemo(
    () => (card ? splitEvidence(card) : { before: [], after: [], count: 0 }),
    [card],
  );
  const clipMs = card ? momentDurationMs(card) : null;

  // Clip navigation (the same stack + clock as the review session, minus the
  // hotkeys — this is an ordinary page, the arrows scroll it).
  const player = useRef<ClipPlayerHandle | null>(null);
  const sentences = useMemo(() => (card ? momentSentences(card) : []), [card]);
  const media = useMemo<SentenceMedia | null>(() => {
    if (!card) return null;
    const origin = clipClockOrigin(card);
    if (!origin) return null;
    return {
      video: () => player.current?.video() ?? null,
      seekPlay: (s) => player.current?.seek(s),
      ...origin,
    };
  }, [card]);
  const nav = useSentencePlayback(sentences, media);

  /** Every line the clip window covers besides the target — flagged in context. */
  const evidenceIds = useMemo(
    () =>
      new Set(
        (card?.extend ?? []).map((l) => l.line_id).filter((id): id is number => id != null),
      ),
    [card],
  );

  if (isLoading) {
    return (
      <div className="animate-fade-in">
        <PageHeader title="Card" />
        <StudyTabs />
        <div className="grid gap-6 lg:grid-cols-[minmax(0,1.4fr)_minmax(0,1fr)]">
          <Skeleton className="aspect-video w-full rounded-xl" />
          <Skeleton className="h-64 w-full rounded-xl" />
        </div>
      </div>
    );
  }

  if (isError || !card) {
    return (
      <div className="animate-fade-in">
        <PageHeader title="Card" />
        <StudyTabs />
        <ErrorState message={errMsg(error) || "That card is gone."} onRetry={() => refetch()} />
      </div>
    );
  }

  const moments = data?.moments ?? [];
  const siblings = data?.siblings ?? [];
  const reviews = data?.reviews ?? [];

  return (
    <div className="animate-fade-in">
      <PageHeader
        title={card.lemma}
        subtitle={card.meaning_short ?? card.gloss ?? undefined}
        actions={
          <>
            <Button variant="ghost" size="sm" onClick={() => navigate(-1)}>
              <ArrowLeft className="size-4" />
              Back
            </Button>
            <Button
              variant="secondary"
              size="sm"
              onClick={() => regen.mutate(card.id)}
              loading={regen.isPending}
              disabled={!card.source_available}
              title={
                card.source_available
                  ? "Cut the clip again from the episode"
                  : "That episode isn't on disk any more"
              }
            >
              <Scissors className="size-4" />
              Regenerate clip
            </Button>
            <Button variant="secondary" size="sm" onClick={() => setEdit(true)}>
              <Pencil className="size-4" />
              Edit
            </Button>
            {card.state !== "known" && (
              <Button
                variant="secondary"
                size="sm"
                onClick={() => act.mutate({ id: card.id, action: "known" })}
              >
                <Check className="size-4" />
                Mark known
              </Button>
            )}
            {card.state === "new" && !card.study_now && (
              <Button
                variant="primary"
                size="sm"
                onClick={() => act.mutate({ id: card.id, action: "study_next" })}
              >
                <Zap className="size-4" />
                Study next
              </Button>
            )}
            <CardMenu
              card={card}
              onSwapMoment={() => setSwap(true)}
              onEdit={() => setEdit(true)}
              onDeleted={() => navigate("/study/stack")}
            />
          </>
        }
      />

      <StudyTabs />

      <div className="grid gap-6 lg:grid-cols-[minmax(0,1.4fr)_minmax(0,1fr)]">
        {/* ── left: the moment ─────────────────────────── */}
        <div className="min-w-0 space-y-4">
          <ClipPlayer
            ref={player}
            clip={card.clip}
            source={clipSource(card)}
            controls
            videoClassName="max-h-[52vh]"
          />

          {media && (
            <SentenceTransport
              sentences={sentences}
              activeIndex={nav.activeIndex}
              canNext={nav.canNext}
              onPrev={nav.prev}
              onNext={nav.next}
              onReplay={nav.replay}
              onSeek={nav.seekTo}
              className="px-0.5"
            />
          )}

          <div className="flex flex-wrap items-center gap-2">
            <Button
              variant="secondary"
              size="sm"
              disabled={!card.source_available}
              onClick={() => setWatching(toMoment(card))}
              title={card.source_available ? "Watch the scene in context" : "The episode file is gone"}
            >
              <Play className="size-3.5" />
              Watch scene
            </Button>
            {extended != null && (
              <Button
                variant="ghost"
                size="sm"
                disabled={!card.source_available}
                onClick={() =>
                  setWatching({
                    ...toMoment(card),
                    start_ms: extended.start_ms,
                    end_ms: extended.end_ms,
                  })
                }
                title={
                  evidence.count > 0
                    ? "The meaning leans on the neighbouring lines — play all of them"
                    : "The sentence continues into the next cue"
                }
              >
                Play extended
              </Button>
            )}
            <Button
              variant="ghost"
              size="sm"
              disabled={!card.line_id}
              loading={playMigaku.isPending}
              onClick={() => card.line_id && playMigaku.mutate(card.line_id)}
            >
              <ExternalLink className="size-3.5" />
              Play in Migaku
            </Button>
            {card.line_available && card.episode_id != null && (
              <Link to={`/transcript/${card.episode_id}`}>
                <Button variant="ghost" size="sm">
                  <FileText className="size-3.5" />
                  Transcript
                </Button>
              </Link>
            )}
            <Button variant="ghost" size="sm" onClick={() => setSwap(true)}>
              <Shuffle className="size-3.5" />
              Swap moment
            </Button>
          </div>

          <Card>
            <CardContent className="space-y-3 p-5">
              <WordChip card={card} size="md" showMeaning={false} />
              <SentenceStack
                card={card}
                sentences={sentences}
                activeIndex={nav.activeIndex}
                playing={nav.playing}
                furigana="all"
                showStatus
                onSeek={media ? nav.seekTo : undefined}
              />
              {card.translation && (
                <p className="flex flex-wrap items-baseline gap-2 text-sm leading-relaxed text-muted">
                  <span>{card.translation}</span>
                  {card.translation_source === "mt" && (
                    <Badge variant="outline">machine translation</Badge>
                  )}
                </p>
              )}
              <div className="flex flex-wrap items-center gap-2 text-xs text-faint">
                {card.show_title && <span>{card.show_title}</span>}
                {card.ep_number != null && <span>· E{card.ep_number}</span>}
                <span>· {formatMs(card.start_ms)}</span>
                {clipMs != null && (
                  <span title="How much of the scene this card's clip covers">
                    · {formatDuration(clipMs)} clip
                  </span>
                )}
                {evidence.count > 0 && (
                  <Badge variant="outline" title={card.why_clear ?? undefined}>
                    +{evidence.count} line{evidence.count === 1 ? "" : "s"} of context
                  </Badge>
                )}
                {card.tags.map((t) => (
                  <Badge key={t} variant="outline">
                    {t}
                  </Badge>
                ))}
              </div>

              {(card.meaning_full || card.why_clear || card.usage_note) && (
                <div className="space-y-2 border-t border-border pt-3">
                  {card.meaning_full && (
                    <p className="text-sm leading-relaxed text-muted">{card.meaning_full}</p>
                  )}
                  {card.why_clear && (
                    <p className="text-xs leading-relaxed text-faint">
                      <span className="font-medium text-muted">Why this moment: </span>
                      {card.why_clear}
                    </p>
                  )}
                  {card.usage_note && (
                    <p className="text-xs leading-relaxed text-faint">
                      <span className="font-medium text-muted">Usage: </span>
                      {card.usage_note}
                    </p>
                  )}
                  {card.notes && (
                    <p className="text-xs leading-relaxed text-brand-bright/80">{card.notes}</p>
                  )}
                </div>
              )}
            </CardContent>
          </Card>

          {card.context.length > 0 && (
            <Card>
              <CardHeader className="pb-2">
                <CardTitle>In context</CardTitle>
              </CardHeader>
              <CardContent className="space-y-2">
                {card.context.map((line) => (
                  <div
                    key={`${line.idx}-${line.start_ms}`}
                    className={cn(
                      "rounded-lg px-3 py-2",
                      line.is_target
                        ? "bg-brand/10 ring-1 ring-brand/30"
                        : line.line_id != null && evidenceIds.has(line.line_id)
                          ? "bg-surface/50 ring-1 ring-border-strong"
                          : "bg-surface/50",
                    )}
                  >
                    <p className="font-jp text-sm leading-relaxed text-fg" lang="ja">
                      {stripBidi(line.text)}
                      {!line.is_target && line.line_id != null && evidenceIds.has(line.line_id) && (
                        <span className="ml-2 align-middle text-[0.65rem] font-medium text-faint">
                          in the clip
                        </span>
                      )}
                    </p>
                    {line.translation && (
                      <p className="mt-0.5 text-xs leading-relaxed text-faint">{line.translation}</p>
                    )}
                  </div>
                ))}
              </CardContent>
            </Card>
          )}
        </div>

        {/* ── right: the numbers ───────────────────────── */}
        <div className="min-w-0 space-y-4">
          {/* the word's other cards (multi-card words): same word, other anime */}
          {siblings.length > 0 && (
            <Card>
              <CardHeader className="pb-2">
                <CardTitle>Other cards for this word</CardTitle>
              </CardHeader>
              <CardContent className="space-y-2 pt-1">
                {siblings.map((s) => (
                  <Link
                    key={s.id}
                    to={`/study/cards/${s.id}`}
                    className={cn(
                      "block rounded-lg border border-border/70 px-3 py-2 transition-colors hover:border-border-strong",
                      s.state === "rejected" && "opacity-60",
                    )}
                  >
                    <p className="font-jp text-sm leading-relaxed text-fg" lang="ja">
                      {stripBidi(s.text)}
                    </p>
                    <p className="mt-0.5 flex flex-wrap items-center gap-1.5 text-[0.7rem] text-faint">
                      {s.show_title && <span className="truncate">{s.show_title}</span>}
                      {s.ep_number != null && <span>· E{s.ep_number}</span>}
                      <Badge variant={s.state === "rejected" ? "danger" : "outline"}>
                        {STATE_LABEL[s.state] ?? s.state}
                      </Badge>
                      {s.state === "new" && s.queue_pos != null && <span>· stack #{s.queue_pos}</span>}
                    </p>
                  </Link>
                ))}
              </CardContent>
            </Card>
          )}
          <Card>
            <CardHeader className="pb-2">
              <CardTitle>Scheduling</CardTitle>
            </CardHeader>
            <CardContent className="pt-1">
              <Row
                label="State"
                value={
                  <span className="inline-flex items-center gap-2">
                    {STATE_LABEL[card.state] ?? card.state}
                    {card.study_now && <Badge variant="solid">study next</Badge>}
                    {card.buried_until && <Badge variant="outline">skipped today</Badge>}
                  </span>
                }
              />
              {card.state === "new" ? (
                <Row label="Position in the stack" value={card.queue_pos ?? "—"} />
              ) : (
                <Row label="Due" value={dueLabel(card.due_at)} />
              )}
              <Row
                label="Interval"
                value={card.scheduled_days > 0 ? `${card.scheduled_days} d` : "—"}
              />
              <Row
                label="Stability"
                value={card.stability != null ? `${card.stability.toFixed(1)} d` : "—"}
                hint={stabilityHint(card.stability)}
              />
              <Row
                label="Difficulty"
                value={card.difficulty != null ? card.difficulty.toFixed(1) : "—"}
                hint={
                  card.difficulty != null
                    ? card.difficulty >= 7
                      ? "A stubborn word — it comes back often on purpose."
                      : "An easy word for you — intervals grow quickly."
                    : undefined
                }
              />
              <Row label="Reviews" value={`${card.reps} · ${card.lapses} lapse${card.lapses === 1 ? "" : "s"}`} />
              <Row
                label="Misses"
                value={
                  <span className="tabular-nums tracking-widest text-fg">
                    {failLimit > 0
                      ? "●".repeat(Math.min(card.fail_count, failLimit)) +
                        "○".repeat(Math.max(0, failLimit - card.fail_count))
                      : card.fail_count}
                    <span className="ml-2 text-xs text-faint">
                      {failLimit > 0 ? `of ${failLimit}` : "returning is off"}
                    </span>
                  </span>
                }
                hint={
                  failLimit > 0
                    ? "Missing it this many times sends the word back to the stack with a fresh moment."
                    : undefined
                }
              />
              {card.demoted_count > 0 && (
                <Row
                  label="Returned to the stack"
                  value={`${card.demoted_count}×`}
                  hint={card.demoted_at ? `last time ${formatRelative(card.demoted_at)}` : undefined}
                />
              )}
              {card.introduced_at && (
                <Row label="First seen" value={formatRelative(card.introduced_at)} />
              )}
              {card.known_at && (
                <Row
                  label="Known since"
                  value={`${formatRelative(card.known_at)} (${card.known_source ?? "srs"})`}
                />
              )}
              <Row
                label="Generation"
                value={
                  <span className="text-xs text-muted">
                    {card.source}
                    {card.score != null && ` · score ${card.score.toFixed(2)}`}
                    {card.clarity != null && ` · clarity ${card.clarity.toFixed(2)}`}
                    {card.priority != null && ` · priority ${card.priority}`}
                  </span>
                }
              />
            </CardContent>
          </Card>

          <Card>
            <CardHeader className="flex-row items-center justify-between gap-3 pb-2">
              <CardTitle>Other moments</CardTitle>
              <Button variant="ghost" size="sm" onClick={() => setSwap(true)}>
                <Shuffle className="size-3.5" />
                Choose another
              </Button>
            </CardHeader>
            <CardContent className="pt-1">
              {moments.filter((m) => !m.is_primary).length === 0 ? (
                <p className="py-3 text-xs text-faint">
                  This is the only moment found for {card.lemma} so far.
                </p>
              ) : (
                <ul className="space-y-2">
                  {moments
                    .filter((m) => !m.is_primary)
                    .slice(0, 5)
                    .map((m) => (
                      <li key={m.id} className="border-b border-border/40 pb-2 last:border-0">
                        <p className="line-clamp-2 font-jp text-xs leading-relaxed text-muted" lang="ja">
                          {stripBidi(m.text)}
                        </p>
                        <p className="mt-0.5 text-[0.7rem] text-faint">
                          {m.show_title}
                          {m.ep_number != null && ` · E${m.ep_number}`}
                          {m.accepted === false && " · turned down"}
                        </p>
                      </li>
                    ))}
                </ul>
              )}
            </CardContent>
          </Card>

          <Card>
            <CardHeader className="pb-2">
              <CardTitle>History</CardTitle>
            </CardHeader>
            <CardContent className="pt-1">
              {reviews.length === 0 ? (
                <p className="py-3 text-xs text-faint">No reviews yet.</p>
              ) : (
                <ul className="space-y-1.5">
                  {reviews.slice(0, 30).map((r: SrsReview) => (
                    <li
                      key={r.id}
                      className={cn(
                        "flex flex-wrap items-baseline gap-x-2 text-xs",
                        r.undone && "line-through opacity-50",
                      )}
                    >
                      <span className={cn("font-medium", RATING_TONE[r.rating])}>
                        {RATING_LABEL[r.rating]}
                      </span>
                      <span className="text-faint">
                        {r.state_before} → {r.state_after}
                      </span>
                      {r.demoted && <Badge variant="warning">returned</Badge>}
                      <span className="ml-auto text-faint">{formatRelative(r.reviewed_at)}</span>
                    </li>
                  ))}
                </ul>
              )}
            </CardContent>
          </Card>

          <Card className="border-comp-red/25">
            <CardHeader className="pb-2">
              <CardTitle className="text-comp-red">Danger zone</CardTitle>
            </CardHeader>
            <CardContent className="flex flex-wrap gap-2 pt-1">
              <Button
                variant="secondary"
                size="sm"
                onClick={() => act.mutate({ id: card.id, action: "forget" })}
                title="Wipe the scheduling state and put the card back in the stack"
              >
                <RotateCcw className="size-3.5" />
                Forget progress
              </Button>
              <Button
                variant="secondary"
                size="sm"
                onClick={() => act.mutate({ id: card.id, action: "reject" })}
                title="Park it permanently — it never comes back as a candidate"
              >
                <CircleSlash className="size-3.5" />
                Not worth it
              </Button>
              <Button variant="danger" size="sm" onClick={() => setConfirmDelete(true)}>
                <Trash2 className="size-3.5" />
                Delete
              </Button>
            </CardContent>
          </Card>
        </div>
      </div>

      <SwapMomentDialog card={card} open={swap} onOpenChange={setSwap} moments={moments} />
      <EditCardDialog card={card} open={edit} onOpenChange={setEdit} />
      {watching && <MomentPlayer moment={watching} onClose={() => setWatching(null)} />}

      <Dialog open={confirmDelete} onOpenChange={setConfirmDelete}>
        <DialogContent className="w-[min(28rem,calc(100vw-2rem))]">
          <DialogHeader>
            <DialogTitle>Delete {card.lemma}?</DialogTitle>
            <DialogDescription>
              The card, its clip and its history go away for good. The word returns to the candidate
              pool.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="ghost" onClick={() => setConfirmDelete(false)}>
              Cancel
            </Button>
            <Button
              variant="danger"
              loading={del.isPending}
              onClick={() =>
                del.mutate(card.id, { onSuccess: () => navigate("/study/stack") })
              }
            >
              <Trash2 className="size-4" />
              Delete for good
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
