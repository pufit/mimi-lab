import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useQuery } from "@tanstack/react-query";
import { ExternalLink, Loader2, RotateCcw, X } from "lucide-react";
import { api, ApiError } from "@/lib/api";
import { usePlayMoment } from "@/lib/hooks";
import { Button } from "@/components/ui/button";
import { Furigana } from "@/components/furigana";
import { toast } from "@/components/ui/toast";
import { formatMs } from "@/lib/utils";
import type { Moment } from "@/lib/types";

/** Start a touch before the line so the first mora isn't clipped. */
const LEAD_MS = 250;
/** Linger a touch past end_ms so the last mora isn't cut. */
const TAIL_MS = 150;

/**
 * Sentence player — click a Moment, hear that line.
 *
 * Streams the episode straight from the server (same signed-URL endpoint as
 * the fallback player), pre-seeked to the subtitle line; playback auto-pauses
 * at the end of the line. Pressing play again just keeps watching — the stop
 * watcher disarms after firing. Deliberately does NOT report watch progress:
 * a 4-second sentence replay must never clobber an episode's resume position
 * or trip the auto-watched heuristic.
 */
export function MomentPlayer({ moment, onClose }: { moment: Moment; onClose: () => void }) {
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const armedRef = useRef(true);
  const [lineEnded, setLineEnded] = useState(false);
  const playMigaku = usePlayMoment();

  const startS = Math.max(0, moment.start_ms - LEAD_MS) / 1000;
  const stopS = (Math.max(moment.end_ms, moment.start_ms + 500) + TAIL_MS) / 1000;

  const { data, isLoading, isError, error } = useQuery({
    // token URLs are short-lived — always fetch fresh, never cache
    queryKey: ["moment-play", moment.episode_id],
    queryFn: () => api.browserPlay(moment.episode_id),
    retry: false,
    staleTime: 0,
    gcTime: 0,
  });

  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  // 404 → the video file vanished from disk since the search; bail with a toast.
  useEffect(() => {
    if (!isError) return;
    const msg =
      error instanceof ApiError && error.status === 404
        ? "This episode's video is no longer on disk."
        : error instanceof Error
          ? error.message
          : "Playback failed";
    toast.error("Can't play this moment", msg);
    onCloseRef.current();
  }, [isError, error]);

  // Escape closes; lock body scroll while open.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onCloseRef.current();
    };
    window.addEventListener("keydown", onKey);
    document.body.style.overflow = "hidden";
    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = "";
    };
  }, []);

  // Stop watcher: pause once at the end of the line, then disarm so the
  // native play control continues into the episode.
  useEffect(() => {
    let raf = 0;
    const tick = () => {
      const v = videoRef.current;
      if (v && armedRef.current && !v.paused && v.currentTime >= stopS) {
        v.pause();
        armedRef.current = false;
        setLineEnded(true);
      }
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [stopS]);

  const replay = () => {
    const v = videoRef.current;
    if (!v) return;
    armedRef.current = true;
    setLineEnded(false);
    v.currentTime = startS;
    // Autoplay may be blocked outside a user gesture — Replay IS a gesture,
    // and on initial open a rejection just leaves the paused, seeked frame.
    void v.play().catch(() => {});
  };

  return createPortal(
    <div
      className="fixed inset-0 z-[80] flex items-center justify-center p-3 sm:p-6"
      role="dialog"
      aria-modal="true"
    >
      <button
        aria-hidden
        tabIndex={-1}
        className="absolute inset-0 cursor-default bg-black/80 backdrop-blur-sm"
        onClick={onClose}
      />
      <div className="relative z-[81] flex w-full max-w-4xl flex-col overflow-hidden rounded-2xl border border-border-strong bg-bg-elevated shadow-2xl shadow-black/70">
        {/* header */}
        <div className="flex items-center justify-between gap-3 border-b border-border px-4 py-2.5">
          <p className="min-w-0 truncate text-sm font-semibold text-fg">
            {moment.title ?? "Moment"}
            <span className="font-normal text-faint">
              {moment.ep_number != null && <> · E{moment.ep_number}</>}
              {" · "}
              {formatMs(moment.start_ms)}
            </span>
          </p>
          <button
            onClick={onClose}
            aria-label="Close player"
            className="grid size-8 shrink-0 place-items-center rounded-lg text-faint transition-colors hover:bg-surface-hover hover:text-fg"
          >
            <X className="size-4" />
          </button>
        </div>

        {/* video */}
        <div className="grid min-h-[30vh] place-items-center bg-black">
          {isLoading && (
            <div className="flex flex-col items-center gap-2 py-20 text-muted">
              <Loader2 className="size-6 animate-spin text-brand-bright" />
              <p className="text-xs">Preparing stream…</p>
            </div>
          )}
          {data && (
            <video
              ref={videoRef}
              controls
              preload="metadata"
              src={data.video_url}
              crossOrigin="anonymous"
              className="max-h-[62vh] w-full"
              onLoadedMetadata={(e) => {
                e.currentTarget.currentTime = startS;
                void e.currentTarget.play().catch(() => {});
              }}
            >
              {data.sub_url && (
                <track kind="subtitles" srcLang="ja" label="日本語" src={data.sub_url} default />
              )}
              {data.sub2_url && (
                <track kind="subtitles" srcLang="en" label="English" src={data.sub2_url} />
              )}
            </video>
          )}
        </div>

        {/* the line itself + actions */}
        <div className="border-t border-border px-4 py-3">
          <Furigana
            furigana={moment.text_furigana}
            text={moment.text}
            className="text-lg leading-relaxed text-fg [&_rt]:text-brand-bright"
          />
          {moment.translation && (
            <p className="mt-1 text-sm leading-relaxed text-muted">{moment.translation}</p>
          )}
          <div className="mt-3 flex flex-wrap items-center gap-2">
            <Button variant={lineEnded ? "play" : "secondary"} size="sm" onClick={replay}>
              <RotateCcw className="size-3.5" />
              Replay line
            </Button>
            <Button
              variant="ghost"
              size="sm"
              onClick={() => playMigaku.mutate(moment.line_id, { onSuccess: onClose })}
              loading={playMigaku.isPending}
              title="Continue the episode from this line in Migaku — immersion features live"
            >
              <ExternalLink className="size-3.5" />
              Play in Migaku
            </Button>
            <p className="ml-auto text-[0.7rem] text-faint">
              {lineEnded
                ? "Paused at the end of the line — press play to keep watching."
                : "Auto-pauses when the line ends."}
            </p>
          </div>
        </div>
      </div>
    </div>,
    document.body,
  );
}
