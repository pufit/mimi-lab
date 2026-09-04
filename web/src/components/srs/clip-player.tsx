import {
  forwardRef,
  useCallback,
  useEffect,
  useImperativeHandle,
  useRef,
  useState,
} from "react";
import type { ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { Loader2, RotateCcw, TriangleAlert, VideoOff } from "lucide-react";
import { api, ApiError } from "@/lib/api";
import { momentWindow } from "@/lib/srs-extend";
import { cn } from "@/lib/utils";
import type { SrsCard, SrsClip } from "@/lib/srs-types";
import type { Moment } from "@/lib/types";

/**
 * Everything the fallback path needs: which episode to stream and which slice
 * of it is the moment. `source_available === false` means the episode file is
 * gone from disk, so there is nothing to fall back to.
 */
export interface ClipPlayerSource {
  episode_id: number | null;
  start_ms: number;
  end_ms: number;
  source_available?: boolean;
  /**
   * The window already carries the lead/tail pad (it came from the cut clip),
   * so the player must not add its own on top — that would drift the fallback
   * out of sync with the clip the same card plays once it is ready.
   */
  padded?: boolean;
}

/**
 * `clipSource(card)` — the adapter every caller uses.
 *
 * The window is the *moment*, not the target cue: a card whose meaning leans on
 * neighbouring lines is cut to cover them, and the fallback episode stream has
 * to seek and stop at exactly the same places (evidence amendment §2).
 */
export function clipSource(
  card: Pick<SrsCard, "episode_id" | "start_ms" | "end_ms" | "source_available" | "extend" | "clip">,
): ClipPlayerSource {
  const w = momentWindow(card);
  return {
    episode_id: card.episode_id,
    start_ms: w.start_ms,
    end_ms: w.end_ms,
    source_available: card.source_available,
    padded: w.padded,
  };
}

/**
 * `MomentPlayer` adapter (design §8.10) — lets a card open the "watch the
 * scene" portal, which streams the episode around the line.
 */
export function toMoment(card: SrsCard): Moment {
  return {
    line_id: card.line_id ?? 0,
    anilist_id: card.anilist_id ?? 0,
    episode_id: card.episode_id ?? 0,
    title: card.show_title,
    ep_number: card.ep_number,
    text: card.text,
    text_furigana: card.text_furigana,
    translation: card.translation,
    start_ms: card.start_ms,
    end_ms: card.end_ms,
  };
}

export interface ClipPlayerHandle {
  /** Restart the moment from the top (also what the overlay button does). */
  replay: () => void;
  /** Seek to a media time (seconds) and play; re-arms the fallback's stop watcher. */
  seek: (seconds: number) => void;
  /** The live element — the sentence clock reads `currentTime` off it. */
  video: () => HTMLVideoElement | null;
}

export interface ClipPlayerProps {
  clip: SrsClip;
  /** Card moment; enables the episode-stream fallback while the clip is pending. */
  source?: ClipPlayerSource | null;
  autoplay?: boolean;
  controls?: boolean;
  muted?: boolean;
  onEnded?: () => void;
  className?: string;
  /** Audio-only mode: the element still plays, it is just not painted. */
  hidden?: boolean;
  /** Extra classes for the `<video>` itself (the wrapper takes `className`). */
  videoClassName?: string;
}

/** Start a touch early / linger a touch late so no mora is clipped (MomentPlayer). */
export const LEAD_MS = 250;
const TAIL_MS = 150;

const STATUS_TEXT: Record<SrsClip["status"], string> = {
  pending: "Clip still cutting",
  ready: "",
  failed: "Clip failed",
  no_source: "Episode file is gone",
  missing: "Clip file is missing",
};

/**
 * The one video surface for an SRS card.
 *
 * Plays `clip.mp4` with its poster when the clip is ready; while the clip is
 * still being cut (or when it failed) it streams the original episode
 * pre-seeked to the line and auto-pauses at its end, exactly like
 * `MomentPlayer`. It **never** reports watch progress — a four-second sentence
 * replay must not clobber an episode's resume position.
 */
export const ClipPlayer = forwardRef<ClipPlayerHandle, ClipPlayerProps>(function ClipPlayer(
  {
    clip,
    source,
    autoplay = false,
    controls = true,
    muted = false,
    onEnded,
    className,
    hidden = false,
    videoClassName,
  },
  ref,
) {
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const armedRef = useRef(true);
  const [ended, setEnded] = useState(false);

  const clipReady = clip.status === "ready" && !!clip.video_url;
  const canStream = !!source?.episode_id && source.source_available !== false;
  const streaming = !clipReady && canStream;

  const lead = source?.padded ? 0 : LEAD_MS;
  const tail = source?.padded ? 0 : TAIL_MS;
  const startS = source ? Math.max(0, source.start_ms - lead) / 1000 : 0;
  const stopS = source ? (Math.max(source.end_ms, source.start_ms + 500) + tail) / 1000 : 0;

  const {
    data: stream,
    isLoading: streamLoading,
    isError: streamError,
    error: streamErr,
  } = useQuery({
    // Signed media URLs are short-lived: never cache, never re-serve.
    queryKey: ["srs", "clip-fallback", source?.episode_id],
    queryFn: () => api.browserPlay(source!.episode_id!),
    enabled: streaming,
    retry: false,
    staleTime: 0,
    gcTime: 0,
  });

  // Stop watcher for the streamed fallback: pause once at the end of the line,
  // then disarm so the native control can keep watching the episode.
  useEffect(() => {
    if (!streaming || !stopS) return;
    let raf = 0;
    const tick = () => {
      const v = videoRef.current;
      if (v && armedRef.current && !v.paused && v.currentTime >= stopS) {
        v.pause();
        armedRef.current = false;
        setEnded(true);
        onEnded?.();
      }
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
    // `onEnded` is intentionally not a dependency: callers pass inline closures.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [streaming, stopS]);

  // A new clip version (or a swapped moment) resets the replay overlay.
  useEffect(() => {
    setEnded(false);
    armedRef.current = true;
  }, [clip.video_url, clip.version, source?.start_ms]);

  const replay = useCallback(() => {
    const v = videoRef.current;
    if (!v) return;
    armedRef.current = true;
    setEnded(false);
    v.currentTime = streaming ? startS : 0;
    // Replay is a user gesture, so `play()` is allowed; a rejection during
    // autoplay just leaves a paused, seeked frame.
    void v.play().catch(() => {});
  }, [streaming, startS]);

  const seek = useCallback((seconds: number) => {
    const v = videoRef.current;
    if (!v) return;
    armedRef.current = true;
    setEnded(false);
    try {
      v.currentTime = Math.max(0, seconds);
    } catch {
      /* not seekable yet */
    }
    void v.play().catch(() => {});
  }, []);

  useImperativeHandle(ref, () => ({ replay, seek, video: () => videoRef.current }), [replay, seek]);

  const src = clipReady ? clip.video_url! : (stream?.video_url ?? null);
  const poster = clip.poster_url ?? undefined;

  const frame = (body: ReactNode) => (
    <div
      className={cn(
        "relative overflow-hidden rounded-xl border border-border bg-black",
        hidden && "sr-only",
        className,
      )}
    >
      {body}
    </div>
  );

  // Nothing to play: no clip file and no episode on disk (or the stream 404'd).
  if (!src) {
    const failed = streamError || (!streaming && !clipReady);
    const detail =
      streamError && streamErr instanceof ApiError && streamErr.status === 404
        ? "The episode file is no longer on disk."
        : clip.error;
    return frame(
      <div
        className="grid aspect-video w-full place-items-center bg-cover bg-center"
        style={poster ? { backgroundImage: `url(${poster})` } : undefined}
      >
        <div className="flex flex-col items-center gap-2 rounded-lg bg-bg/75 px-4 py-3 text-center backdrop-blur-sm">
          {clip.status === "pending" && !failed ? (
            <Loader2 className="size-5 animate-spin text-brand-bright" />
          ) : clip.status === "no_source" ? (
            <VideoOff className="size-5 text-faint" />
          ) : (
            <TriangleAlert className="size-5 text-comp-amber" />
          )}
          <p className="text-xs font-medium text-muted">
            {streamLoading ? "Preparing stream…" : STATUS_TEXT[clip.status] || "No clip"}
          </p>
          {detail && <p className="max-w-xs text-[0.65rem] leading-snug text-faint">{detail}</p>}
        </div>
      </div>,
    );
  }

  return frame(
    <>
      <video
        ref={videoRef}
        key={streaming ? `stream-${source?.episode_id}` : `clip-${clip.version}-${src}`}
        src={src}
        poster={poster}
        controls={controls}
        // Chrome's ⋮ overflow menu (download / PiP / playback rate) belongs to
        // no part of this product; strip it wherever the native chrome is shown.
        controlsList="nodownload noplaybackrate noremoteplayback"
        disablePictureInPicture
        disableRemotePlayback
        muted={muted}
        autoPlay={autoplay}
        preload="metadata"
        playsInline
        crossOrigin={streaming ? "anonymous" : undefined}
        className={cn("w-full bg-black object-contain", videoClassName)}
        onLoadedMetadata={(e) => {
          if (!streaming) return;
          e.currentTarget.currentTime = startS;
          if (autoplay) void e.currentTarget.play().catch(() => {});
        }}
        onEnded={() => {
          if (streaming) return; // the stop watcher owns the fallback path
          setEnded(true);
          onEnded?.();
        }}
      />

      {streaming && (
        <span className="pointer-events-none absolute left-2 top-2 rounded-md bg-black/70 px-2 py-1 text-[0.65rem] font-medium text-muted backdrop-blur-sm">
          {clip.status === "pending" ? "Clip still cutting — streaming the episode" : "Streaming the episode"}
        </span>
      )}

      {ended && (
        <button
          type="button"
          onClick={replay}
          className="absolute inset-0 grid place-items-center bg-black/45 backdrop-blur-[1px] transition-colors hover:bg-black/55 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-brand"
          aria-label="Replay the clip"
        >
          <span className="flex items-center gap-2 rounded-full border border-border-strong bg-bg-elevated/90 px-4 py-2 text-sm font-medium text-fg shadow-lg">
            <RotateCcw className="size-4 text-brand-bright" />
            Replay
          </span>
        </button>
      )}
    </>,
  );
});
