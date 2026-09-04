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
  ExternalLink,
  GraduationCap,
  ChevronDown,
  ListOrdered,
  Pickaxe,
} from "lucide-react";
import {
  useMoments,
  usePlayMoment,
  useClip,
  useTranslateLine,
  MOMENTS_PAGE,
} from "@/lib/hooks";
import { useQuery } from "@tanstack/react-query";
import { useCreateCard } from "@/lib/srs-hooks";
import { srsApi } from "@/lib/srs-api";
import { apiErrorBody } from "@/lib/api";
import { PageHeader } from "@/components/layout/page-header";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { EmptyState } from "@/components/ui/empty-state";
import { ErrorState } from "@/components/ui/error-state";
import { Skeleton } from "@/components/ui/skeleton";
import { Furigana } from "@/components/furigana";
import { MomentPlayer } from "@/components/moment-player";
import { toast } from "@/components/ui/toast";
import { cn, formatMs } from "@/lib/utils";
import type { Moment, MomentSort, ClipResult, SrsCreateCardConflict } from "@/lib/types";

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
  const [playing, setPlaying] = useState<Moment | null>(null);

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

  // A single word (no spaces) is a lemma we can turn into a study card.
  const lemma = query && !/\s/.test(query) ? query : null;

  // One lookup for the whole page: if the word already has a card, every moment
  // renders "Open card" up front instead of discovering it through a 409.
  const deckHit = useQuery({
    queryKey: ["srs", "lemma-card", lemma],
    // `q` is a substring match (価値 also returns 価値観), so ask for enough rows
    // to be sure the exact lemma is among them and then match it exactly.
    queryFn: () => srsApi.cards({ q: lemma ?? "", state: "all", limit: 50 }),
    enabled: !!lemma,
    staleTime: 30_000,
  });
  const existingCardId = lemma
    ? (deckHit.data?.items.find((c) => c.lemma === lemma)?.id ?? null)
    : null;

  return (
    <div className="animate-fade-in">
      <PageHeader
        title="Moments"
        subtitle="Every line from your downloaded episodes that uses a word — click a moment to play that sentence."
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
          title="Search your downloaded library"
          description="Look up any Japanese word to find every sentence where it appears in content you've downloaded — click a result to watch that exact line, clip it, or jump back into Migaku."
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
              Nothing you&apos;ve downloaded uses <span className="font-jp text-fg">{query}</span>
              {show ? ` in ${show.title}` : ""} yet. Download more episodes, or try the
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
            {lemma && (
              <div className="ml-auto">
                <Link to={`/study/stack?tab=upnext`}>
                  <Button
                    variant="ghost"
                    size="sm"
                    title="See which words are queued to become study cards"
                  >
                    <GraduationCap className="size-3.5" />
                    Study candidates
                  </Button>
                </Link>
              </div>
            )}
          </div>

          <div className="grid gap-4 lg:grid-cols-2">
            {moments.map((m) => (
              <MomentCard
                key={m.line_id}
                moment={m}
                lemma={lemma}
                existingCardId={existingCardId}
                showIplus1={sort === "iplus1"}
                onOpen={() => setPlaying(m)}
              />
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

      {playing && <MomentPlayer moment={playing} onClose={() => setPlaying(null)} />}
    </div>
  );
}

function MomentCard({
  moment,
  lemma,
  existingCardId,
  showIplus1,
  onOpen,
}: {
  moment: Moment;
  /** Non-null when the search is one word — that word can become a study card. */
  lemma: string | null;
  /** The card this lemma already has, resolved once for the whole page. */
  existingCardId: number | null;
  showIplus1: boolean;
  onOpen: () => void;
}) {
  const play = usePlayMoment();
  const clip = useClip();
  const translate = useTranslateLine();
  const create = useCreateCard();
  const [createdCardId, setCardId] = useState<number | null>(null);
  const cardId = createdCardId ?? existingCardId;
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

  // The whole card plays the sentence; inner controls stopPropagation.
  const openFromCard = () => {
    // don't hijack a text-selection drag (people copy sentences)
    if (window.getSelection()?.toString()) return;
    onOpen();
  };

  return (
    <article
      role={hasVideo ? "button" : undefined}
      tabIndex={hasVideo ? 0 : undefined}
      onClick={hasVideo ? openFromCard : undefined}
      onKeyDown={
        hasVideo
          ? (e) => {
              if ((e.key === "Enter" || e.key === " ") && e.target === e.currentTarget) {
                e.preventDefault();
                onOpen();
              }
            }
          : undefined
      }
      aria-label={hasVideo ? "Play this sentence" : undefined}
      className={cn(
        "group relative flex gap-4 overflow-hidden rounded-2xl border border-border bg-surface/60 p-4 transition-colors hover:border-border-strong",
        hasVideo &&
          "cursor-pointer focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand",
      )}
    >
      {/* hover affordance for cards without a thumbnail */}
      {hasVideo && !imageUrl && (
        <div
          aria-hidden
          className="pointer-events-none absolute right-3 top-3 grid size-8 place-items-center rounded-full bg-brand/90 text-white opacity-0 shadow-lg transition-opacity group-hover:opacity-100"
        >
          <Play className="size-3.5 fill-current" />
        </div>
      )}

      {/* thumbnail — only when we actually have (or just made) an image; no
          empty grey placeholder boxes for clip-less lines */}
      {imageUrl && (
        <div className="relative aspect-video w-40 shrink-0 self-start overflow-hidden rounded-lg border border-border bg-bg-elevated">
          <img src={imageUrl} alt="" loading="lazy" className="size-full object-cover" />
          {hasVideo && (
            <div
              aria-hidden
              className="pointer-events-none absolute inset-0 grid place-items-center bg-black/0 opacity-0 transition-all group-hover:bg-black/40 group-hover:opacity-100"
            >
              <Play className="size-7 fill-white text-white drop-shadow" />
            </div>
          )}
          {audioUrl && (
            <button
              onClick={(e) => {
                e.stopPropagation();
                toggleAudio();
              }}
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
            onClick={(e) => {
              e.stopPropagation();
              translate.mutate(moment.line_id);
            }}
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
              onClick={(e) => e.stopPropagation()}
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

        {/* actions — the card itself plays the sentence; these are extras */}
        <div className="mt-3 flex flex-wrap items-center gap-2">
          {hasVideo && (
            <Button
              variant="secondary"
              size="sm"
              onClick={(e) => {
                e.stopPropagation();
                play.mutate(moment.line_id);
              }}
              loading={play.isPending}
              title="Continue the episode from this line in Migaku — immersion features live"
            >
              <ExternalLink className="size-3.5" />
              Play in Migaku
            </Button>
          )}
          {!imageUrl && hasVideo && (
            <Button
              variant="ghost"
              size="sm"
              onClick={(e) => {
                e.stopPropagation();
                clip.mutate(moment.line_id, { onSuccess: setClipped });
              }}
              loading={clip.isPending}
              title="Generate screenshot + audio clip from the video"
            >
              <Scissors className="size-3.5" />
              Make clip
            </Button>
          )}
          {lemma &&
            (cardId ? (
              <Link to={`/study/cards/${cardId}`} onClick={(e) => e.stopPropagation()}>
                <Button variant="ghost" size="sm" title="This word is already in your deck">
                  <GraduationCap className="size-3.5" />
                  Open card
                </Button>
              </Link>
            ) : (
              <Button
                variant="secondary"
                size="sm"
                onClick={(e) => {
                  e.stopPropagation();
                  create.mutate(
                    { lemma, line_id: moment.line_id },
                    {
                      onSuccess: (res) => res.card && setCardId(res.card.id),
                      onError: (err) => {
                        const body = apiErrorBody<SrsCreateCardConflict>(err);
                        if (body?.card_id) {
                          setCardId(body.card_id);
                          toast({
                            title: `${lemma} is already in your deck`,
                            description: "Use Open card to go to it.",
                          });
                        } else {
                          toast.error(
                            "Couldn't add the card",
                            err instanceof Error ? err.message : undefined,
                          );
                        }
                      },
                    },
                  );
                }}
                loading={create.isPending}
                title="Make a study card from this sentence"
              >
                <GraduationCap className="size-3.5" />
                Add to Study
              </Button>
            ))}
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
