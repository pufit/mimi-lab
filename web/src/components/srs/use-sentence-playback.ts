// Sentence navigation over one media clock (clip navigation, 2026-09-03).
//
// Migaku's subtitle keys on a card's clip: ← previous sentence, → next
// sentence, ↓ replay the current one. The hook does not own a `<video>` — the
// review session keeps one persistent element (`useClipElement`) and falls back
// to a streamed episode (`ClipPlayer`) while a clip is still cutting — so it is
// handed a small adapter (`SentenceMedia`) and reads the clock through it.
//
// The *media* is the single source of truth: `activeIndex` is derived from
// `currentTime` (state only changes at a sentence boundary or when playback
// starts/stops), and a seek just moves the clock — the highlight follows. The
// clock is read on the element's own events (`timeupdate` ~4 Hz, `seeked`,
// `play`/`pause`/`ended`) plus a 250 ms heartbeat, so it keeps working in a
// background tab or a headless page where `requestAnimationFrame` is frozen;
// an rAF loop runs on top only to make the hand-over between two sentences
// land on the exact frame when the page is visible. Playback continues to the
// end of the clip after a seek, as in Migaku: to hear one sentence again, press
// ↓ again.

import { useCallback, useEffect, useRef, useState } from "react";
import { LEAD_MS as STREAM_LEAD_MS } from "@/components/srs/clip-player";
import { momentWindow } from "@/lib/srs-extend";
import { activeSentenceIndex } from "@/lib/srs-sentences";
import type { Sentence } from "@/lib/srs-sentences";
import type { SrsCard } from "@/lib/srs-types";

/** Landing on a sentence starts a touch early so its first mora is not clipped. */
export const SEEK_LEAD_MS = 150;

export interface SentenceMedia {
  /** The element the moment plays in — `null` while nothing is mounted/loaded. */
  video: () => HTMLVideoElement | null;
  /** Seek to a media time (seconds) and play. */
  seekPlay: (seconds: number) => void;
  /** Episode ms that the media's `t = 0` corresponds to (a cut clip starts at
   *  `clip.start_ms`; the streamed episode at 0). */
  originMs: number;
  /** Episode ms where the playable window starts — the first sentence rewinds
   *  here, so ↓ on a single-cue card is exactly R. */
  floorMs: number;
}

/**
 * Where a card's media sits on the episode clock. A ready clip is the file cut
 * from `clip.start_ms` (its `t = 0`); otherwise the episode itself is streamed
 * (`ClipPlayer`), pre-seeked a touch before the moment. `null` when there is
 * nothing to play at all.
 */
export function clipClockOrigin(
  card: Pick<SrsCard, "clip" | "source_available" | "episode_id" | "start_ms" | "end_ms" | "extend">,
): Pick<SentenceMedia, "originMs" | "floorMs"> | null {
  if (card.clip.status === "ready" && card.clip.video_url) {
    const origin = card.clip.start_ms ?? momentWindow(card).start_ms;
    return { originMs: origin, floorMs: origin };
  }
  if (!card.source_available || card.episode_id == null) return null;
  const w = momentWindow(card);
  return { originMs: 0, floorMs: Math.max(0, w.start_ms - (w.padded ? 0 : STREAM_LEAD_MS)) };
}

export interface SentencePlayback {
  /** Index of the sentence playing (or paused on); `-1` = no clock to read. */
  activeIndex: number;
  /** The media is actually running (drives the "live" bars). */
  playing: boolean;
  /** There is a sentence after the current one. */
  canNext: boolean;
  /** ← — previous sentence; on the first one it rewinds to the start. */
  prev: () => void;
  /** → — next sentence; no-op on the last one. */
  next: () => void;
  /** ↓ — the current sentence from its start. */
  replay: () => void;
  /** Play from sentence `index` (a click on a row or a strip block). */
  seekTo: (index: number) => void;
}

interface Clock {
  activeIndex: number;
  playing: boolean;
}

const IDLE: Clock = { activeIndex: -1, playing: false };

/** Element events that move the clock; each one re-reads it. */
const CLOCK_EVENTS = [
  "timeupdate",
  "seeking",
  "seeked",
  "play",
  "playing",
  "pause",
  "ended",
  "loadedmetadata",
  "emptied",
] as const;

/** Heartbeat for pages where rAF is frozen (background tab, headless). */
const HEARTBEAT_MS = 250;

export function useSentencePlayback(
  sentences: Sentence[],
  media: SentenceMedia | null,
): SentencePlayback {
  const [clock, setClock] = useState<Clock>(IDLE);
  const mediaRef = useRef(media);
  mediaRef.current = media;
  const sentencesRef = useRef(sentences);
  sentencesRef.current = sentences;

  // The clock: read `currentTime` on the element's events, on a heartbeat and
  // on every animation frame; publish only on change. The element may be
  // swapped under us (the streamed fallback re-keys its `<video>`), so the
  // listeners follow whatever `media.video()` returns.
  useEffect(() => {
    if (!media || sentences.length === 0) {
      setClock((c) => (c.activeIndex === -1 && !c.playing ? c : IDLE));
      return;
    }
    let raf = 0;
    let bound: HTMLVideoElement | null = null;
    let last: Clock = { activeIndex: -2, playing: false };

    const read = () => {
      const v = media.video();
      if (v !== bound) {
        if (bound) for (const e of CLOCK_EVENTS) bound.removeEventListener(e, onEvent);
        bound = v;
        if (v) for (const e of CLOCK_EVENTS) v.addEventListener(e, onEvent);
      }
      let next: Clock = IDLE;
      if (v) {
        next = {
          activeIndex: activeSentenceIndex(sentences, v.currentTime * 1000 + media.originMs),
          playing: !v.paused && !v.ended,
        };
      }
      if (next.activeIndex !== last.activeIndex || next.playing !== last.playing) {
        last = next;
        setClock(next);
      }
    };
    const onEvent = () => read();
    const frame = () => {
      read();
      raf = requestAnimationFrame(frame);
    };

    read();
    raf = requestAnimationFrame(frame);
    const beat = window.setInterval(read, HEARTBEAT_MS);
    return () => {
      cancelAnimationFrame(raf);
      window.clearInterval(beat);
      if (bound) for (const e of CLOCK_EVENTS) bound.removeEventListener(e, onEvent);
    };
  }, [media, sentences]);

  /** The index right now — read off the clock, not the (one frame old) state. */
  const liveIndex = useCallback((): number => {
    const m = mediaRef.current;
    const list = sentencesRef.current;
    if (!m || list.length === 0) return -1;
    const v = m.video();
    if (!v) return 0;
    return activeSentenceIndex(list, v.currentTime * 1000 + m.originMs);
  }, []);

  const seekTo = useCallback((index: number) => {
    const m = mediaRef.current;
    const list = sentencesRef.current;
    if (!m || list.length === 0) return;
    const k = Math.min(list.length - 1, Math.max(0, index));
    const floorS = Math.max(0, (m.floorMs - m.originMs) / 1000);
    const startS = (list[k].start_ms - m.originMs - SEEK_LEAD_MS) / 1000;
    m.seekPlay(k === 0 ? floorS : Math.max(floorS, startS));
  }, []);

  const prev = useCallback(() => {
    const i = liveIndex();
    if (i < 0) return;
    seekTo(Math.max(0, i - 1));
  }, [liveIndex, seekTo]);

  const next = useCallback(() => {
    const i = liveIndex();
    if (i < 0 || i >= sentencesRef.current.length - 1) return;
    seekTo(i + 1);
  }, [liveIndex, seekTo]);

  const replay = useCallback(() => {
    const i = liveIndex();
    if (i < 0) return;
    seekTo(i);
  }, [liveIndex, seekTo]);

  return {
    activeIndex: clock.activeIndex,
    playing: clock.playing,
    canNext: clock.activeIndex >= 0 && clock.activeIndex < sentences.length - 1,
    prev,
    next,
    replay,
    seekTo,
  };
}
