// The review session (design §8.3, §8.4, §3.3, §3.6, §3.9–3.11).
//
// A chromeless route with one job: answer cards. Everything about this page is
// built around four rules from the design:
//
//  1. **The queue is imperative.** `fetchSrsQueue` is a bare `fetchQuery`; the
//     session reducer owns the cards from the moment they land. No SSE
//     invalidation, focus refetch or reconnect can reorder them mid-session.
//  2. **One persistent `<video>`** for the whole session (`useClipElement`),
//     unlocked by the first gesture; per card only `src` changes. Reveal never
//     pauses; rating pauses and resets before the next `src` is assigned.
//  3. **`client_id` lives in the mutation variables**, minted when the rating is
//     dispatched, so a manual retry replays the exact same variables and the
//     server dedupes (§3.10) instead of double-rating.
//  4. **409 drops the card** — the card is no longer in an active state; it is
//     never re-inserted (§7.2).
//  5. **Nothing replaces the card on screen but the learner.** A learning step
//     that comes due mid-answer slots in *behind* the current card (`slotDue`);
//     it becomes the head only at a card boundary — a rating or a K/S/B/D —
//     so a review is always the *next* card opened, never a swap under your
//     hands (the user, 2026-09-03).
//
// The current card is always `queue[0]`: a rating removes it from the queue, so
// "re-inserted at the current index" is literally `unshift`.

import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { Loader2 } from "lucide-react";
import { MomentPlayer } from "@/components/moment-player";
import { CardMenu } from "@/components/srs/card-menu";
import { EditCardDialog } from "@/components/srs/edit-card-dialog";
import { LessonActions } from "@/components/srs/lesson-card";
import { RatingBar } from "@/components/srs/rating-bar";
import type { ClipPlayerHandle } from "@/components/srs/clip-player";
import { ClipStage, ReviewCard } from "@/components/srs/review-card";
import { SessionProgress } from "@/components/srs/session-progress";
import {
  EMPTY_STATS,
  SessionSummary,
  type SessionStats,
} from "@/components/srs/session-summary";
import { ShortcutsSheet } from "@/components/srs/shortcuts-sheet";
import { SwapMomentDialog } from "@/components/srs/swap-moment-dialog";
import { TokenLine } from "@/components/srs/token-line";
import { useClipElement } from "@/components/srs/use-clip-element";
import { clipClockOrigin, useSentencePlayback } from "@/components/srs/use-sentence-playback";
import type { SentenceMedia } from "@/components/srs/use-sentence-playback";
import { toast, useToastViewportPosition } from "@/components/ui/toast";
import { useHotkeys } from "@/lib/hotkeys";
import type { HotkeyMap } from "@/lib/hotkeys";
import { momentWindow } from "@/lib/srs-extend";
import { momentSentences } from "@/lib/srs-sentences";
import {
  errMsg,
  fetchSrsQueue,
  useCardAction,
  useMoveCard,
  useReviewCard,
  useSrsPrefs,
  useSrsSettings,
  useSrsSummary,
  useUndoReview,
} from "@/lib/srs-hooks";
import type {
  SrsAction,
  SrsCard,
  SrsQueue,
  SrsQueueCounts,
  SrsRating,
  SrsReviewRequest,
  SrsReviewResult,
} from "@/lib/srs-types";
import type { Moment } from "@/lib/types";

const QUEUE_LIMIT = 20;
/** Refill when fewer than this many cards are left locally (§8.3.7). */
const REFILL_AT = 5;
/** Crossfade to the back (§8.3.4) — also how long the exit ghost lives. */
const REVEAL_MS = 180;
/** Rating keys arm this long after the crossfade completes (§8.3.4/§8.4). */
const ARM_MS = 200;
const FLASH_MS = 200;
/** How often the learn-ahead check runs while a card is on screen (§3.6.5). */
const LEARN_AHEAD_POLL_MS = 5000;
/** Don't hammer the queue endpoint while cards are still in hand. */
const REFILL_COOLDOWN_MS = 10_000;

/**
 * There is no separate "lesson" phase any more (2026-09-03): a new card goes
 * through the same front → back as every other card, with the answer blurred
 * until the reveal. `card.state === "new"` still changes the label on the front
 * and the buttons under the back — see `LessonActions`.
 */
type Phase = "loading" | "front" | "back" | "done";

interface HistoryEntry {
  card: SrsCard;
  rating: SrsRating;
  client_id: string;
  at: number;
  /** Filled once the server answers — enables a targeted undo. */
  review_id: number | null;
}

interface ActionEntry {
  /** `bottom` = the card went back to the end of the stack (its Undo is a `move`). */
  kind: "known" | "suspend" | "bury" | "bottom";
  card: SrsCard;
  at: number;
}

interface SessionState {
  queue: SrsCard[];
  phase: Phase;
  history: HistoryEntry[];
  actionStack: ActionEntry[];
  stats: SessionStats;
  learningSoon: SrsCard[];
  /** `Date.now() - Date.parse(server_time)`; every due comparison subtracts it. */
  skewMs: number;
  counts: SrsQueueCounts | null;
  day: string;
  /** Cards retired mid-session (409, K/S/B) — never re-admitted by a refill. */
  retired: number[];
  /** When the current card was put on screen (→ `elapsed_ms`). */
  shownAt: number;
  loaded: boolean;
}

const INITIAL: SessionState = {
  queue: [],
  phase: "loading",
  history: [],
  actionStack: [],
  stats: EMPTY_STATS,
  learningSoon: [],
  skewMs: 0,
  counts: null,
  day: "",
  retired: [],
  shownAt: Date.now(),
  loaded: false,
};

type Act =
  | {
      type: "loaded";
      cards: SrsCard[];
      learningSoon: SrsCard[];
      counts: SrsQueueCounts;
      serverTime: string;
      day: string;
      mode: "init" | "append";
      now: number;
    }
  | { type: "reveal" }
  | {
      type: "rate";
      card: SrsCard;
      rating: SrsRating;
      client_id: string;
      now: number;
      elapsed_ms: number;
    }
  | { type: "result"; result: SrsReviewResult; client_id: string }
  | { type: "conflict"; client_id: string }
  | { type: "restoreRated"; client_id: string; now: number }
  | { type: "remove"; entry: ActionEntry; now: number; undoable?: boolean }
  | { type: "replaceCard"; card: SrsCard }
  | { type: "undoAction"; entry: ActionEntry; card: SrsCard; now: number }
  | { type: "undoneReview"; card: SrsCard; review_id: number; now: number }
  | { type: "learnAhead"; now: number }
  | { type: "exhausted" };

/** Re-derive the phase for whatever card is now at the head of the queue. */
function settle(s: SessionState, now: number): SessionState {
  if (s.queue.length === 0) {
    // Learn-ahead: rather than end the session, continue with the learning
    // cards that fall due within LEARN_AHEAD_MIN (§3.6.5).
    if (s.learningSoon.length > 0) {
      return { ...s, queue: s.learningSoon, learningSoon: [], phase: "front", shownAt: now };
    }
    return { ...s, phase: s.loaded ? "done" : "loading" };
  }
  return { ...s, phase: "front", shownAt: now };
}

/** Split `learningSoon` into the steps that have come due (skew-corrected) and the rest. */
function dueLearning(s: SessionState, now: number): { due: SrsCard[]; rest: SrsCard[] } {
  if (s.learningSoon.length === 0) return { due: [], rest: s.learningSoon };
  const serverNow = now - s.skewMs;
  const live = new Set(s.queue.map((c) => c.id));
  const due: SrsCard[] = [];
  const rest: SrsCard[] = [];
  for (const c of s.learningSoon) {
    const t = c.due_at ? Date.parse(c.due_at) : NaN;
    if (Number.isNaN(t) || t > serverNow) rest.push(c);
    // due — and only worth slotting if the queue does not already hold it
    else if (!live.has(c.id)) due.push(c);
  }
  return { due, rest };
}

/**
 * Slot the learning steps that have come due into the queue. `at = 0` is a card
 * boundary (the previous card was just rated or removed): the step is the next
 * card on screen. `at = 1` is the periodic poll: the step goes right *behind*
 * the card in hand — a card is never swapped out from under the learner.
 */
function slotDue(s: SessionState, now: number, at: 0 | 1): SessionState {
  const { due, rest } = dueLearning(s, now);
  if (rest.length === s.learningSoon.length) return s;
  const queue =
    at === 0 || s.queue.length === 0
      ? [...due, ...s.queue]
      : [s.queue[0], ...due, ...s.queue.slice(1)];
  return {
    ...s,
    queue,
    learningSoon: rest,
    // a learn-ahead re-entry is a card in hand; the chip must not read 0 while it
    // is on screen.
    counts: s.counts ? { ...s.counts, learning_due: s.counts.learning_due + due.length } : s.counts,
  };
}

function countState(cards: SrsCard[], state: SrsCard["state"]): number {
  return cards.reduce((n, c) => n + (c.state === state ? 1 : 0), 0);
}

function bucketDelta(counts: SrsQueueCounts | null, card: SrsCard, sign: 1 | -1): SrsQueueCounts | null {
  if (!counts) return counts;
  const c = { ...counts };
  const bump = (n: number) => Math.max(0, n + sign * -1);
  if (card.state === "new") c.new_left_today = bump(c.new_left_today);
  else if (card.state === "review") c.review_due = bump(c.review_due);
  else if (card.state === "learning" || card.state === "relearning")
    c.learning_due = bump(c.learning_due);
  return c;
}

function reducer(s: SessionState, a: Act): SessionState {
  switch (a.type) {
    case "loaded": {
      let queue: SrsCard[];
      if (a.mode === "init" || s.queue.length === 0) queue = a.cards;
      else {
        // A refill appends — except learning steps, which are due *now* and
        // belong right behind the card in hand, not after four other cards.
        const isStep = (c: SrsCard) => c.state === "learning" || c.state === "relearning";
        queue = [
          s.queue[0],
          ...a.cards.filter(isStep),
          ...s.queue.slice(1),
          ...a.cards.filter((c) => !isStep(c)),
        ];
      }
      const parsed = Date.parse(a.serverTime);
      const next: SessionState = {
        ...s,
        queue,
        learningSoon: a.learningSoon,
        // The server's buckets are "what is still owed today"; a card already in
        // hand (an explicit "+5 more new" grant, a learn-ahead re-entry) is not
        // owed any more, and the chips would read 0 while it is on screen. Floor
        // each bucket at what the queue actually holds — it still only ever
        // decreases from there, as `bucketDelta` drains it.
        counts: {
          ...a.counts,
          new_left_today: Math.max(a.counts.new_left_today, countState(queue, "new")),
          review_due: Math.max(a.counts.review_due, countState(queue, "review")),
          learning_due: Math.max(
            a.counts.learning_due,
            countState(queue, "learning") + countState(queue, "relearning"),
          ),
        },
        day: a.day,
        skewMs: Number.isNaN(parsed) ? s.skewMs : a.now - parsed,
        loaded: true,
      };
      // Only re-time the head when there was none before, so a refill never
      // restarts the current card's answer timer.
      return s.queue.length === 0 ? settle(next, a.now) : next;
    }

    case "reveal":
      return s.phase === "front" ? { ...s, phase: "back" } : s;

    case "rate": {
      const idx = s.queue.findIndex((c) => c.id === a.card.id);
      if (idx < 0) return s;
      const queue = s.queue.slice();
      queue.splice(idx, 1);
      const ratings = { ...s.stats.ratings };
      ratings[a.rating] = ratings[a.rating] + 1;
      const stats: SessionStats = {
        ...s.stats,
        reviewed: s.stats.reviewed + 1,
        again: s.stats.again + (a.rating === 1 ? 1 : 0),
        newLearned: s.stats.newLearned + (a.card.state === "new" ? 1 : 0),
        timeMs: s.stats.timeMs + Math.min(a.elapsed_ms, 120_000),
        ratings,
      };
      const counts = bucketDelta(s.counts, a.card, 1);
      // A card boundary: a learning step that came due meanwhile is the next
      // card on screen.
      return slotDue(
        settle(
          {
            ...s,
            queue,
            stats,
            counts: counts ? { ...counts, reviewed_today: counts.reviewed_today + 1 } : counts,
            history: [
              ...s.history,
              { card: a.card, rating: a.rating, client_id: a.client_id, at: a.now, review_id: null },
            ],
          },
          a.now,
        ),
        a.now,
        0,
      );
    }

    case "result": {
      const history = s.history.map((h) =>
        h.client_id === a.client_id ? { ...h, review_id: a.result.review_id } : h,
      );
      return {
        ...s,
        history,
        stats: a.result.duplicate
          ? s.stats
          : { ...s.stats, becameKnown: s.stats.becameKnown + (a.result.became_known ? 1 : 0) },
      };
    }

    // 409: the card was not in an active state. It has already been popped by
    // "rate"; undo the optimistic bookkeeping and never re-admit it.
    case "conflict": {
      const entry = s.history.find((h) => h.client_id === a.client_id);
      if (!entry) return s;
      return {
        ...s,
        history: s.history.filter((h) => h.client_id !== a.client_id),
        retired: [...s.retired, entry.card.id],
        stats: revert(s.stats, entry),
        counts: revertCounts(s.counts, entry.card),
      };
    }

    // Network / 5xx: put the card back at the current index (= the head).
    case "restoreRated": {
      const entry = s.history.find((h) => h.client_id === a.client_id);
      if (!entry) return s;
      return settle(
        {
          ...s,
          queue: [entry.card, ...s.queue],
          history: s.history.filter((h) => h.client_id !== a.client_id),
          stats: revert(s.stats, entry),
          counts: revertCounts(s.counts, entry.card),
        },
        a.now,
      );
    }

    // An edit / moment swap applied while the card is in hand. Only the queue
    // copy changes: no counts, no history, no settle().
    case "replaceCard": {
      const idx = s.queue.findIndex((c) => c.id === a.card.id);
      if (idx < 0) return s;
      const queue = s.queue.slice();
      queue[idx] = a.card;
      return { ...s, queue };
    }

    case "remove": {
      const idx = s.queue.findIndex((c) => c.id === a.entry.card.id);
      if (idx < 0) return s;
      const queue = s.queue.slice();
      queue.splice(idx, 1);
      // Also a card boundary — a due learning step comes up next.
      return slotDue(
        settle(
          {
            ...s,
            queue,
            retired: [...s.retired, a.entry.card.id],
            // `undoable: false` — the card left the queue through a path Z cannot
            // reverse (hard delete, "not worth it"), so it never enters the stack.
            actionStack: a.undoable === false ? s.actionStack : [...s.actionStack, a.entry],
            // A card sent to the bottom is still owed today — the next new card
            // takes its slot — so the "new left" chip must not drain for it.
            counts: a.entry.kind === "bottom" ? s.counts : bucketDelta(s.counts, a.entry.card, 1),
          },
          a.now,
        ),
        a.now,
        0,
      );
    }

    case "undoAction":
      return settle(
        {
          ...s,
          queue: [a.card, ...s.queue],
          actionStack: s.actionStack.filter((e) => e !== a.entry),
          retired: s.retired.filter((id) => id !== a.entry.card.id),
          counts: a.entry.kind === "bottom" ? s.counts : bucketDelta(s.counts, a.card, -1),
        },
        a.now,
      );

    case "undoneReview": {
      const entry = [...s.history].reverse().find((h) => h.review_id === a.review_id);
      const history = entry ? s.history.filter((h) => h !== entry) : s.history;
      const counts = revertCounts(s.counts, a.card);
      return settle(
        {
          ...s,
          queue: [a.card, ...s.queue],
          history,
          retired: s.retired.filter((id) => id !== a.card.id),
          stats: entry ? revert(s.stats, entry) : s.stats,
          counts:
            counts && entry
              ? { ...counts, reviewed_today: Math.max(0, counts.reviewed_today - 1) }
              : counts,
        },
        a.now,
      );
    }

    // The 5 s poll. A step that has come due goes *behind* the card in hand —
    // prepending it here swapped the card the learner was answering (and, with
    // no settle(), showed the newcomer already revealed): the "reviews pop up
    // while I'm doing another card" bug, 2026-09-03.
    case "learnAhead": {
      const next = slotDue(s, a.now, 1);
      return next !== s && s.queue.length === 0 ? settle(next, a.now) : next;
    }

    case "exhausted":
      return s.queue.length === 0 && s.learningSoon.length === 0 && s.phase !== "done"
        ? { ...s, phase: "done", loaded: true }
        : s;

    default:
      return s;
  }
}

function revert(stats: SessionStats, entry: HistoryEntry): SessionStats {
  const ratings = { ...stats.ratings };
  ratings[entry.rating] = Math.max(0, ratings[entry.rating] - 1);
  return {
    ...stats,
    reviewed: Math.max(0, stats.reviewed - 1),
    again: Math.max(0, stats.again - (entry.rating === 1 ? 1 : 0)),
    newLearned: Math.max(0, stats.newLearned - (entry.card.state === "new" ? 1 : 0)),
    ratings,
  };
}

function revertCounts(counts: SrsQueueCounts | null, card: SrsCard): SrsQueueCounts | null {
  return bucketDelta(counts, card, -1);
}

/**
 * `MomentPlayer` adapter (§8.10). `window` widens it to the whole moment — the
 * evidence lines before the target and the continuation after it.
 */
function toMoment(card: SrsCard, window?: { start_ms: number; end_ms: number }): Moment | null {
  if (card.line_id == null || card.episode_id == null) return null;
  return {
    line_id: card.line_id,
    anilist_id: card.anilist_id ?? 0,
    episode_id: card.episode_id,
    title: card.show_title,
    ep_number: card.ep_number,
    text: card.text,
    text_furigana: card.text_furigana,
    translation: card.translation,
    start_ms: window?.start_ms ?? card.start_ms,
    end_ms: window?.end_ms ?? card.end_ms,
  };
}

const ACTION_LABEL: Record<ActionEntry["kind"], string> = {
  known: "Marked known",
  suspend: "Suspended",
  bury: "Buried for today",
  bottom: "Sent to the bottom",
};

export function ReviewPage() {
  const navigate = useNavigate();
  const qc = useQueryClient();
  const [prefs, setPrefs] = useSrsPrefs();
  const clip = useClipElement();
  const [state, dispatch] = useReducer(reducer, INITIAL);
  const stateRef = useRef(state);
  stateRef.current = state;

  useToastViewportPosition("top-center");

  const { data: summary } = useSrsSummary();
  const { data: settings } = useSrsSettings();

  const [armed, setArmed] = useState(false);
  const [contextOpen, setContextOpen] = useState(false);
  const [translationOn, setTranslationOn] = useState(true);
  const [flash, setFlash] = useState<SrsRating | null>(null);
  const [ghost, setGhost] = useState<SrsCard | null>(null);
  const [shortcutsOpen, setShortcutsOpen] = useState(false);
  const [editOpen, setEditOpen] = useState(false);
  const [swapOpen, setSwapOpen] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  /**
   * The session container. `CardMenu` suppresses Radix's focus-return to its
   * trigger button (a focused trigger swallows Space, which re-opens the menu
   * and disarms every shortcut for the rest of the session), so the focus has
   * to land somewhere neutral instead.
   */
  const sessionRef = useRef<HTMLDivElement | null>(null);
  const handleMenuOpen = useCallback((open: boolean) => {
    setMenuOpen(open);
    if (!open) requestAnimationFrame(() => sessionRef.current?.focus());
  }, []);
  const [scene, setScene] = useState<Moment | null>(null);
  const [fetching, setFetching] = useState(false);

  /** The episode-stream fallback shown while a clip is still being cut. */
  const fallbackClip = useRef<ClipPlayerHandle | null>(null);
  const fetchingRef = useRef(false);
  const lastFetchRef = useRef(0);
  const doneRef = useRef(false);
  const inFlight = useRef(0);
  const demotionToasts = useRef(new Map<number, number>());

  const current = state.queue[0] ?? null;
  const dialogOpen = shortcutsOpen || editOpen || swapOpen || menuOpen || scene !== null;

  // ── Clip navigation: ←/→/↓ over the moment's sentences ───
  //
  // The sentences live on the episode clock; the adapter tells the hook where
  // the media's t=0 sits on it. A cut clip starts at the window the backend cut
  // it with (`clip.start_ms`); the streamed-episode fallback IS the episode.

  const sentences = useMemo(() => (current ? momentSentences(current) : []), [current]);
  const clipReady = !!current && current.clip.status === "ready" && !!current.clip.video_url;
  const media = useMemo<SentenceMedia | null>(() => {
    if (!current) return null;
    const origin = clipClockOrigin(current);
    if (!origin) return null;
    if (clipReady) return { video: clip.element, seekPlay: clip.seekPlay, ...origin };
    return {
      video: () => fallbackClip.current?.video() ?? null,
      seekPlay: (s) => fallbackClip.current?.seek(s),
      ...origin,
    };
  }, [current, clipReady, clip.element, clip.seekPlay]);
  const nav = useSentencePlayback(sentences, media);

  // ── Mutations (declared up here: the refill effect reads `cardAction.isPending`) ──

  const undoReview = useUndoReview();
  const cardAction = useCardAction();
  const moveCard = useMoveCard();

  // ── Queue loading ────────────────────────────────────────

  const ingest = useCallback((q: SrsQueue, mode: "init" | "append") => {
    const s = stateRef.current;
    const live = new Set(s.queue.map((c) => c.id));
    const retired = new Set(s.retired);
    const usable = (c: SrsCard) => !retired.has(c.id) && !live.has(c.id);
    const cards = q.cards.filter(usable);
    dispatch({
      type: "loaded",
      cards,
      learningSoon: q.learning_soon.filter(usable),
      counts: q.counts,
      serverTime: q.server_time,
      day: q.day,
      mode,
      now: Date.now(),
    });
    return cards.length;
  }, []);

  const loadQueue = useCallback(
    async (extraNew = 0, mode: "init" | "append" = "append"): Promise<number | null> => {
      // `null` = skipped because a fetch is already in flight (StrictMode
      // double-effects, overlapping refills) — never "the server has nothing".
      if (fetchingRef.current) return null;
      fetchingRef.current = true;
      lastFetchRef.current = Date.now();
      setFetching(true);
      try {
        const q = await fetchSrsQueue(qc, QUEUE_LIMIT, extraNew);
        const n = ingest(q, mode);
        if (n > 0) doneRef.current = false;
        return n;
      } catch (e) {
        toast.error("Couldn't load the queue", errMsg(e));
        dispatch({ type: "exhausted" });
        doneRef.current = true;
        return 0;
      } finally {
        fetchingRef.current = false;
        lastFetchRef.current = Date.now();
        setFetching(false);
      }
    },
    [ingest, qc],
  );

  useEffect(() => {
    void loadQueue(0, "init").then((n) => {
      // Nothing at all today → straight to the "all caught up" state; don't
      // let the refill effect ask again.
      if (n === 0 && stateRef.current.queue.length === 0) doneRef.current = true;
    });
  }, [loadQueue]);

  // Refill (§8.3.7) — and one last look before the summary screen. Never while
  // a rating or a menu/key action is still unconfirmed: a queue snapshot the
  // server serves before that write commits still lists the card as due, and it
  // would come straight back (2026-09-03: a just-rated learning step reappeared
  // two cards later). `state.history` is in the deps so the check re-runs when
  // a rating's result lands and `inFlight` has dropped.
  useEffect(() => {
    if (!state.loaded || fetching || inFlight.current > 0 || cardAction.isPending) return;
    if (state.queue.length >= REFILL_AT) return;
    const since = Date.now() - lastFetchRef.current;
    if (state.queue.length === 0) {
      if (doneRef.current || state.learningSoon.length > 0) return;
      void loadQueue().then((n) => {
        if (n === 0) {
          doneRef.current = true;
          dispatch({ type: "exhausted" });
        }
      });
      return;
    }
    if (since < REFILL_COOLDOWN_MS) return;
    void loadQueue();
    // eslint-disable-next-line react-hooks/exhaustive-deps -- `state.history` re-arms the check
  }, [
    state.loaded,
    state.queue.length,
    state.learningSoon.length,
    state.history,
    fetching,
    cardAction.isPending,
    loadQueue,
  ]);

  // Learn-ahead poll: skew-corrected, so a client clock that is minutes off
  // never pulls a learning card in early (§3.4).
  useEffect(() => {
    const t = setInterval(() => dispatch({ type: "learnAhead", now: Date.now() }), LEARN_AHEAD_POLL_MS);
    return () => clearInterval(t);
  }, []);

  // ── Media: one element, `src` swapped per card ───────────

  const currentId = current?.id ?? null;
  const nextClipUrl =
    state.queue[1]?.clip.status === "ready" ? (state.queue[1]?.clip.video_url ?? null) : null;

  useEffect(() => {
    clip.pauseReset();
    if (!current) {
      clip.show(null, false);
      return;
    }
    const ready = current.clip.status === "ready" ? current.clip.video_url : null;
    clip.show(ready, true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [currentId]);

  useEffect(() => {
    if (nextClipUrl) clip.warm(nextClipUrl);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nextClipUrl]);

  // Per-card UI reset + the 200 ms arm delay after the reveal crossfade.
  useEffect(() => {
    setContextOpen(false);
    setTranslationOn(true);
  }, [currentId]);

  useEffect(() => {
    if (state.phase !== "back") {
      setArmed(false);
      return;
    }
    setArmed(false);
    const t = setTimeout(() => setArmed(true), REVEAL_MS + ARM_MS);
    return () => clearTimeout(t);
  }, [state.phase, currentId]);

  // ── Mutations ────────────────────────────────────────────

  const undoSpecific = useCallback(
    (reviewId: number) => {
      undoReview.mutate(
        { review_id: reviewId },
        {
          onSuccess: (res) => {
            const tid = demotionToasts.current.get(res.undone_review_id);
            if (tid != null) {
              toast.dismiss(tid);
              demotionToasts.current.delete(res.undone_review_id);
            }
            dispatch({
              type: "undoneReview",
              card: res.card,
              review_id: res.undone_review_id,
              now: Date.now(),
            });
          },
        },
      );
    },
    [undoReview],
  );

  const review = useReviewCard({
    onReviewed: (result, vars) => {
      inFlight.current = Math.max(0, inFlight.current - 1);
      // A duplicate (a retried client_id) still settles the history entry — the
      // refill guard waits on it — but must not repeat the side effects.
      dispatch({ type: "result", result, client_id: vars.client_id });
      if (result.duplicate) return;
      if (result.demoted) {
        const tid = toast.warning(
          result.message ?? `${result.card.lemma} → back to the stack`,
          "It starts over from the stack with a fresh clip.",
          {
            duration: 9000,
            action: { label: "Undo", hotkey: "Z", onClick: () => undoSpecific(result.review_id) },
          },
        );
        demotionToasts.current.set(result.review_id, tid);
      } else if (result.suspended) {
        toast.warning(
          `${result.card.lemma} was parked`,
          result.message ?? "Too many returns — resume it from the stack when you meet it again.",
        );
      }
    },
    onConflict: (_conflict, vars) => {
      inFlight.current = Math.max(0, inFlight.current - 1);
      dispatch({ type: "conflict", client_id: vars.client_id });
      void loadQueue();
    },
    onFailure: (_error, vars) => {
      inFlight.current = Math.max(0, inFlight.current - 1);
      dispatch({ type: "restoreRated", client_id: vars.client_id, now: Date.now() });
      toast.warning("Rating not saved", "The card is back — retry when you're online.", {
        duration: 12_000,
        action: { label: "Retry", onClick: () => resend(vars) },
      });
    },
  });

  /** Replays the *same* variables (same `client_id`) — the server dedupes. */
  const resend = useCallback(
    (vars: SrsReviewRequest) => {
      const s = stateRef.current;
      const card = s.queue.find((c) => c.id === vars.card_id);
      if (card) {
        dispatch({
          type: "rate",
          card,
          rating: vars.rating,
          client_id: vars.client_id,
          now: Date.now(),
          elapsed_ms: vars.elapsed_ms ?? 0,
        });
      }
      inFlight.current += 1;
      review.mutate(vars);
    },
    [review],
  );

  const rate = useCallback(
    (rating: SrsRating) => {
      const s = stateRef.current;
      const card = s.queue[0];
      if (!card || inFlight.current > 0) return;
      const client_id =
        typeof crypto !== "undefined" && "randomUUID" in crypto
          ? crypto.randomUUID()
          : `${card.id}-${Date.now()}-${Math.random().toString(16).slice(2)}`;
      const vars: SrsReviewRequest = {
        card_id: card.id,
        rating,
        elapsed_ms: Math.max(0, Date.now() - s.shownAt),
        client_id,
      };
      setFlash(rating);
      setTimeout(() => setFlash(null), FLASH_MS);
      setGhost(card);
      setTimeout(() => setGhost(null), REVEAL_MS);
      clip.pauseReset();
      dispatch({
        type: "rate",
        card,
        rating,
        client_id,
        now: Date.now(),
        elapsed_ms: vars.elapsed_ms ?? 0,
      });
      inFlight.current += 1;
      review.mutate(vars);
    },
    [clip, review],
  );

  const undoEntry = useCallback(
    (entry: ActionEntry) => {
      const onSuccess = (res: { card: SrsCard }) =>
        dispatch({ type: "undoAction", entry, card: res.card, now: Date.now() });
      if (entry.kind === "bottom") {
        // The inverse of "bottom" is the card's old slot. `queue_pos` is the
        // dense 1-based stack position; `/srs/stack/move` takes a 0-based index
        // into the list the card has already been removed from — hence the -1
        // (the same arithmetic as the stack page's bulk undo).
        moveCard.mutate(
          { card_id: entry.card.id, position: Math.max(0, (entry.card.queue_pos ?? 1) - 1) },
          { onSuccess },
        );
        return;
      }
      const action: SrsAction = entry.kind === "bury" ? "unbury" : "resume";
      cardAction.mutate({ id: entry.card.id, action }, { onSuccess });
    },
    [cardAction, moveCard],
  );

  /**
   * K / S / B / D — no review row; the session keeps its own undo stack (§3.11).
   * D ("bottom") is a stack verb: it only applies while the card is still `new`,
   * and it does not spend today's allowance — the next new card takes the slot,
   * so the queue is refilled as soon as the server has moved the card.
   */
  const sessionAction = useCallback(
    (kind: ActionEntry["kind"]) => {
      const card = stateRef.current.queue[0];
      if (!card) return;
      if (kind === "bottom" && card.state !== "new") return;
      const action: SrsAction =
        kind === "known" ? "known"
        : kind === "suspend" ? "suspend"
        : kind === "bottom" ? "bottom"
        : "bury";
      const entry: ActionEntry = { kind, card, at: Date.now() };
      setGhost(card);
      setTimeout(() => setGhost(null), REVEAL_MS);
      clip.pauseReset();
      cardAction.mutate(
        { id: card.id, action },
        kind === "bottom" ? { onSuccess: () => void loadQueue() } : undefined,
      );
      dispatch({ type: "remove", entry, now: Date.now() });
      toast({
        title: ACTION_LABEL[kind],
        description: card.lemma,
        action: { label: "Undo", hotkey: "Z", onClick: () => undoEntry(entry) },
      });
    },
    [cardAction, clip, loadQueue, undoEntry],
  );

  /**
   * The same bookkeeping for an action taken from the kebab menu (mouse/touch).
   * `CardMenu` has already applied it server-side, so we only drop the card from
   * the imperative queue — otherwise it would linger until the next rating's 409
   * — and push the Undo entry the keyboard path gets.
   */
  const menuAction = useCallback(
    (card: SrsCard, action: SrsAction) => {
      const kind: ActionEntry["kind"] | null =
        action === "known" ? "known"
        : action === "suspend" ? "suspend"
        : action === "bury" ? "bury"
        : action === "bottom" ? "bottom"
        : null;
      // study_next only repositions the card; it stays answerable.
      if (kind === null && action !== "reject") return;
      if (stateRef.current.queue[0]?.id === card.id) {
        setGhost(card);
        setTimeout(() => setGhost(null), REVEAL_MS);
        clip.pauseReset();
      }
      const entry: ActionEntry = { kind: kind ?? "bury", card, at: Date.now() };
      dispatch({ type: "remove", entry, now: Date.now(), undoable: kind !== null });
      if (kind) {
        toast({
          title: ACTION_LABEL[kind],
          description: card.lemma,
          action: { label: "Undo", hotkey: "Z", onClick: () => undoEntry(entry) },
        });
      }
      // The server has already moved it to the bottom — pull its replacement now.
      if (kind === "bottom") void loadQueue();
    },
    [clip, loadQueue, undoEntry],
  );

  /** A card deleted from the menu is gone for good — drop it, no undo entry. */
  const dropDeleted = useCallback((card: SrsCard) => {
    dispatch({
      type: "remove",
      entry: { kind: "bury", card, at: Date.now() },
      now: Date.now(),
      undoable: false,
    });
  }, []);

  /** Z: the action stack first when it is newer than the last rating (§8.4). */
  const undoLatest = useCallback(() => {
    const s = stateRef.current;
    const top = s.actionStack[s.actionStack.length - 1];
    const lastRating = s.history[s.history.length - 1];
    if (top && (!lastRating || top.at > lastRating.at)) {
      undoEntry(top);
      return;
    }
    undoReview.mutate(
      {},
      {
        onSuccess: (res) => {
          const tid = demotionToasts.current.get(res.undone_review_id);
          if (tid != null) {
            toast.dismiss(tid);
            demotionToasts.current.delete(res.undone_review_id);
          }
          dispatch({
            type: "undoneReview",
            card: res.card,
            review_id: res.undone_review_id,
            now: Date.now(),
          });
        },
      },
    );
  }, [undoEntry, undoReview]);

  // ── Scene playback ───────────────────────────────────────

  const watchScene = useCallback(() => {
    const card = stateRef.current.queue[0];
    if (!card || !card.source_available) return;
    const m = toMoment(card);
    if (!m) return;
    clip.pauseReset();
    setScene(m);
  }, [clip]);

  const playExtended = useCallback(() => {
    const card = stateRef.current.queue[0];
    if (!card || !card.source_available || card.extend.length === 0) return;
    const m = toMoment(card, momentWindow(card));
    if (!m) return;
    clip.pauseReset();
    setScene(m);
  }, [clip]);

  // ── Keyboard map (§8.4) ──────────────────────────────────

  const exit = useCallback(() => navigate("/study"), [navigate]);
  const reveal = useCallback(() => dispatch({ type: "reveal" }), []);

  const cycleFurigana = useCallback(() => {
    const order = ["none", "target", "all"] as const;
    const i = order.indexOf(prefs.frontFurigana);
    setPrefs({ frontFurigana: order[(i + 1) % order.length] });
  }, [prefs.frontFurigana, setPrefs]);

  const phase = state.phase;
  const good = useCallback(() => {
    const p = stateRef.current.phase;
    if (p === "front") reveal();
    else if (p === "back" && armed) rate(3);
  }, [armed, rate, reveal]);

  const grade = useCallback(
    (rating: SrsRating) => {
      const s = stateRef.current;
      const p = s.phase;
      // First exposure: 1/2 are inert either side of the reveal (§3.3) — there
      // is nothing to have forgotten yet.
      const lesson = s.queue[0]?.state === "new";
      if (p === "front") {
        // Again is unambiguous from the front — except on a lesson card, where
        // the front is just "read it".
        if (rating === 1 && !lesson) rate(1);
        else reveal();
        return;
      }
      if (p !== "back" || !armed) return;
      if (lesson) {
        if (rating === 3 || rating === 4) rate(rating);
        return;
      }
      if (prefs.ratingMode === "two" && (rating === 2 || rating === 4)) return;
      rate(rating);
    },
    [armed, prefs.ratingMode, rate, reveal],
  );

  // Space belongs to the clip (the user, 2026-09-03: "unbind it and bind to
  // start/pause video clip"): pause, resume, or start a finished clip over —
  // exactly what a click on the picture does. Enter is the forward key: reveal,
  // then Good. While the clip is still cutting the stage shows the episode
  // stream instead, so Space has to reach that element.
  const toggleClip = useCallback(() => {
    if (stateRef.current.queue[0]?.clip.status === "ready") {
      clip.togglePlay();
      return;
    }
    const v = fallbackClip.current?.video();
    if (!v) return;
    if (v.paused) void v.play().catch(() => undefined);
    else v.pause();
  }, [clip]);

  const hotkeys = useMemo<HotkeyMap>(
    () => ({
      space: toggleClip,
      enter: () => {
        if (stateRef.current.phase === "done") exit();
        else good();
      },
      // The arrows are the clip's, and only the clip's (Migaku's subtitle keys):
      // ← previous sentence · → next sentence · ↓ replay the current one.
      left: nav.prev,
      right: nav.next,
      down: nav.replay,
      "1": () => grade(1),
      "2": () => grade(2),
      "3": () => grade(3),
      "4": () => grade(4),
      // R replays the *whole* clip — which now spans the evidence lines as well
      // as the target sentence. While the clip is still cutting the stage shows
      // the episode stream instead, so R has to reach that element.
      r: () => {
        if (stateRef.current.queue[0]?.clip.status === "ready") clip.replay();
        else fallbackClip.current?.replay();
      },
      a: () => clip.replay(true),
      f: cycleFurigana,
      // The translation is part of the answer now, so T from the front is a
      // reveal; on the back it toggles the sentence translation as before.
      t: () => {
        if (stateRef.current.phase === "front") reveal();
        else setTranslationOn((v) => !v);
      },
      c: () => {
        if (stateRef.current.phase === "front") reveal();
        setContextOpen((v) => !v);
      },
      w: watchScene,
      // The moment player prints the line's translation, so from the front it
      // would leak straight past the blur — reveal first, like C does.
      x: () => {
        if (stateRef.current.phase === "front") reveal();
        playExtended();
      },
      k: () => sessionAction("known"),
      s: () => sessionAction("suspend"),
      b: () => sessionAction("bury"),
      // D = "not now": a new card goes back to the bottom of the stack.
      d: () => sessionAction("bottom"),
      e: () => setEditOpen(true),
      m: () => setSwapOpen(true),
      z: undoLatest,
      "?": () => setShortcutsOpen(true),
      esc: exit,
    }),
    [
      clip,
      cycleFurigana,
      exit,
      good,
      grade,
      nav.next,
      nav.prev,
      nav.replay,
      playExtended,
      reveal,
      sessionAction,
      toggleClip,
      undoLatest,
      watchScene,
    ],
  );

  useHotkeys(hotkeys, !dialogOpen);

  // ── Render ───────────────────────────────────────────────

  const demoteAfter = settings?.demote_after_fails ?? 2;
  const againWarns =
    !!current && demoteAfter > 0 && current.fail_count + 1 >= demoteAfter && current.state !== "new";
  const busy = review.isPending || !armed;

  const chips = (
    <SessionProgress
      counts={state.counts}
      reviewed={state.stats.reviewed}
      serving={!!current}
      becameKnown={state.stats.becameKnown}
      onExit={exit}
      onShortcuts={() => setShortcutsOpen(true)}
      menu={
        current ? (
          <CardMenu
            card={current}
            align="end"
            showHotkeys
            onOpenChange={handleMenuOpen}
            onEdit={() => setEditOpen(true)}
            onSwapMoment={() => setSwapOpen(true)}
            onAfterAction={menuAction}
            onDeleted={() => dropDeleted(current)}
          />
        ) : undefined
      }
    />
  );

  const dialogs = current ? (
    <>
      <EditCardDialog
        card={current}
        open={editOpen}
        onOpenChange={setEditOpen}
        onSaved={(c) => dispatch({ type: "replaceCard", card: c })}
      />
      <SwapMomentDialog
        card={current}
        open={swapOpen}
        onOpenChange={setSwapOpen}
        onSaved={(c) => dispatch({ type: "replaceCard", card: c })}
      />
    </>
  ) : null;

  const sheet = (
    <ShortcutsSheet
      open={shortcutsOpen}
      onOpenChange={setShortcutsOpen}
      prefs={prefs}
      setPrefs={setPrefs}
    />
  );

  if (phase === "loading") {
    return (
      <div className="flex h-[100dvh] flex-col overflow-hidden">
        {chips}
        <div className="flex min-h-0 flex-1 items-center justify-center gap-2 text-sm text-muted">
          <Loader2 className="size-4 animate-spin" /> Building the queue…
        </div>
        {sheet}
      </div>
    );
  }

  if (phase === "done" || !current) {
    return (
      <div className="flex h-[100dvh] flex-col overflow-hidden">
        {chips}
        <div className="flex min-h-0 flex-1 items-center justify-center overflow-y-auto">
          <SessionSummary
            stats={state.stats}
            streakDays={summary?.streak_days ?? null}
            nextDueAt={summary?.next_due_at ?? null}
            canAddNew={(state.counts?.new_stack_total ?? summary?.new_stack_total ?? 0) > 0}
            loadingMore={fetching}
            onAddNew={() => {
              doneRef.current = false;
              void loadQueue(5);
            }}
            onBackToDeck={exit}
            onManageStack={() => navigate("/study/stack")}
          />
        </div>
        {sheet}
      </div>
    );
  }

  // A first exposure: same front → reveal → back as any other card, only the
  // label and the action bar differ.
  const lesson = current.state === "new";
  const revealed = phase === "back";

  return (
    <div
      ref={sessionRef}
      tabIndex={-1}
      className="flex h-[100dvh] flex-col overflow-hidden bg-bg focus:outline-none"
    >
      {chips}

      {/* `m-auto` rather than `justify-center`: a centred flex child stays fully
          scrollable when it is taller than the box, `justify-content:center`
          clips its top. */}
      <div className="flex min-h-0 flex-1 flex-col overflow-y-auto px-1 py-4 sm:px-4">
        {/* `grid-cols-1` (= minmax(0,1fr)) matters: an implicit `auto` column
            grows to the widest nowrap line in the card (the truncated meaning),
            and the clip stretched with it past the edge of a phone screen. */}
        <div className="m-auto grid w-full max-w-3xl grid-cols-1 gap-5 lg:max-w-6xl lg:grid-cols-2 lg:items-center lg:gap-8">
          <ClipStage
            card={current}
            clip={clip}
            onWatchScene={watchScene}
            fallbackRef={fallbackClip}
            sentences={sentences}
            nav={media ? nav : undefined}
            className="lg:sticky lg:top-4"
          />

          <div className="relative">
            {ghost && (
              <div className="card-exit pointer-events-none absolute inset-0 z-10 bg-bg px-1">
                <TokenLine
                  tokens={ghost.tokens}
                  text={ghost.text}
                  target_surface={ghost.target_surface}
                  furigana="none"
                  className="text-xl opacity-60"
                />
              </div>
            )}

            <div key={current.id} className="card-enter">
              <ReviewCard
                card={current}
                phase={revealed ? "back" : "front"}
                furigana={prefs.frontFurigana}
                showTranslation={translationOn}
                contextOpen={contextOpen}
                onContextOpenChange={setContextOpen}
                onReveal={reveal}
                onWatchScene={current.source_available ? watchScene : undefined}
                onPlayExtended={
                  current.source_available && current.extend.length > 0 ? playExtended : undefined
                }
                onEdit={() => setEditOpen(true)}
                sentences={sentences}
                activeSentence={nav.activeIndex}
                playing={nav.playing}
                onSeekSentence={media ? nav.seekTo : undefined}
              />
            </div>
          </div>
        </div>
      </div>

      {revealed && lesson ? (
        <LessonActions
          disabled={busy}
          onGotIt={() => rate(3)}
          onAlreadyKnow={() => rate(4)}
        />
      ) : revealed ? (
        <RatingBar
          preview={current.preview}
          ratingMode={prefs.ratingMode}
          disabled={busy}
          againWarns={againWarns}
          flash={flash}
          onRate={rate}
        />
      ) : (
        <div className="sticky bottom-0 z-20 border-t border-border/70 bg-bg/90 px-3 pt-3 pb-[max(0.75rem,env(safe-area-inset-bottom))] backdrop-blur-md">
          <button
            type="button"
            onClick={reveal}
            className="mx-auto flex h-12 w-full max-w-4xl items-center justify-center rounded-xl border border-border-strong bg-surface text-sm font-medium text-fg transition-colors hover:bg-surface-hover"
          >
            {lesson ? "Show the meaning" : "Show the answer"}
          </button>
          <p className="mt-1.5 text-center text-[0.65rem] text-faint">
            Enter · reveal · Space · pause / play · ← → sentences · ↓ replay · R whole clip · ? shortcuts
          </p>
        </div>
      )}

      {scene && <MomentPlayer moment={scene} onClose={() => setScene(null)} />}
      {dialogs}
      {sheet}
    </div>
  );
}
