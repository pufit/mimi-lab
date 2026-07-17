import { useEffect, useRef } from "react";
import { createPortal } from "react-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Loader2, X } from "lucide-react";
import { api, ApiError } from "@/lib/api";
import { qk } from "@/lib/hooks";
import { toast } from "@/components/ui/toast";

/**
 * Fallback in-browser player — used when the Connector (Chrome + Migaku) is
 * offline, or on explicit request. Streams the episode straight from the
 * server with VTT subtitle tracks, resumes at the saved position, and reports
 * watch progress every ~15s while playing plus once on pause/close/unmount.
 */
export function BrowserPlayer({
  episodeId,
  title,
  onClose,
}: {
  episodeId: number;
  title?: string;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const closedRef = useRef(false);

  const { data, isLoading, isError, error } = useQuery({
    queryKey: ["browser-play", episodeId],
    queryFn: () => api.browserPlay(episodeId),
    retry: false,
    staleTime: 0,
    gcTime: 0,
  });

  const report = () => {
    const v = videoRef.current;
    if (!v || !Number.isFinite(v.duration) || v.duration <= 0) return;
    api
      .watchProgress(episodeId, Math.floor(v.currentTime * 1000), Math.floor(v.duration * 1000))
      .catch(() => {
        /* progress reporting is best-effort */
      });
  };

  const close = () => {
    if (closedRef.current) return;
    closedRef.current = true;
    report();
    qc.invalidateQueries({ queryKey: qk.continue });
    qc.invalidateQueries({ queryKey: ["episodes"] });
    onClose();
  };
  const closeRef = useRef(close);
  closeRef.current = close;

  // 404 → no video for this episode; surface a toast and bail out.
  useEffect(() => {
    if (!isError) return;
    const msg =
      error instanceof ApiError && error.status === 404
        ? "This episode has no local video."
        : error instanceof Error
          ? error.message
          : "Playback failed";
    toast.error("Can't play in browser", msg);
    closeRef.current();
  }, [isError, error]);

  // Escape closes; lock body scroll while open.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") closeRef.current();
    };
    window.addEventListener("keydown", onKey);
    document.body.style.overflow = "hidden";
    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = "";
    };
  }, []);

  // Progress heartbeat every 15s while actually playing + final report on unmount.
  useEffect(() => {
    const iv = setInterval(() => {
      const v = videoRef.current;
      if (v && !v.paused && !v.ended) report();
    }, 15000);
    return () => {
      clearInterval(iv);
      report();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [episodeId]);

  return createPortal(
    <div className="fixed inset-0 z-[80] flex items-center justify-center p-3 sm:p-6" role="dialog" aria-modal="true">
      <button
        aria-hidden
        tabIndex={-1}
        className="absolute inset-0 cursor-default bg-black/80 backdrop-blur-sm"
        onClick={close}
      />
      <div className="relative z-[81] flex w-full max-w-6xl flex-col overflow-hidden rounded-2xl border border-border-strong bg-bg-elevated shadow-2xl shadow-black/70">
        <div className="flex items-center justify-between gap-3 border-b border-border px-4 py-2.5">
          <p className="min-w-0 truncate text-sm font-semibold text-fg">
            {title ?? "Playing in browser"}
          </p>
          <button
            onClick={close}
            aria-label="Close player"
            className="grid size-8 shrink-0 place-items-center rounded-lg text-faint transition-colors hover:bg-surface-hover hover:text-fg"
          >
            <X className="size-4" />
          </button>
        </div>

        <div className="grid min-h-[40vh] place-items-center bg-black">
          {isLoading && (
            <div className="flex flex-col items-center gap-2 py-24 text-muted">
              <Loader2 className="size-6 animate-spin text-brand-bright" />
              <p className="text-xs">Preparing stream…</p>
            </div>
          )}
          {data && (
            <video
              ref={videoRef}
              controls
              autoPlay
              src={data.video_url}
              crossOrigin="anonymous"
              className="max-h-[78vh] w-full"
              onLoadedMetadata={(e) => {
                if (data.seek_ms != null && data.seek_ms > 0) {
                  e.currentTarget.currentTime = data.seek_ms / 1000;
                }
              }}
              onPause={report}
              onEnded={report}
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

        <p className="border-t border-border px-4 py-2 text-[0.7rem] text-faint">
          Fallback player — no Migaku features. Progress is saved automatically; toggle
          subtitle tracks with the video&apos;s CC control.
        </p>
      </div>
    </div>,
    document.body,
  );
}
