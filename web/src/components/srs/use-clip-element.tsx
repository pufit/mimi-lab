// One persistent <video> for the whole review session (design §8.3, audio contract).
//
// iOS Safari unlocks unmuted playback *per media element*, and only after that
// element was played from a user gesture; a fresh <video> per card would show
// the Play overlay again on every card. So the session creates exactly one
// element, keeps it alive for its whole lifetime and only swaps `src`:
//
//     el.src = video_url; el.load(); el.play()
//
// A second, hidden element warms the next clip (`src` + `load()`, plus a
// `fetch()` into the HTTP cache on iOS, which ignores `preload="auto"`).
//
// The element is moved between mount points with `appendChild` (a DOM move, not
// a re-creation), so the page keeps it mounted in a stable slot across cards.
//
// The window is the file's own: `clip.mp4` is cut to the whole moment — the
// evidence lines the meaning leans on, the target sentence, and any
// continuation — so this hook deliberately holds *no* start/stop times and
// never clamps playback to the target cue. `show()` plays from 0 to the end of
// the file and `replay()` (R) rewinds to 0; widening the moment is entirely a
// backend concern (`app/srs/clips.py window()`).

import { useCallback, useEffect, useRef, useState } from "react";

const IS_IOS =
  typeof navigator !== "undefined" &&
  (/iP(hone|od|ad)/.test(navigator.userAgent) ||
    (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1));

export interface ClipElement {
  /** Callback ref for the box the persistent `<video>` lives in. */
  mount: (node: HTMLDivElement | null) => void;
  /** Point the element at a clip; `autoplay` tries to start it right away. */
  show: (url: string | null, autoplay?: boolean) => void;
  /** Replay from the top (R); `audioOnly` hides the picture (A). */
  replay: (audioOnly?: boolean) => void;
  /** Seek to a media time (seconds) and play — ←/→/↓ land on a sentence. */
  seekPlay: (seconds: number) => void;
  /** Click on the picture: pause, or resume (from the top once it has ended). */
  togglePlay: () => void;
  /** Pause + rewind — called before the next card's `src` is assigned. */
  pauseReset: () => void;
  /** The live element (`null` before the first mount) — the sentence clock reads it. */
  element: () => HTMLVideoElement | null;
  /** Pre-load the next clip into the hidden warm element. */
  warm: (url: string | null) => void;
  /** `play()` was rejected by the autoplay policy → show the Play overlay. */
  needsGesture: boolean;
  /** The current clip played to its end (replay affordance). */
  ended: boolean;
  /** Not playing right now — the stage shows a play glyph when this is a *pause*
   *  (i.e. not `ended`, not `needsGesture`). */
  paused: boolean;
  /** Picture hidden (A = audio only); reset on every new clip. */
  audioOnly: boolean;
}

function createVideo(): HTMLVideoElement {
  const el = document.createElement("video");
  el.playsInline = true;
  el.setAttribute("playsinline", "");
  el.setAttribute("webkit-playsinline", "");
  el.preload = "auto";
  el.controls = false;
  el.style.display = "block";
  el.style.width = "100%";
  el.style.maxWidth = "100%";
  el.style.maxHeight = "42vh";
  el.style.objectFit = "contain";
  el.style.backgroundColor = "#000";
  return el;
}

export function useClipElement(): ClipElement {
  const elRef = useRef<HTMLVideoElement | null>(null);
  const warmRef = useRef<HTMLVideoElement | null>(null);
  const srcRef = useRef<string | null>(null);
  const [needsGesture, setNeedsGesture] = useState(false);
  const [ended, setEnded] = useState(false);
  const [paused, setPaused] = useState(true);
  const [audioOnly, setAudioOnly] = useState(false);

  const element = useCallback((): HTMLVideoElement => {
    if (!elRef.current) {
      const el = createVideo();
      el.addEventListener("ended", () => setEnded(true));
      el.addEventListener("playing", () => {
        setEnded(false);
        setNeedsGesture(false);
      });
      // `play` fires the moment `play()` is called (before any frame), `pause`
      // on every pause including the one at the end of the file.
      el.addEventListener("play", () => setPaused(false));
      el.addEventListener("pause", () => setPaused(true));
      elRef.current = el;
    }
    return elRef.current;
  }, []);

  const mount = useCallback(
    (node: HTMLDivElement | null) => {
      if (!node) return; // keep the element alive; it is re-appended on the next mount
      const el = element();
      if (el.parentElement !== node) node.appendChild(el);
      if (!warmRef.current) {
        const warm = createVideo();
        warm.muted = true;
        warm.style.display = "none";
        warmRef.current = warm;
      }
      if (warmRef.current.parentElement !== node) node.appendChild(warmRef.current);
    },
    [element],
  );

  const tryPlay = useCallback(
    (el: HTMLVideoElement) => {
      const p = el.play();
      if (p && typeof p.catch === "function") {
        p.then(() => setNeedsGesture(false)).catch(() => setNeedsGesture(true));
      }
    },
    [],
  );

  const show = useCallback(
    (url: string | null, autoplay = true) => {
      const el = element();
      setEnded(false);
      setAudioOnly(false);
      el.style.visibility = "visible";
      if (!url) {
        srcRef.current = null;
        el.removeAttribute("src");
        el.load();
        return;
      }
      if (srcRef.current !== url) {
        srcRef.current = url;
        el.src = url;
        el.load();
      } else {
        el.currentTime = 0;
      }
      if (autoplay) tryPlay(el);
    },
    [element, tryPlay],
  );

  const replay = useCallback(
    (hidePicture = false) => {
      const el = element();
      setAudioOnly(hidePicture);
      el.style.visibility = hidePicture ? "hidden" : "visible";
      setEnded(false);
      try {
        el.currentTime = 0;
      } catch {
        /* not seekable yet */
      }
      tryPlay(el);
    },
    [element, tryPlay],
  );

  const seekPlay = useCallback(
    (seconds: number) => {
      const el = element();
      setEnded(false);
      try {
        el.currentTime = Math.max(0, seconds);
      } catch {
        /* not seekable yet */
      }
      tryPlay(el);
    },
    [element, tryPlay],
  );

  const togglePlay = useCallback(() => {
    const el = element();
    if (!srcRef.current) return;
    if (!el.paused) {
      el.pause();
      return;
    }
    // A click on a finished clip starts it over; on a paused one it resumes.
    if (el.ended) replay();
    else tryPlay(el);
  }, [element, replay, tryPlay]);

  const pauseReset = useCallback(() => {
    const el = elRef.current;
    if (!el) return;
    el.pause();
    try {
      el.currentTime = 0;
    } catch {
      /* not seekable */
    }
  }, []);

  const getElement = useCallback(() => elRef.current, []);

  const warm = useCallback((url: string | null) => {
    const el = warmRef.current;
    if (!el || !url) return;
    if (el.getAttribute("src") === url) return;
    el.src = url;
    el.load();
    if (IS_IOS) void fetch(url).catch(() => undefined);
  }, []);

  // Release the media on unmount — an element left with a `src` keeps the
  // connection (and the decoder) alive after the session is over.
  useEffect(() => {
    return () => {
      const el = elRef.current;
      if (el) {
        el.pause();
        el.removeAttribute("src");
        el.load();
        el.remove();
      }
      const wm = warmRef.current;
      if (wm) {
        wm.removeAttribute("src");
        wm.load();
        wm.remove();
      }
      elRef.current = null;
      warmRef.current = null;
    };
  }, []);

  return {
    mount,
    show,
    replay,
    seekPlay,
    togglePlay,
    pauseReset,
    warm,
    element: getElement,
    needsGesture,
    ended,
    paused,
    audioOnly,
  };
}
