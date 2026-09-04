import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
  Check,
  ChevronDown,
  Gavel,
  RefreshCw,
  SkipForward,
  Sparkles,
  Undo2,
  Unlock,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { srsApi } from "@/lib/srs-api";
import { errMsg, useCandidates, useWordAction } from "@/lib/srs-hooks";
import { toast } from "@/components/ui/toast";
import { cn, formatRelative } from "@/lib/utils";
import type { SrsCandidate, SrsJudgeStatus } from "@/lib/srs-types";

/** Judge / filter codes in the learner's words. */
const REASONS: Record<string, string> = {
  fragment: "a piece of a longer word",
  fixed_expression: "only used inside a set phrase",
  function_word: "grammar, not vocabulary",
  name: "looks like a name",
  interjection: "an interjection",
  tokenizer_error: "the tokenizer split it wrong",
  too_basic: "too basic to be worth a card",
  no_clear_moment: "no clear moment in your library yet",
  odd_register: "odd register — not how you'd say it",
  proper_noun: "a proper noun",
  kana_no_gloss: "kana-only with no dictionary entry",
  digits: "just numbers",
  grammar_combo: "a grammar combination, not a word",
  rank: "far outside the useful frequency range",
  known_reading: "you already know this word in kanji",
  no_moment: "no usable sentence in your library",
  duplicate: "already covered by another card",
};

export function reasonText(c: SrsCandidate): string | null {
  const code = c.judge_reason;
  const plain = code ? (REASONS[code] ?? code.replace(/_/g, " ")) : null;
  switch (c.judge_status) {
    case "filtered":
      return plain ? `Filtered: ${plain}` : "Filtered out before the judge";
    case "rejected":
      return plain ? `Judge: ${plain}` : "The judge turned it down";
    case "probably_known":
      return "Common enough that you probably know it";
    case "pending":
      return "Waiting for the judge";
    case "accepted":
      return c.card_id ? "Already a card" : "Accepted — card on its way";
    case "error":
      return plain ? `Failed: ${plain}` : "The judge call failed";
    default:
      return null;
  }
}

/** "rank #1,944 · 5 lines · unlocks 2 episodes · in your next episode" */
export function whyText(c: SrsCandidate): string {
  const bits: string[] = [];
  if (c.freq_rank != null) bits.push(`rank #${c.freq_rank.toLocaleString()}`);
  bits.push(`${c.occ} time${c.occ === 1 ? "" : "s"} in ${c.eps} episode${c.eps === 1 ? "" : "s"}`);
  if (c.moment_lines > 0) bits.push(`${c.moment_lines} clear line${c.moment_lines === 1 ? "" : "s"}`);
  if (c.leverage_crossings > 0)
    bits.push(`unlocks ${c.leverage_crossings} episode${c.leverage_crossings === 1 ? "" : "s"}`);
  if (c.next_watch_hits > 0) bits.push("in your next episode");
  return bits.join(" · ");
}

export function ScoreBar({ score, max }: { score: number; max: number }) {
  const pct = max > 0 ? Math.max(3, Math.min(100, (score / max) * 100)) : 0;
  return (
    <span
      className="inline-flex h-1.5 w-16 overflow-hidden rounded-full bg-surface align-middle"
      title={`Score ${score.toFixed(2)}`}
    >
      <span className="h-full rounded-full bg-brand-bright" style={{ width: `${pct}%` }} />
    </span>
  );
}

const STATUSES: { value: SrsJudgeStatus | "all"; label: string }[] = [
  { value: "unjudged", label: "Not looked at" },
  { value: "pending", label: "Being judged" },
  { value: "rejected", label: "Turned down" },
  { value: "filtered", label: "Filtered" },
  { value: "accepted", label: "Accepted" },
  { value: "error", label: "Failed" },
  { value: "all", label: "Everything" },
];

const SORTS = [
  { value: "score", label: "Best first" },
  { value: "rank", label: "Most common" },
  { value: "occ", label: "Most frequent here" },
];

/**
 * The Up next tab (design §8.6): which words are queued to become cards, why
 * each one is or isn't eligible, and the two things you can do about it.
 */
export function CandidatesTable() {
  const [status, setStatus] = useState<string>("unjudged");
  const [sort, setSort] = useState("score");
  const [q, setQ] = useState("");
  const [debounced, setDebounced] = useState("");
  const [refreshing, setRefreshing] = useState(false);
  const [showKnown, setShowKnown] = useState(false);

  useEffect(() => {
    const t = setTimeout(() => setDebounced(q.trim()), 300);
    return () => clearTimeout(t);
  }, [q]);

  const list = useCandidates({ status, q: debounced, sort, limit: 100 });
  const probably = useCandidates({ status: "probably_known", sort: "rank", limit: 200 });
  const word = useWordAction();

  const items = list.data?.items ?? [];
  const maxScore = items.reduce((m, c) => Math.max(m, c.score), 0);
  const knownItems = probably.data?.items ?? [];

  const refresh = async () => {
    setRefreshing(true);
    try {
      await srsApi.candidates({ status, sort, limit: 1, refresh: true });
      toast.success("Recounting the corpus…", "New candidates appear when the pass finishes");
    } catch (e) {
      toast.error("Couldn't start the recount", errMsg(e));
    } finally {
      setRefreshing(false);
    }
  };

  return (
    <div className="min-w-0">
      {/* controls */}
      <div className="mb-4 flex flex-wrap items-center gap-2">
        <div className="w-full max-w-xs">
          <Input
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="Search a word…"
            className="h-9 font-jp"
          />
        </div>
        <select
          value={status}
          onChange={(e) => setStatus(e.target.value)}
          aria-label="Which candidates"
          className="h-9 rounded-lg border border-border-strong bg-surface px-3 text-xs font-medium text-fg outline-none focus:border-brand"
        >
          {STATUSES.map((s) => (
            <option key={s.value} value={s.value}>
              {s.label}
            </option>
          ))}
        </select>
        <select
          value={sort}
          onChange={(e) => setSort(e.target.value)}
          aria-label="Sort candidates"
          className="h-9 rounded-lg border border-border-strong bg-surface px-3 text-xs font-medium text-fg outline-none focus:border-brand"
        >
          {SORTS.map((s) => (
            <option key={s.value} value={s.value}>
              {s.label}
            </option>
          ))}
        </select>
        <div className="ml-auto flex items-center gap-2 text-[0.7rem] text-faint">
          {list.data?.computed_at && <span>counted {formatRelative(list.data.computed_at)}</span>}
          <Button variant="ghost" size="sm" onClick={refresh} loading={refreshing}>
            <RefreshCw className="size-3.5" />
            Recount
          </Button>
        </div>
      </div>

      {list.isLoading && (
        <div className="space-y-2">
          {Array.from({ length: 6 }).map((_, i) => (
            <Skeleton key={i} className="h-16 w-full rounded-xl" />
          ))}
        </div>
      )}

      {!list.isLoading && items.length === 0 && (
        <p className="rounded-xl border border-dashed border-border-strong px-4 py-10 text-center text-sm text-muted">
          Nothing here. Recount the corpus, or switch which candidates you&apos;re looking at.
        </p>
      )}

      <div className="divide-y divide-border/60 overflow-hidden rounded-xl border border-border bg-surface/40">
        {items.map((c, i) => (
          <CandidateRow
            key={c.lemma}
            rank={i + 1}
            candidate={c}
            maxScore={maxScore}
            busy={word.isPending}
            onSkip={() => word.mutate({ lemma: c.lemma, op: c.user_flag === "skip" ? "unskip" : "skip" })}
            onJudge={() => word.mutate({ lemma: c.lemma, op: "judge" })}
          />
        ))}
      </div>

      {/* Probably known */}
      {knownItems.length > 0 && (
        <div className="mt-6 rounded-xl border border-border bg-surface/30">
          <button
            type="button"
            onClick={() => setShowKnown((v) => !v)}
            className="flex w-full items-center gap-2 px-4 py-3 text-left text-sm font-medium text-fg"
          >
            <ChevronDown className={cn("size-4 text-faint transition-transform", showKnown && "rotate-180")} />
            Probably known ({probably.data?.total ?? knownItems.length})
            <span className="ml-2 text-xs font-normal text-faint">
              Common words that never became cards — confirm them and they stop coming up.
            </span>
          </button>
          {showKnown && (
            <div className="border-t border-border px-4 py-3">
              <div className="mb-3 flex flex-wrap gap-2">
                <Button
                  variant="secondary"
                  size="sm"
                  loading={word.isPending}
                  onClick={() => {
                    for (const c of knownItems.slice(0, 50))
                      word.mutate({ lemma: c.lemma, op: "confirm-known" });
                  }}
                >
                  <Check className="size-3.5" />
                  Confirm the first 50
                </Button>
              </div>
              <div className="flex flex-wrap gap-2">
                {knownItems.map((c) => (
                  <div
                    key={c.lemma}
                    className="flex items-center gap-2 rounded-lg border border-border bg-bg-elevated px-2.5 py-1.5"
                  >
                    <span className="font-jp text-sm text-fg" lang="ja">
                      {c.lemma}
                    </span>
                    {c.freq_rank != null && (
                      <span className="text-[0.65rem] tabular-nums text-faint">
                        #{c.freq_rank.toLocaleString()}
                      </span>
                    )}
                    <button
                      type="button"
                      onClick={() => word.mutate({ lemma: c.lemma, op: "confirm-known" })}
                      className="text-faint transition-colors hover:text-comp-green"
                      title="I know this word"
                    >
                      <Check className="size-3.5" />
                    </button>
                    <button
                      type="button"
                      onClick={() => word.mutate({ lemma: c.lemma, op: "judge" })}
                      className="text-faint transition-colors hover:text-brand-bright"
                      title="Make a card for it after all"
                    >
                      <Gavel className="size-3.5" />
                    </button>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function CandidateRow({
  candidate: c,
  rank,
  maxScore,
  busy,
  onSkip,
  onJudge,
}: {
  candidate: SrsCandidate;
  rank: number;
  maxScore: number;
  busy: boolean;
  onSkip: () => void;
  onJudge: () => void;
}) {
  const reason = reasonText(c);
  const skipped = c.user_flag === "skip";
  return (
    <div
      className={cn(
        "flex flex-wrap items-start gap-3 px-4 py-3 transition-colors hover:bg-surface-hover/50",
        skipped && "opacity-55",
      )}
    >
      <span className="w-6 shrink-0 pt-1 text-right text-xs tabular-nums text-faint">{rank}</span>

      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
          <Link
            to={`/moments?q=${encodeURIComponent(c.lemma)}`}
            className="font-jp text-base font-medium text-fg transition-colors hover:text-brand-bright"
            lang="ja"
            title="See every moment with this word"
          >
            {c.reading && c.reading !== c.lemma ? (
              <ruby>
                {c.lemma}
                <rt className="text-[0.45em] text-faint">{c.reading}</rt>
              </ruby>
            ) : (
              c.lemma
            )}
          </Link>
          {c.gloss && <span className="min-w-0 truncate text-xs text-muted">{c.gloss}</span>}
          <ScoreBar score={c.score} max={maxScore} />
          {c.leverage_crossings > 0 && (
            <span className="inline-flex items-center gap-1 text-[0.7rem] text-comp-green" title="Episodes pushed over your comprehension target">
              <Unlock className="size-3" />+{c.leverage_crossings}
            </span>
          )}
          {c.in_unwatched && <Badge variant="outline">unwatched</Badge>}
          {c.card_id && (
            <Link to={`/study/cards/${c.card_id}`}>
              <Badge variant="success">already a card</Badge>
            </Link>
          )}
        </div>

        <p className="mt-1 text-[0.7rem] text-faint">{whyText(c)}</p>

        {c.best_moment && (
          <p className="mt-1 line-clamp-1 font-jp text-xs text-muted" lang="ja">
            {c.best_moment.text}
            {c.best_moment.translation && (
              <span className="font-sans text-faint"> — {c.best_moment.translation}</span>
            )}
          </p>
        )}

        {reason && (
          <p className="mt-1 text-[0.7rem] text-comp-amber">
            {reason}
            {c.judge_note && <span className="text-faint"> — {c.judge_note}</span>}
          </p>
        )}
      </div>

      <div className="flex shrink-0 items-center gap-1.5">
        <Button variant="ghost" size="sm" onClick={onSkip} disabled={busy}>
          {skipped ? <Undo2 className="size-3.5" /> : <SkipForward className="size-3.5" />}
          {skipped ? "Un-skip" : "Skip"}
        </Button>
        <Button
          variant="secondary"
          size="sm"
          onClick={onJudge}
          disabled={busy || c.judge_status === "pending" || !!c.card_id}
          title="Look for a clear moment and make a card now"
        >
          <Sparkles className="size-3.5" />
          Judge now
        </Button>
      </div>
    </div>
  );
}
