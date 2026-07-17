import { useState, useRef, useEffect, useMemo } from "react";
import { createPortal } from "react-dom";
import { useParams, Link, useNavigate } from "react-router-dom";
import {
  ArrowLeft,
  Play,
  Captions,
  Check,
  CheckCheck,
  ChevronDown,
  Star,
  Calendar,
  Clapperboard,
  ScrollText,
  Sparkles,
  Eye,
  EyeOff,
  DownloadCloud,
  Loader2,
  MonitorPlay,
  RotateCw,
  X,
  Users,
  ShieldCheck,
  Layers,
  HardDrive,
  Rss,
  Trash2,
} from "lucide-react";
import {
  useTitle,
  useEpisodes,
  useDownloads,
  usePlay,
  useFetchSubs,
  useSetWatched,
  useWatchedUpTo,
  useAnalyzeTitle,
  useFollowTitle,
  useDownloadEpisode,
  useEpisodeReleases,
  useSeasonReleases,
  useDownloadBatch,
  useCancelDownload,
  useRetryDownload,
  useConnectorStatus,
  useDeleteTitle,
} from "@/lib/hooks";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { ErrorState } from "@/components/ui/error-state";
import { ComprehensionPill } from "@/components/comprehension";
import { ComprehensionPanel } from "@/components/comprehension-panel";
import { BrowserPlayer } from "@/components/browser-player";
import { SETTLED_STATES, isFailedState, failedStateLabel } from "@/lib/download-states";
import {
  cn,
  titleName,
  titleSub,
  malStatusLabel,
  stripHtml,
  formatMs,
} from "@/lib/utils";
import type { Download, Episode, ReleaseOption, Title } from "@/lib/types";

export function TitlePage() {
  const { id } = useParams();
  const anilistId = Number(id);
  const { data: title, isLoading, isError, refetch } = useTitle(anilistId);

  if (isLoading) return <TitleSkeleton />;
  if (isError || !title)
    return (
      <div className="animate-fade-in">
        <BackLink />
        <ErrorState message="Title not found." onRetry={() => refetch()} />
      </div>
    );

  return (
    <div className="animate-fade-in">
      <Hero title={title} />
      <EpisodeList anilistId={anilistId} />
    </div>
  );
}

function BackLink() {
  return (
    <Link
      to="/"
      className="mb-4 inline-flex items-center gap-1.5 text-sm text-muted transition-colors hover:text-fg"
    >
      <ArrowLeft className="size-4" />
      Library
    </Link>
  );
}

function Hero({ title }: { title: Title }) {
  const name = titleName(title);
  const sub = titleSub(title);
  const synopsis = stripHtml(title.description);

  return (
    <div className="relative -mx-5 -mt-20 mb-8 md:-mx-8 md:-mt-8">
      {/* banner */}
      <div className="relative h-56 overflow-hidden md:h-72">
        {title.banner_url ? (
          <img src={title.banner_url} alt="" className="size-full object-cover" />
        ) : (
          <div className="size-full bg-gradient-to-br from-brand-dim/30 via-surface to-bg" />
        )}
        <div className="absolute inset-0 bg-gradient-to-t from-bg via-bg/70 to-bg/20" />
        <div className="absolute inset-0 bg-gradient-to-r from-bg/80 to-transparent" />
      </div>

      <div className="relative -mt-28 px-5 md:px-8">
        <BackLink />
        <div className="flex flex-col gap-6 sm:flex-row sm:items-end">
          {/* poster */}
          <div className="hidden w-40 shrink-0 overflow-hidden rounded-xl border border-border-strong shadow-2xl shadow-black/50 sm:block">
            {title.cover_url ? (
              <img src={title.cover_url} alt={name} className="aspect-[2/3] w-full object-cover" />
            ) : (
              <div className="aspect-[2/3] w-full bg-surface" />
            )}
          </div>

          <div className="min-w-0 flex-1 pb-1">
            <h1 className="text-3xl font-bold tracking-tight text-fg text-balance md:text-4xl">
              {name}
            </h1>
            {sub && <p className="mt-1 text-base text-muted">{sub}</p>}
            {title.native && (
              <p className="font-jp mt-0.5 text-sm text-faint">{title.native}</p>
            )}

            {/* meta row */}
            <div className="mt-4 flex flex-wrap items-center gap-2.5">
              {title.mal_status && (
                <Badge variant="solid">{malStatusLabel(title.mal_status)}</Badge>
              )}
              {title.mal_score != null && title.mal_score > 0 && (
                <span className="inline-flex items-center gap-1 text-sm text-muted">
                  <Star className="size-3.5 fill-comp-amber text-comp-amber" />
                  <span className="font-semibold text-fg">{title.mal_score}</span>
                  <span className="text-faint">/ 10</span>
                </span>
              )}
              {title.format && (
                <span className="inline-flex items-center gap-1 text-sm text-muted">
                  <Clapperboard className="size-3.5" />
                  {title.format}
                </span>
              )}
              {title.year && (
                <span className="inline-flex items-center gap-1 text-sm text-muted">
                  <Calendar className="size-3.5" />
                  {title.year}
                </span>
              )}
              {title.episode_count_local > 0 && (
                <span className="text-sm text-faint">
                  {title.episode_count_local}
                  {title.total_episodes ? ` / ${title.total_episodes}` : ""} eps local
                </span>
              )}
              {title.avg_comprehension != null && (
                <span className="inline-flex items-center gap-1.5 text-sm text-muted">
                  <Sparkles className="size-3.5 text-brand-bright" />
                  avg <ComprehensionPill pct={title.avg_comprehension} size="sm" />
                </span>
              )}
            </div>

            {synopsis && (
              <p className="mt-4 max-w-3xl text-sm leading-relaxed text-muted line-clamp-4">
                {synopsis}
              </p>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

function EpisodeList({ anilistId }: { anilistId: number }) {
  const downloads = useDownloads();
  const mine = (downloads.data ?? []).filter((d) => d.anilist_id === anilistId);
  const hasActive = mine.some((d) => !SETTLED_STATES.has(d.state.toLowerCase()));
  const activeBatch = mine.find(
    (d) => d.kind === "batch" && !SETTLED_STATES.has(d.state.toLowerCase()),
  );
  const dlById = useMemo(
    () => new Map((downloads.data ?? []).map((d) => [d.id, d])),
    [downloads.data],
  );

  const { data: episodes, isLoading, isError, refetch } = useEpisodes(anilistId, hasActive);
  const analyze = useAnalyzeTitle(anilistId);
  const follow = useFollowTitle();
  const [packOpen, setPackOpen] = useState(false);

  // episode numbers already on disk — the season-pack picker marks these as
  // "have" and leaves them out of the default selection
  const localEps = useMemo(
    () => new Set((episodes ?? []).filter((e) => !!e.video_path).map((e) => e.ep_number)),
    [episodes],
  );

  return (
    <div className="px-0">
      <div className="mb-3 flex items-center justify-between gap-3">
        <h2 className="text-lg font-semibold text-fg">Episodes</h2>
        <div className="flex items-center gap-2">
          <Button
            variant="secondary"
            size="sm"
            onClick={() => analyze.mutate(undefined)}
            loading={analyze.isPending}
            title="Fetch jimaku subtitles and score comprehension — no download needed"
          >
            <Sparkles className="size-3.5" />
            <span className="hidden sm:inline">Analyze comprehension</span>
            <span className="sm:hidden">Analyze</span>
          </Button>
          <Button
            variant="secondary"
            size="sm"
            onClick={() => follow.mutate(anilistId)}
            loading={follow.isPending}
            title="Auto-download new episodes of this show via an RSS follow"
          >
            <Rss className="size-3.5" />
            <span className="hidden sm:inline">Follow</span>
          </Button>
          <Button
            variant="primary"
            size="sm"
            onClick={() => setPackOpen(true)}
            title="Download the whole season in one torrent — best for older shows whose individual episodes are no longer seeded"
          >
            <Layers className="size-3.5" />
            Download season
          </Button>
          <DeleteTitleButton anilistId={anilistId} />
        </div>
      </div>

      {activeBatch && <BatchProgressBanner dl={activeBatch} anilistId={anilistId} />}

      {isLoading && (
        <div className="space-y-2">
          {Array.from({ length: 5 }).map((_, i) => (
            <Skeleton key={i} className="h-16 w-full rounded-xl" />
          ))}
        </div>
      )}
      {isError && <ErrorState onRetry={() => refetch()} />}
      {episodes && episodes.length === 0 && (
        <div className="rounded-xl border border-dashed border-border-strong bg-surface/30 px-5 py-10 text-center">
          <p className="text-sm text-muted">No episodes analyzed yet.</p>
          <p className="mx-auto mt-1 max-w-md text-xs text-faint">
            Hit <span className="text-brand-bright">Analyze comprehension</span> to pull
            subtitles from jimaku and score how hard this show is, or{" "}
            <span className="text-brand-bright">Download season</span> to grab the whole
            show in one go.
          </p>
        </div>
      )}
      {episodes && episodes.length > 0 && (
        <div className="space-y-2">
          {episodes
            .slice()
            .sort((a, b) => a.ep_number - b.ep_number)
            .map((ep) => (
              <EpisodeRow
                key={ep.id}
                ep={ep}
                anilistId={anilistId}
                seasonActive={!!activeBatch}
                dl={ep.download_id != null ? dlById.get(ep.download_id) : undefined}
              />
            ))}
        </div>
      )}

      <SeasonPackModal
        anilistId={anilistId}
        open={packOpen}
        onClose={() => setPackOpen(false)}
        localEps={localEps}
      />
    </div>
  );
}

function EpisodeRow({
  ep,
  anilistId,
  seasonActive,
  dl,
}: {
  ep: Episode;
  anilistId: number;
  seasonActive: boolean;
  dl?: Download;
}) {
  const [expanded, setExpanded] = useState(false);
  const [browserOpen, setBrowserOpen] = useState(false);
  const play = usePlay();
  const fetchSubs = useFetchSubs(anilistId);
  const setWatched = useSetWatched(anilistId);
  const watchedUpTo = useWatchedUpTo(anilistId);
  const cancel = useCancelDownload(anilistId);
  const retry = useRetryDownload(anilistId);
  const { data: connector } = useConnectorStatus();
  const connectorOnline = !!connector?.connected;

  const state = ep.download_state?.toLowerCase() ?? null;
  const failed = isFailedState(state);
  const downloading = state === "downloading" || state === "queued";
  const dlPct = dl ? Math.round((dl.progress ?? 0) * 100) : null;

  const markUpToHere = () => {
    if (
      window.confirm(
        `Mark episodes 1–${ep.ep_number} of this show as watched?`,
      )
    ) {
      watchedUpTo.mutate(ep.ep_number);
    }
  };

  // thin in-browser watch-progress bar (partial fallback-player sessions)
  const watchPct =
    !ep.watched &&
    ep.watch_progress_ms != null &&
    ep.watch_progress_ms > 30000 &&
    ep.duration_ms
      ? Math.min(100, (ep.watch_progress_ms / ep.duration_ms) * 100)
      : null;

  return (
    <div
      className={cn(
        "overflow-hidden rounded-xl border bg-surface/60 transition-colors",
        expanded ? "border-border-strong" : "border-border hover:border-border-strong",
      )}
    >
      <div className="flex flex-col gap-3 p-3.5 sm:flex-row sm:items-center">
        {/* episode number disc */}
        <div className="flex items-center gap-3.5">
          <button
            onClick={() => setWatched.mutate({ episodeId: ep.id, watched: !ep.watched })}
            disabled={setWatched.isPending}
            title={ep.watched ? "Mark unwatched" : "Mark watched"}
            className={cn(
              "grid size-11 shrink-0 place-items-center rounded-lg border text-sm font-bold tabular-nums transition-colors",
              ep.watched
                ? "border-comp-green/40 bg-comp-green/15 text-comp-green"
: "border-border-strong bg-bg-elevated text-muted hover:border-faint",
            )}
          >
            {ep.watched ? <Check className="size-5" /> : ep.ep_number}
          </button>

          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <p className="text-sm font-semibold text-fg">
                Episode {ep.ep_number}
                {ep.title && <span className="font-normal text-muted"> · {ep.title}</span>}
              </p>
            </div>
            <div className="mt-1 flex flex-wrap items-center gap-1.5">
              {ep.has_subtitle ? (
                <Badge variant="success">
                  <Captions className="size-3" />
                  Subbed
                </Badge>
              ) : (
                <Badge variant="outline">
                  <Captions className="size-3" />
                  No subs
                </Badge>
              )}
              {ep.has_english && (
                <Badge variant="default" title="English secondary/reference track available">
                  EN
                </Badge>
              )}
              {ep.watched ? (
                <Badge variant="default">
                  <Eye className="size-3" />
                  Watched
                </Badge>
              ) : (
                <Badge variant="outline">
                  <EyeOff className="size-3" />
                  Unwatched
                </Badge>
              )}
              {ep.new_word_count != null && ep.new_word_count > 0 && (
                <Badge variant="warning">{ep.new_word_count} new words</Badge>
              )}
              {ep.duration_ms != null && (
                <span className="text-[0.7rem] text-faint">{formatMs(ep.duration_ms)}</span>
              )}
            </div>
          </div>
        </div>

        {/* right actions */}
        <div className="flex flex-wrap items-center gap-2 sm:ml-auto">
          <ComprehensionPill pct={ep.comprehension_pct} />

          {!ep.has_subtitle && (
            <Button
              variant="ghost"
              size="sm"
              onClick={() => fetchSubs.mutate(ep.id)}
              loading={fetchSubs.isPending}
            >
              <Captions className="size-3.5" />
              Get subs
            </Button>
          )}

          {ep.has_subtitle && (
            <Button
              variant="ghost"
              size="icon-sm"
              asChild
              title="Read the transcript with per-word known/unknown coloring"
            >
              <Link to={`/transcript/${ep.id}`} aria-label="Open transcript">
                <ScrollText className="size-4" />
              </Link>
            </Button>
          )}

          <Button
            variant="ghost"
            size="icon-sm"
            onClick={markUpToHere}
            disabled={watchedUpTo.isPending}
            aria-label={`Mark watched up to episode ${ep.ep_number}`}
            title={`Mark watched up to here (episodes 1–${ep.ep_number})`}
          >
            <CheckCheck className="size-4" />
          </Button>

          <Button
            variant="ghost"
            size="sm"
            onClick={() => setExpanded((v) => !v)}
            aria-expanded={expanded}
          >
            <ChevronDown
              className={cn("size-4 transition-transform", expanded && "rotate-180")}
            />
            Comprehension
          </Button>

          {ep.video_path ? (
            connectorOnline ? (
              <>
                <Button
                  variant="play"
                  size="sm"
                  onClick={() => play.mutate({ episodeId: ep.id })}
                  loading={play.isPending}
                >
                  <Play className="size-3.5 fill-current" />
                  Play in Migaku
                </Button>
                <Button
                  variant="ghost"
                  size="icon-sm"
                  onClick={() => setBrowserOpen(true)}
                  aria-label="Play in browser"
                  title="Play in browser (fallback player, no Migaku)"
                >
                  <MonitorPlay className="size-4" />
                </Button>
              </>
            ) : (
              <Button
                variant="play"
                size="sm"
                onClick={() => setBrowserOpen(true)}
                title="Connector offline — stream in the browser instead"
              >
                <MonitorPlay className="size-3.5" />
                Play in browser
              </Button>
            )
          ) : downloading ? (
            <span className="inline-flex items-center gap-1.5 rounded-lg border border-border-strong bg-surface py-1.5 pl-2.5 pr-1 text-xs text-muted">
              <Loader2 className="size-3.5 animate-spin" />
              {state === "queued" ? "Queued…" : "Downloading…"}
              {dlPct != null && state !== "queued" && (
                <span className="tabular-nums text-fg">{dlPct}%</span>
              )}
              {ep.download_id != null && (
                <button
                  onClick={() => cancel.mutate(ep.download_id as number)}
                  disabled={cancel.isPending}
                  aria-label="Cancel download"
                  title="Cancel download"
                  className="ml-0.5 grid size-5 place-items-center rounded text-faint transition-colors hover:bg-comp-red/15 hover:text-comp-red"
                >
                  <X className="size-3.5" />
                </button>
              )}
            </span>
          ) : failed && state ? (
            <>
              <Badge variant="danger" title={`Download state: ${state}`}>
                {failedStateLabel(state)}
              </Badge>
              {ep.download_id != null ? (
                <Button
                  variant="secondary"
                  size="sm"
                  onClick={() => retry.mutate(ep.download_id as number)}
                  loading={retry.isPending}
                  title="Retry this download"
                >
                  <RotateCw className="size-3.5" />
                  Retry
                </Button>
              ) : (
                <DownloadMenu ep={ep} anilistId={anilistId} />
              )}
            </>
          ) : seasonActive ? (
            <span
              className="inline-flex items-center gap-1.5 rounded-lg border border-brand/30 bg-brand/10 px-2.5 py-1.5 text-xs text-brand-bright"
              title="This episode is part of the season pack currently downloading"
            >
              <Layers className="size-3.5" />
              In season pack
            </span>
          ) : (
            <DownloadMenu ep={ep} anilistId={anilistId} />
          )}
        </div>
      </div>

      {/* thin fallback-player resume bar */}
      {watchPct != null && (
        <div className="h-1 w-full bg-bg-elevated" title={`Resume at ${formatMs(ep.watch_progress_ms)}`}>
          <div
            className="h-full bg-brand-bright/80 transition-all"
            style={{ width: `${watchPct}%` }}
          />
        </div>
      )}

      {expanded && (
        <div className="border-t border-border bg-bg-elevated/40">
          <ComprehensionPanel episodeId={ep.id} open={expanded} />
        </div>
      )}

      {browserOpen && (
        <BrowserPlayer
          episodeId={ep.id}
          title={`Episode ${ep.ep_number}${ep.title ? ` · ${ep.title}` : ""}`}
          onClose={() => setBrowserOpen(false)}
        />
      )}
    </div>
  );
}

function DownloadMenu({ ep, anilistId }: { ep: Episode; anilistId: number }) {
  const [open, setOpen] = useState(false);
  const [pos, setPos] = useState<{ top: number; left: number; width: number } | null>(null);
  const wrapRef = useRef<HTMLDivElement>(null);
  const releases = useEpisodeReleases(ep.id, open);
  const download = useDownloadEpisode(anilistId);

  // Position a fixed, portaled panel from the trigger's rect. The panel is
  // rendered into document.body so it isn't clipped by the episode card's
  // overflow-hidden (which is why an absolutely-positioned panel was invisible).
  const place = () => {
    const el = wrapRef.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    const width = Math.min(352, window.innerWidth - 16);
    let left = r.right - width;
    left = Math.max(8, Math.min(left, window.innerWidth - width - 8));
    setPos({ top: r.bottom + 8, left, width });
  };

  const toggle = () => {
    if (!open) place();
    setOpen((o) => !o);
  };

  useEffect(() => {
    if (!open) return;
    const reposition = () => place();
    window.addEventListener("resize", reposition);
    window.addEventListener("scroll", reposition, true);
    return () => {
      window.removeEventListener("resize", reposition);
      window.removeEventListener("scroll", reposition, true);
    };
  }, [open]);

  const pick = (r?: ReleaseOption) => {
    download.mutate({ episodeId: ep.id, release: r });
    setOpen(false);
  };

  return (
    <div ref={wrapRef} className="inline-flex">
      <Button
        variant="secondary"
        size="sm"
        onClick={toggle}
        loading={download.isPending}
        aria-expanded={open}
        title="Choose a release to download from nyaa"
      >
        <DownloadCloud className="size-3.5" />
        Download
        <ChevronDown className={cn("size-3.5 transition-transform", open && "rotate-180")} />
      </Button>

      {open &&
        pos &&
        createPortal(
          <>
            {/* click-away backdrop */}
            <button
              aria-hidden
              tabIndex={-1}
              className="fixed inset-0 z-[60] cursor-default"
              onClick={() => setOpen(false)}
            />
            <div
              className="fixed z-[61] overflow-hidden rounded-xl border border-border-strong bg-bg-elevated shadow-2xl shadow-black/50"
              style={{ top: pos.top, left: pos.left, width: pos.width }}
            >
              <div className="flex items-center justify-between border-b border-border px-3 py-2">
                <span className="text-xs font-medium uppercase tracking-wide text-faint">
                  Releases · ep {ep.ep_number}
                </span>
                {releases.data?.matched_by === "haiku" && (
                  <span className="inline-flex items-center gap-1 text-[0.7rem] text-brand-bright">
                    <Sparkles className="size-3" /> Haiku pick
                  </span>
                )}
              </div>

              <div className="max-h-80 overflow-y-auto p-1.5">
                {releases.isLoading && (
                  <div className="space-y-1.5 p-1.5">
                    {Array.from({ length: 3 }).map((_, i) => (
                      <Skeleton key={i} className="h-12 w-full rounded-lg" />
                    ))}
                    <p className="px-1 pt-1 text-center text-[0.7rem] text-faint">
                      Searching nyaa + asking Haiku…
                    </p>
                  </div>
                )}
                {releases.isError && (
                  <p className="px-3 py-6 text-center text-sm text-comp-red">Search failed.</p>
                )}
                {releases.data && releases.data.releases.length === 0 && (
                  <p className="px-3 py-6 text-center text-sm text-muted">
                    {releases.data.reason || "No releases found."}
                  </p>
                )}
                {releases.data?.releases.map((r, i) => (
                  <ReleaseRow key={r.nyaa_id ?? `${r.title}-${i}`} r={r} onPick={() => pick(r)} />
                ))}
              </div>
            </div>
          </>,
          document.body,
        )}
    </div>
  );
}

function ReleaseRow({ r, onPick }: { r: ReleaseOption; onPick: () => void }) {
  return (
    <button
      onClick={onPick}
      className={cn(
        "flex w-full items-start gap-2 rounded-lg border p-2 text-left transition-colors",
        r.recommended
          ? "border-brand/40 bg-brand/10 hover:bg-brand/15"
          : "border-transparent hover:bg-surface-hover/60",
      )}
    >
      <DownloadCloud className="mt-0.5 size-3.5 shrink-0 text-faint" />
      <div className="min-w-0 flex-1">
        <p className="line-clamp-2 text-xs font-medium leading-snug text-fg">{r.title}</p>
        <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[0.7rem] text-faint">
          {r.release_group && <span className="text-muted">{r.release_group}</span>}
          {r.resolution && <Badge variant="outline">{r.resolution}</Badge>}
          {r.size && <span>{r.size}</span>}
          <span className="inline-flex items-center gap-0.5">
            <Users className="size-3" />
            {r.seeders ?? 0}
          </span>
          {r.trusted && <ShieldCheck className="size-3 text-comp-green" aria-label="Trusted" />}
        </div>
        {r.recommended && (
          <div className="mt-1 flex items-start gap-1 text-[0.7rem] text-brand-bright">
            <Star className="mt-0.5 size-3 shrink-0 fill-brand-bright" />
            <span className="line-clamp-2">{r.reason || "Recommended"}</span>
          </div>
        )}
      </div>
    </button>
  );
}

function BatchProgressBanner({ dl, anilistId }: { dl: Download; anilistId: number }) {
  const cancel = useCancelDownload(anilistId);
  const state = dl.state.toLowerCase();
  const pct = Math.round((dl.progress ?? 0) * 100);
  const done = dl.done_files ?? 0;
  const total = dl.total_files ?? null;
  const importing = state === "completed"; // torrent finished, importing episodes
  const importPct = total ? Math.round((done / total) * 100) : 0;
  const barPct = importing ? importPct : pct;
  const phase = importing
    ? `Importing episodes${total ? ` · ${done}/${total}` : ""}`
    : state === "queued"
      ? "Queued — waiting for Transmission"
      : `Downloading season pack · ${pct}%`;

  return (
    <div className="mb-4 rounded-xl border border-brand/30 bg-brand/10 p-4">
      <div className="flex items-center gap-3">
        <div className="grid size-9 shrink-0 place-items-center rounded-lg bg-brand/20 text-brand-bright">
          {importing ? <Loader2 className="size-4 animate-spin" /> : <Layers className="size-4" />}
        </div>
        <div className="min-w-0 flex-1">
          <p className="truncate text-sm font-semibold text-fg">
            {dl.title_guess ?? "Season pack"}
          </p>
          <p className="text-xs text-brand-bright">{phase}</p>
        </div>
        <button
          onClick={() => cancel.mutate(dl.id)}
          disabled={cancel.isPending}
          aria-label="Cancel season download"
          title="Cancel season download"
          className="grid size-7 place-items-center rounded-lg text-faint transition-colors hover:bg-comp-red/15 hover:text-comp-red"
        >
          <X className="size-4" />
        </button>
      </div>
      <div className="mt-3 flex items-center gap-2.5">
        <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-bg-elevated">
          <div
            className="h-full rounded-full bg-brand transition-all duration-500"
            style={{ width: `${Math.max(2, barPct)}%` }}
          />
        </div>
        <span className="w-10 text-right text-xs tabular-nums text-muted">{barPct}%</span>
      </div>
      <p className="mt-2 text-[0.7rem] text-faint">
        Episodes appear below as each file is processed — you can start the early ones
        before the rest finish.
      </p>
    </div>
  );
}

function SeasonPackModal({
  anilistId,
  open,
  onClose,
  localEps,
}: {
  anilistId: number;
  open: boolean;
  onClose: () => void;
  /** Episode numbers already on disk — pre-deselected in the picker. */
  localEps: Set<number>;
}) {
  const releases = useSeasonReleases(anilistId, open);
  const download = useDownloadBatch(anilistId);
  // when a pack is chosen we step into the episode picker; null = pack list
  const [pack, setPack] = useState<ReleaseOption | null>(null);
  const [eps, setEps] = useState<Set<number>>(new Set());

  useEffect(() => {
    if (!open) {
      setPack(null);
      return;
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") (pack ? setPack(null) : onClose());
    };
    window.addEventListener("keydown", onKey);
    document.body.style.overflow = "hidden";
    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = "";
    };
  }, [open, onClose, pack]);

  if (!open) return null;

  const data = releases.data;
  const releaseList = data?.releases ?? [];
  const total = data?.total_episodes ?? null;
  // an episode picker only makes sense with a known, reasonable episode count
  const selectable = !!total && total > 1 && total <= 200;
  const allEps = (n: number) => new Set(Array.from({ length: n }, (_, i) => i + 1));

  const choose = (r: ReleaseOption) => {
    if (selectable && total) {
      // default selection: everything you don't already have locally
      const initial = new Set([...allEps(total)].filter((n) => !localEps.has(n)));
      setEps(initial.size > 0 ? initial : allEps(total));
      setPack(r);
    } else {
      download.mutate({ release: r });
      onClose();
    }
  };
  const start = () => {
    if (!pack || !total) return;
    const arr = [...eps].sort((a, b) => a - b);
    download.mutate({ release: pack, episodes: arr.length === total ? undefined : arr });
    onClose();
  };
  const toggle = (n: number) =>
    setEps((s) => {
      const x = new Set(s);
      x.has(n) ? x.delete(n) : x.add(n);
      return x;
    });

  return createPortal(
    <div
      className="fixed inset-0 z-[70] flex items-end justify-center sm:items-center"
      role="dialog"
      aria-modal="true"
    >
      <button
        aria-hidden
        tabIndex={-1}
        className="absolute inset-0 cursor-default bg-black/60 backdrop-blur-sm"
        onClick={onClose}
      />
      <div className="animate-fade-in relative z-[71] flex max-h-[85vh] w-full max-w-2xl flex-col overflow-hidden rounded-t-2xl border border-border-strong bg-bg-elevated shadow-2xl shadow-black/60 sm:rounded-2xl">
        <div className="flex items-start justify-between gap-3 border-b border-border px-5 py-4">
          <div className="min-w-0">
            <h3 className="flex items-center gap-2 text-base font-semibold text-fg">
              {pack ? (
                <button
                  onClick={() => setPack(null)}
                  aria-label="Back to packs"
                  className="-ml-1 inline-flex size-6 items-center justify-center rounded text-muted hover:text-fg"
                >
                  <ArrowLeft className="size-4" />
                </button>
              ) : (
                <Layers className="size-4 text-brand-bright" />
              )}
              {pack ? "Choose episodes" : "Download season"}
            </h3>
            <p className="mt-0.5 line-clamp-1 text-xs text-muted">
              {pack ? pack.title : data?.title ?? "Season packs"}
              {!pack && data?.total_episodes ? ` · ${data.total_episodes} episodes` : ""}
            </p>
          </div>
          <div className="flex items-center gap-2">
            {!pack && data?.matched_by === "haiku" && (
              <span className="inline-flex items-center gap-1 text-[0.7rem] text-brand-bright">
                <Sparkles className="size-3" /> Haiku pick
              </span>
            )}
            <button
              onClick={onClose}
              aria-label="Close"
              className="grid size-7 place-items-center rounded-lg text-faint hover:bg-surface-hover/60 hover:text-fg"
            >
              <X className="size-4" />
            </button>
          </div>
        </div>

        {pack && total ? (
          <EpisodeSelect
            total={total}
            eps={eps}
            localEps={localEps}
            onToggle={toggle}
            onAll={() => setEps(allEps(total))}
            onNone={() => setEps(new Set())}
          />
        ) : (
          <div className="min-h-0 flex-1 overflow-y-auto p-3">
            {releases.isLoading && (
              <div className="space-y-2 p-1">
                {Array.from({ length: 3 }).map((_, i) => (
                  <Skeleton key={i} className="h-20 w-full rounded-xl" />
                ))}
                <p className="px-1 pt-1 text-center text-xs text-faint">
                  Searching nyaa for season packs + asking Haiku…
                </p>
              </div>
            )}
            {releases.isError && (
              <p className="px-3 py-10 text-center text-sm text-comp-red">
                Search failed — nyaa may be unreachable.
              </p>
            )}
            {data && releaseList.length === 0 && (
              <div className="px-3 py-10 text-center">
                <p className="text-sm text-muted">{data.reason || "No season packs found."}</p>
                <p className="mx-auto mt-1 max-w-sm text-xs text-faint">
                  Try a per-episode download instead, or search nyaa directly from the Acquire page.
                </p>
              </div>
            )}
            {releaseList.length > 0 && (
              <div className="space-y-2">
                {releaseList.map((r, i) => (
                  <BatchCard
                    key={r.nyaa_id ?? `${r.title}-${i}`}
                    r={r}
                    busy={download.isPending}
                    selectable={selectable}
                    onPick={() => choose(r)}
                  />
                ))}
              </div>
            )}
          </div>
        )}

        <div className="border-t border-border px-5 py-3">
          {pack && total ? (
            <div className="flex items-center justify-between gap-3">
              <p className="text-xs text-faint">
                {eps.size === total ? (
                  "Whole season"
                ) : (
                  <>
                    <span className="font-semibold text-fg">{eps.size}</span> of {total} episodes
                  </>
                )}
              </p>
              <Button variant="primary" size="sm" onClick={start} disabled={eps.size === 0}>
                <DownloadCloud className="size-3.5" />
                {eps.size === total ? `Download all ${total}` : `Download ${eps.size}`}
              </Button>
            </div>
          ) : (
            <p className="text-[0.7rem] text-faint">
              The whole torrent downloads once; every episode is auto-imported into your
              library. Season packs are large (often several GB) — make sure you have the
              disk space.
            </p>
          )}
        </div>
      </div>
    </div>,
    document.body,
  );
}

function EpisodeSelect({
  total,
  eps,
  localEps,
  onToggle,
  onAll,
  onNone,
}: {
  total: number;
  eps: Set<number>;
  localEps: Set<number>;
  onToggle: (n: number) => void;
  onAll: () => void;
  onNone: () => void;
}) {
  return (
    <div className="min-h-0 flex-1 overflow-y-auto p-4">
      <div className="mb-3 flex items-center justify-between gap-3">
        <p className="text-xs text-muted">
          <span className="font-semibold text-fg">{eps.size}</span> of {total} selected
          {localEps.size > 0 && (
            <span className="text-faint"> · {localEps.size} already local</span>
          )}
        </p>
        <div className="flex gap-1.5">
          <Button variant="ghost" size="sm" onClick={onAll}>
            All
          </Button>
          <Button variant="ghost" size="sm" onClick={onNone}>
            None
          </Button>
        </div>
      </div>
      <div className="grid grid-cols-6 gap-1.5 sm:grid-cols-8">
        {Array.from({ length: total }, (_, i) => i + 1).map((n) => {
          const on = eps.has(n);
          const have = localEps.has(n);
          return (
            <button
              key={n}
              onClick={() => onToggle(n)}
              aria-pressed={on}
              title={have ? `Episode ${n} is already in your library` : undefined}
              className={cn(
                "relative flex h-9 items-center justify-center rounded-lg border text-xs font-medium tabular-nums transition-all",
                on
                  ? "border-brand/60 bg-brand/20 text-brand-bright shadow-sm"
                  : have
                    ? "border-comp-green/40 bg-comp-green/10 text-comp-green/80 hover:bg-comp-green/15"
                    : "border-dashed border-border bg-transparent text-faint line-through opacity-40 hover:opacity-70",
              )}
            >
              {n}
              {have && !on && (
                <Check className="absolute right-0.5 top-0.5 size-2.5 text-comp-green" />
              )}
            </button>
          );
        })}
      </div>
      <p className="mt-3 text-[0.7rem] text-faint">
        Unchecked episodes aren&apos;t downloaded.
        {localEps.size > 0 && (
          <>
            {" "}
            <span className="text-comp-green/80">Green</span> episodes are already local and
            skipped by default — select one to re-download it.
          </>
        )}{" "}
        You can grab the rest later.
      </p>
    </div>
  );
}

function BatchCard({
  r,
  onPick,
  busy,
  selectable,
}: {
  r: ReleaseOption;
  onPick: () => void;
  busy: boolean;
  selectable: boolean;
}) {
  const seeders = r.seeders ?? 0;
  const health = seeders >= 5 ? "text-comp-green" : seeders > 0 ? "text-comp-amber" : "text-faint";
  return (
    <div
      className={cn(
        "rounded-xl border p-3 transition-colors",
        r.recommended
          ? "border-brand/50 bg-brand/10"
          : "border-border bg-surface/50 hover:border-border-strong",
      )}
    >
      <div className="flex items-start gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex items-start gap-1.5">
            {r.trusted && (
              <ShieldCheck className="mt-0.5 size-3.5 shrink-0 text-comp-green" aria-label="Trusted" />
            )}
            <p className="line-clamp-2 text-sm font-medium text-fg">{r.title}</p>
          </div>
          <div className="mt-1.5 flex flex-wrap items-center gap-x-2.5 gap-y-1 text-[0.7rem] text-faint">
            {r.release_group && <span className="text-muted">{r.release_group}</span>}
            {r.episode_span && (
              <Badge variant="solid">
                <Layers className="size-3" />
                {r.episode_span}
              </Badge>
            )}
            {r.resolution && <Badge variant="outline">{r.resolution}</Badge>}
            {r.size && (
              <span className="inline-flex items-center gap-1">
                <HardDrive className="size-3" />
                {r.size}
              </span>
            )}
            <span className={cn("inline-flex items-center gap-1 font-medium", health)}>
              <Users className="size-3" />
              {seeders} seeders
            </span>
          </div>
          {r.recommended && r.reason && (
            <div className="mt-1.5 flex items-start gap-1 text-[0.7rem] text-brand-bright">
              <Star className="mt-0.5 size-3 shrink-0 fill-brand-bright" />
              <span className="line-clamp-2">{r.reason}</span>
            </div>
          )}
        </div>
        <Button
          variant={r.recommended ? "primary" : "secondary"}
          size="sm"
          onClick={onPick}
          disabled={busy || (!r.magnet && !r.torrent_url)}
          className="shrink-0"
          title={selectable ? "Pick which episodes to download" : "Download this pack"}
        >
          <DownloadCloud className="size-3.5" />
          {selectable ? "Download…" : "Download"}
        </Button>
      </div>
    </div>
  );
}

function TitleSkeleton() {
  return (
    <div className="-mx-5 -mt-20 md:-mx-8 md:-mt-8">
      <Skeleton className="h-56 w-full rounded-none md:h-72" />
      <div className="px-5 md:px-8">
        <div className="-mt-20 flex gap-6">
          <Skeleton className="hidden h-60 w-40 rounded-xl sm:block" />
          <div className="flex-1 space-y-3 pt-24">
            <Skeleton className="h-9 w-2/3" />
            <Skeleton className="h-4 w-1/3" />
            <Skeleton className="h-20 w-full max-w-3xl" />
          </div>
        </div>
        <div className="mt-8 space-y-2">
          {Array.from({ length: 4 }).map((_, i) => (
            <Skeleton key={i} className="h-16 w-full rounded-xl" />
          ))}
        </div>
      </div>
    </div>
  );
}

/** Remove the title + ALL its data (episodes, subtitle corpus, downloads,
 * files). Cascades server-side (DELETE /api/catalog/titles/{id}); the nightly
 * DB backup is the only undo. */
function DeleteTitleButton({ anilistId }: { anilistId: number }) {
  const navigate = useNavigate();
  const del = useDeleteTitle(anilistId);
  return (
    <Button
      variant="ghost"
      size="sm"
      loading={del.isPending}
      title="Remove this title and all of its data from the library"
      className="text-muted hover:text-red-400"
      onClick={() => {
        if (
          window.confirm(
            "Remove this title from the library?\n\nThis deletes its episodes, the whole subtitle corpus (lines, search index), downloads, clips, and any files on disk. It cannot be undone from the UI.",
          )
        ) {
          del.mutate(undefined, { onSuccess: () => navigate("/") });
        }
      }}
    >
      <Trash2 className="size-3.5" />
    </Button>
  );
}
