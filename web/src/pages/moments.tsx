import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import {
  Search,
  Play,
  Volume2,
  Pause,
  Sparkles,
  Languages,
  Scissors,
  Download,
  Package,
  ChevronDown,
  ListOrdered,
  Pickaxe,
} from "lucide-react";
import {
  useMoments,
  usePlayMoment,
  useClip,
  useAnkiExport,
  useApkgExport,
  useTranslateLine,
  MOMENTS_PAGE,
} from "@/lib/hooks";
import { PageHeader } from "@/components/layout/page-header";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { EmptyState } from "@/components/ui/empty-state";
import { ErrorState } from "@/components/ui/error-state";
import { Skeleton } from "@/components/ui/skeleton";
import { Furigana } from "@/components/furigana";
import { toast } from "@/components/ui/toast";
import { cn, formatMs } from "@/lib/utils";
import type { Moment, MomentSort, ClipResult } from "@/lib/types";

const SUGGESTIONS = ["時間", "大丈夫", "気持ち", "世界", "本当"];

const SORTS: { value: MomentSort; label: string; icon: typeof ListOrdered; hint: string }[] = [
  { value: "position", label: "In order", icon: ListOrdered, hint: "Lines in episode order" },
  {
    value: "iplus1",
    label: "Best for mining",
    icon: Pickaxe,
    hint: "i+1 first — sentences with the fewest unknown words",
  },
];

export function MomentsPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const [input, setInput] = useState(() => searchParams.get("q") ?? "");
  const [query, setQuery] = useState(() => (searchParams.get("q") ?? "").trim());
  const [sort, setSort] = useState<MomentSort>("position");
  const [show, setShow] = useState<{ id: number; title: string } | null>(null);

  // debounce input → query
  useEffect(() => {
    const t = setTimeout(() => setQuery(input.trim()), 350);
    return () => clearTimeout(t);
  }, [input]);

  // keep ?q= in the URL (replace, not push)
  useEffect(() => {
    if ((searchParams.get("q") ?? "") === query) return;
    const next = new URLSearchParams(searchParams);
    if (query) next.set("q", query);
    else next.delete("q");
    setSearchParams(next, { replace: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [query]);

  // adopt external URL changes (e.g. arriving from the Study queue)
  useEffect(() => {
    const urlQ = (searchParams.get("q") ?? "").trim();
    if (urlQ !== query) {
      setInput(urlQ);
      setQuery(urlQ);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchParams]);

  // the show filter only makes sense within one search
  useEffect(() => setShow(null), [query]);

  const {
    data,
    isLoading,
    isFetching,
    isError,
    refetch,
    hasNextPage,
    fetchNextPage,
    isFetchingNextPage,
  } = useMoments(query, sort, show?.id ?? null);

  const moments = useMemo(() => data?.pages.flat() ?? [], [data]);

  // distinct (anilist_id, title) pairs in the current results; the active
  // selection stays listed even when results narrow to that one show.
  const shows = useMemo(() => {
    const map = new Map<number, string>();
    if (show) map.set(show.id, show.title);
    for (const m of moments) {
      if (m.title && !map.has(m.anilist_id)) map.set(m.anilist_id, m.title);
    }
    return [...map.entries()]
      .map(([id, title]) => ({ id, title }))
      .sort((a, b) => a.title.localeCompare(b.title));
  }, [moments, show]);

  const apkg = useApkgExport();
  const exportAll = () =>
    apkg.mutate({
      lineIds: moments.map((m) => m.line_id),
      deck: `Mimi Lab - ${query}`,
    });

  return (
    <div className="animate-fade-in">
      <PageHeader
        title="Moments"
        subtitle="Every line in your library that uses a word — your personal immersion bank."
      />

      {/* search */}
      <div className="relative mb-5 max-w-2xl">
        <Search className="pointer-events-none absolute left-4 top-1/2 size-5 -translate-y-1/2 text-faint" />
        <Input
          autoFocus
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="Search a word… (e.g. 時間, 大丈夫, ある)"
          className="font-jp h-14 rounded-2xl pl-12 pr-4 text-lg"
        />
        {isFetching && query && (
          <div className="absolute right-4 top-1/2 size-4 -translate-y-1/2 animate-spin rounded-full border-2 border-border border-t-brand-bright" />
        )}
      </div>

      {/* sort chips + show filter */}
      {query && (
        <div className="mb-6 flex flex-wrap items-center gap-2">
          {SORTS.map((s) => (
            <button
              key={s.value}
              onClick={() => setSort(s.value)}
              title={s.hint}
              className={cn(
                "inline-flex items-center gap-1.5 rounded-full border px-3.5 py-1.5 text-xs font-medium transition-all",
                sort === s.value
                  ? "border-brand bg-brand/20 text-brand-bright"
                  : "border-border-strong bg-surface text-muted hover:border-faint hover:text-fg",
              )}
            >
              <s.icon className="size-3.5" />
              {s.label}
            </button>
          ))}

          {shows.length > 0 && (
            <div className="relative ml-auto">
              <select
                value={show?.id ?? ""}
                onChange={(e) => {
                  const id = Number(e.target.value);
                  const picked = shows.find((s) => s.id === id);
                  setShow(picked ?? null);
                }}
                aria-label="Filter by show"
                className="h-9 max-w-64 appearance-none truncate rounded-lg border border-border-strong bg-surface pl-3 pr-8 text-xs font-medium text-fg transition-colors hover:border-faint focus-visible:border-brand focus-visible:outline-none"
              >
                <option value="">All shows</option>
                {shows.map((s) => (
                  <option key={s.id} value={s.id}>
                    {s.title}
                  </option>
                ))}
              </select>
              <ChevronDown className="pointer-events-none absolute right-2.5 top-1/2 size-3.5 -translate-y-1/2 text-faint" />
            </div>
          )}
        </div>
      )}

      {/* suggestions */}
      {!query && (
        <div className="mb-8 flex flex-wrap items-center gap-2">
          <span className="text-xs text-faint">Try:</span>
          {SUGGESTIONS.map((s) => (
            <button
              key={s}
              onClick={() => setInput(s)}
              className="font-jp rounded-full border border-border-strong bg-surface px-3.5 py-1.5 text-sm text-muted transition-colors hover:border-brand hover:text-brand-bright"
            >
              {s}
            </button>
          ))}
        </div>
      )}

      {!query && (
        <EmptyState
          icon={Sparkles}
          title="Search your library"
          description="Look up any Japanese word to find every sentence where it appears — with screenshot, audio, translation, and one-click jump back into Migaku."
        />
      )}

      {query && isLoading && <MomentsSkeleton />}

      {query && isError && <ErrorState onRetry={() => refetch()} />}

      {query && data && moments.length === 0 && !isFetching && (
        <EmptyState
          icon={Search}
          title="No moments found"
          description={
            <>
              Nothing in your library uses <span className="font-jp text-fg">{query}</span>
              {show ? ` in ${show.title}` : ""} yet. Ingest more subtitles, or try the
              dictionary form of the word.
            </>
          }
        />
      )}

      {query && moments.length > 0 && (
        <>
          {/* bulk export bar */}
          <div className="mb-4 flex flex-wrap items-center gap-3 rounded-xl border border-border bg-surface/50 px-4 py-2.5">
            <p className="text-sm text-muted">
              <span className="font-semibold text-fg">{moments.length}</span>
              {hasNextPage ? "+" : ""} moment{moments.length === 1 ? "" : "s"} for{" "}
              <span className="font-jp text-brand-bright">{query}</span>
              {show && <span className="text-faint"> · {show.title}</span>}
            </p>
            <div className="ml-auto">
              <Button
                variant="secondary"
                size="sm"
                onClick={exportAll}
                loading={apkg.isPending}
                title="Build an Anki deck (.apkg with screenshots + audio) from every loaded moment"
              >
                <Package className="size-3.5" />
                Export all to Anki (.apkg)
              </Button>
            </div>
          </div>

          <div className="grid gap-4 lg:grid-cols-2">
            {moments.map((m) => (
              <MomentCard key={m.line_id} moment={m} showIplus1={sort === "iplus1"} />
            ))}
          </div>

          {hasNextPage && (
            <div className="mt-6 flex justify-center">
              <Button
                variant="secondary"
                onClick={() => fetchNextPage()}
                loading={isFetchingNextPage}
              >
                Load more
              </Button>
            </div>
          )}
        </>
      )}
    </div>
  );
}

function MomentCard({ moment, showIplus1 }: { moment: Moment; showIplus1: boolean }) {
  const play = usePlayMoment();
  const clip = useClip();
  const anki = useAnkiExport();
  const translate = useTranslateLine();
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const [playing, setPlaying] = useState(false);
  const [clipped, setClipped] = useState<ClipResult | null>(null);

  // prefer a freshly-generated clip over whatever the moment shipped with
  const imageUrl = clipped?.image_url ?? moment.image_url;
  const audioUrl = clipped?.audio_url ?? moment.audio_url;
  const hasVideo = !!moment.video_path;

  const toggleAudio = () => {
    if (!audioUrl) return;
    let el = audioRef.current;
    if (!el) {
      el = new Audio(audioUrl);
      el.onended = () => setPlaying(false);
      el.onpause = () => setPlaying(false);
      audioRef.current = el;
    }
    if (playing) {
      el.pause();
      setPlaying(false);
    } else {
      el.currentTime = 0;
      void el.play().catch(() => toast.error("Audio unavailable"));
      setPlaying(true);
    }
  };

  useEffect(() => {
    return () => {
      audioRef.current?.pause();
    };
  }, []);

  return (
    <article className="group flex gap-4 overflow-hidden rounded-2xl border border-border bg-surface/60 p-4 transition-colors hover:border-border-strong">
      {/* thumbnail — only when we actually have (or just made) an image; no
          empty grey placeholder boxes for video-less episodes */}
      {imageUrl && (
        <div className="relative aspect-video w-40 shrink-0 self-start overflow-hidden rounded-lg border border-border bg-bg-elevated">
          <img src={imageUrl} alt="" loading="lazy" className="size-full object-cover" />
          {audioUrl && (
            <button
              onClick={toggleAudio}
              aria-label={playing ? "Pause audio" : "Play audio"}
              className="absolute bottom-1.5 right-1.5 grid size-8 place-items-center rounded-full bg-black/60 text-white backdrop-blur-md transition-colors hover:bg-brand"
            >
              {playing ? (
                <Pause className="size-3.5 fill-current" />
              ) : (
                <Volume2 className="size-3.5" />
              )}
            </button>
          )}
        </div>
      )}

      {/* body */}
      <div className="flex min-w-0 flex-1 flex-col">
        <div className="flex items-start gap-2">
          <Furigana
            furigana={moment.text_furigana}
            text={moment.text}
            className="min-w-0 flex-1 text-lg leading-relaxed text-fg [&_rt]:text-brand-bright"
          />
          {showIplus1 && moment.unknown_count != null && (
            <Badge
              variant={moment.unknown_count <= 1 ? "success" : "warning"}
              className="mt-1 shrink-0 tabular-nums"
              title={`${moment.unknown_count} unknown word${moment.unknown_count === 1 ? "" : "s"} in this line`}
            >
              i+{moment.unknown_count}
            </Badge>
          )}
        </div>

        {moment.translation ? (
          <p className="mt-1.5 flex items-start gap-1.5 text-sm leading-relaxed text-muted">
            <Languages className="mt-0.5 size-3.5 shrink-0 text-faint" />
            <span>{moment.translation}</span>
          </p>
        ) : (
          <button
            onClick={() => translate.mutate(moment.line_id)}
            disabled={translate.isPending}
            className="mt-1.5 inline-flex items-center gap-1.5 self-start text-xs font-medium text-faint transition-colors hover:text-brand-bright disabled:opacity-60"
            title="Machine-translate this line"
          >
            <Languages className="size-3.5" />
            {translate.isPending ? "Translating…" : "Translate"}
          </button>
        )}

        {/* meta */}
        <div className="mt-2.5 flex min-w-0 items-center gap-2 text-xs text-faint">
          {moment.title && (
            <Link
              to={`/title/${moment.anilist_id}`}
              className="truncate font-medium text-muted transition-colors hover:text-brand-bright"
            >
              {moment.title}
            </Link>
          )}
          {moment.ep_number != null && <span className="shrink-0">· E{moment.ep_number}</span>}
          {moment.episode_title && (
            <span className="truncate text-faint">· {moment.episode_title}</span>
          )}
          <span className="shrink-0 tabular-nums">· {formatMs(moment.start_ms)}</span>
        </div>

        {/* actions */}
        <div className="mt-3 flex flex-wrap items-center gap-2">
          {hasVideo && (
            <Button
              variant="play"
              size="sm"
              onClick={() => play.mutate(moment.line_id)}
              loading={play.isPending}
            >
              <Play className="size-3.5 fill-current" />
              Play from here
            </Button>
          )}
          {!imageUrl && hasVideo && (
            <Button
              variant="ghost"
              size="sm"
              onClick={() => clip.mutate(moment.line_id, { onSuccess: setClipped })}
              loading={clip.isPending}
              title="Generate screenshot + audio clip from the video"
            >
              <Scissors className="size-3.5" />
              Make clip
            </Button>
          )}
          <Button
            variant="secondary"
            size="sm"
            onClick={() => anki.mutate([moment.line_id])}
            loading={anki.isPending}
            title="Export this sentence to Anki (TSV)"
          >
            <Download className="size-3.5" />
            Anki
          </Button>
        </div>
      </div>
    </article>
  );
}

function MomentsSkeleton() {
  return (
    <div className="grid gap-4 lg:grid-cols-2">
      {Array.from({ length: Math.min(6, MOMENTS_PAGE) }).map((_, i) => (
        <div key={i} className="flex gap-4 rounded-2xl border border-border bg-surface/60 p-4">
          <Skeleton className="aspect-video w-40 shrink-0 rounded-lg" />
          <div className="flex-1 space-y-2.5">
            <Skeleton className="h-6 w-full" />
            <Skeleton className="h-4 w-4/5" />
            <Skeleton className="h-3 w-2/5" />
            <div className="flex gap-2 pt-1">
              <Skeleton className="h-8 w-28 rounded-lg" />
              <Skeleton className="h-8 w-16 rounded-lg" />
            </div>
          </div>
        </div>
      ))}
    </div>
  );
}
