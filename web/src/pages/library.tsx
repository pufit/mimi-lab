import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Link, useNavigate } from "react-router-dom";
import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import {
  ArrowUpDown,
  Check,
  ChevronDown,
  FolderSearch,
  History,
  LayoutGrid,
  LibraryBig,
  MonitorPlay,
  Play,
  Rows3,
  RotateCw,
  Search,
  Sparkles,
  Star,
  Tv,
  X,
} from "lucide-react";
import {
  useTitles,
  useScan,
  useMalSync,
  useAnalyzeLibrary,
  useContinueWatching,
  useConnectorStatus,
  usePlay,
} from "@/lib/hooks";
import { PageHeader } from "@/components/layout/page-header";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { EmptyState } from "@/components/ui/empty-state";
import { ErrorState } from "@/components/ui/error-state";
import { Skeleton } from "@/components/ui/skeleton";
import { ComprehensionPill } from "@/components/comprehension";
import { BrowserPlayer } from "@/components/browser-player";
import type { ContinueEntry } from "@/lib/types";
import {
  cn,
  titleName,
  titleSub,
  malStatusLabel,
  stripHtml,
  formatType,
  librarySortComparator,
  MAL_CATEGORY_ORDER,
  MAL_FILTERS,
  LIBRARY_SORTS,
  type LibrarySort,
} from "@/lib/utils";
import type { Title } from "@/lib/types";

type ViewMode = "full" | "compact";

const OTHER_KEY = "__other";

interface Group {
  key: string;
  label: string;
  items: Title[];
}

/** Group titles by MAL status in the fixed category order, sorting within each group. */
function buildGroups(titles: Title[], sort: LibrarySort): Group[] {
  const cmp = librarySortComparator(sort);
  const byKey = new Map<string, Title[]>();
  for (const t of titles) {
    const k = t.mal_status ?? OTHER_KEY;
    const arr = byKey.get(k);
    if (arr) arr.push(t);
    else byKey.set(k, [t]);
  }
  const out: Group[] = [];
  for (const key of MAL_CATEGORY_ORDER) {
    const items = byKey.get(key);
    if (items?.length) out.push({ key, label: malStatusLabel(key), items: items.slice().sort(cmp) });
  }
  // Anything without a MAL status goes last so nothing silently disappears.
  const other = byKey.get(OTHER_KEY);
  if (other?.length) out.push({ key: OTHER_KEY, label: "Not on list", items: other.slice().sort(cmp) });
  return out;
}

function usePersistedChoice<T extends string>(key: string, allowed: readonly T[], fallback: T) {
  const [value, setValue] = useState<T>(() => {
    try {
      const stored = localStorage.getItem(key) as T | null;
      if (stored && allowed.includes(stored)) return stored;
    } catch {
      /* ignore */
    }
    return fallback;
  });
  const set = (v: T) => {
    setValue(v);
    try {
      localStorage.setItem(key, v);
    } catch {
      /* ignore */
    }
  };
  return [value, set] as const;
}

export function LibraryPage() {
  const { data: titles, isLoading, isError, refetch } = useTitles();
  const scan = useScan();
  const malSync = useMalSync();
  const analyze = useAnalyzeLibrary();

  const [filter, setFilter] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [view, setView] = usePersistedChoice<ViewMode>(
    "migaku.library.view",
    ["full", "compact"],
    "full",
  );
  const [sort, setSort] = usePersistedChoice<LibrarySort>(
    "migaku.library.sort",
    ["name", "score", "comprehension"],
    "name",
  );
  const [collapsed, setCollapsed] = useState<Set<string>>(() => new Set());

  const counts = useMemo(() => {
    const c: Record<string, number> = {};
    for (const t of titles ?? []) {
      if (t.mal_status) c[t.mal_status] = (c[t.mal_status] ?? 0) + 1;
    }
    return c;
  }, [titles]);

  // client-side text search over romaji/english
  const needle = search.trim().toLowerCase();
  const searched = useMemo(() => {
    if (!needle) return titles ?? [];
    return (titles ?? []).filter(
      (t) =>
        (t.romaji ?? "").toLowerCase().includes(needle) ||
        (t.english ?? "").toLowerCase().includes(needle),
    );
  }, [titles, needle]);

  const groups = useMemo(() => buildGroups(searched, sort), [searched, sort]);
  const visibleGroups = filter ? groups.filter((g) => g.key === filter) : groups;
  const totalVisible = visibleGroups.reduce((n, g) => n + g.items.length, 0);

  const toggleCollapse = (key: string) =>
    setCollapsed((prev) => {
      const next = new Set(prev);
      next.has(key) ? next.delete(key) : next.add(key);
      return next;
    });

  const sortLabel = LIBRARY_SORTS.find((s) => s.value === sort)?.label.toLowerCase() ?? "name";

  const actions = (
    <>
      <Button
        variant="ghost"
        onClick={() => scan.mutate()}
        loading={scan.isPending}
        title="Scan the Library folder for downloaded video files"
      >
        <FolderSearch className="size-4" />
        Scan files
      </Button>
      <Button variant="secondary" onClick={() => malSync.mutate()} loading={malSync.isPending}>
        <RotateCw className="size-4" />
        Sync MAL
      </Button>
      <Button
        variant="primary"
        onClick={() => analyze.mutate(undefined)}
        loading={analyze.isPending}
        title="Fetch subtitles from jimaku and score comprehension for every title — no download needed"
      >
        <Sparkles className="size-4" />
        Analyze comprehension
      </Button>
    </>
  );

  return (
    <div className="animate-fade-in">
      <PageHeader
        title="Library"
        subtitle={
          titles?.length
            ? `${titles.length} title${titles.length === 1 ? "" : "s"} · grouped by status · sorted by ${sortLabel}`
            : "Your anime, ready to watch and mine"
        }
        actions={actions}
      />

      {/* Continue watching rail */}
      <ContinueRail />

      {/* Controls: search · status filter chips · sort · view mode */}
      {titles && titles.length > 0 && (
        <div className="mb-6 flex flex-wrap items-center gap-x-3 gap-y-2.5">
          <div className="relative w-full sm:w-56">
            <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-faint" />
            <Input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search titles…"
              aria-label="Search titles"
              className="h-9 pl-9 pr-8 text-sm"
            />
            {search && (
              <button
                onClick={() => setSearch("")}
                aria-label="Clear search"
                className="absolute right-2 top-1/2 grid size-5 -translate-y-1/2 place-items-center rounded text-faint hover:text-fg"
              >
                <X className="size-3.5" />
              </button>
            )}
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <Chip active={filter === null} onClick={() => setFilter(null)}>
              All
              <ChipCount>{searched.length}</ChipCount>
            </Chip>
            {MAL_FILTERS.filter((f) => counts[f.value]).map((f) => (
              <Chip key={f.value} active={filter === f.value} onClick={() => setFilter(f.value)}>
                {f.label}
                <ChipCount>{counts[f.value]}</ChipCount>
              </Chip>
            ))}
          </div>
          <div className="ml-auto flex items-center gap-2">
            <SortMenu value={sort} onChange={setSort} />
            <ViewToggle value={view} onChange={setView} />
          </div>
        </div>
      )}

      {isLoading && (view === "compact" ? <CompactSkeleton /> : <PosterGridSkeleton />)}

      {isError && <ErrorState onRetry={() => refetch()} />}

      {titles && titles.length === 0 && (
        <EmptyState
          icon={LibraryBig}
          title="Your library is empty"
          description="Sync your MyAnimeList to pull your list, then Analyze comprehension to score every title — or head to Acquire to grab a series from nyaa."
          action={
            <>
              <Button variant="primary" onClick={() => malSync.mutate()} loading={malSync.isPending}>
                <RotateCw className="size-4" />
                Sync MyAnimeList
              </Button>
              <Button variant="secondary" asChild>
                <Link to="/acquire">Browse nyaa</Link>
              </Button>
            </>
          }
        />
      )}

      {titles && titles.length > 0 && totalVisible === 0 && (
        <EmptyState
          icon={Tv}
          title={needle ? "Nothing matches your search" : "Nothing matches this filter"}
          description={
            needle
              ? `No titles matching "${search.trim()}"${filter ? ` with status "${malStatusLabel(filter)}"` : ""}.`
              : `No titles with status "${malStatusLabel(filter)}".`
          }
          action={
            <Button
              variant="secondary"
              onClick={() => {
                setFilter(null);
                setSearch("");
              }}
            >
              Clear {needle ? "search" : "filter"}
            </Button>
          }
        />
      )}

      {totalVisible > 0 && (
        <div className={view === "compact" ? "space-y-5" : "space-y-8"}>
          {visibleGroups.map((g) => (
            <CategoryBlock
              key={g.key}
              group={g}
              view={view}
              collapsed={collapsed.has(g.key)}
              onToggle={() => toggleCollapse(g.key)}
            />
          ))}
        </div>
      )}
    </div>
  );
}

// ── Continue watching rail ──────────────────────────────────────────────────
function ContinueRail() {
  const { data } = useContinueWatching();
  if (!data || data.length === 0) return null;
  return (
    <section className="mb-8">
      <h2 className="mb-3 flex items-center gap-2 text-sm font-semibold uppercase tracking-wide text-fg">
        <History className="size-4 text-brand-bright" />
        Continue watching
      </h2>
      <div className="-mx-1 flex gap-3 overflow-x-auto px-1 pb-2">
        {data.map((c) => (
          <ContinueCard key={`${c.kind}-${c.episode_id}`} entry={c} />
        ))}
      </div>
    </section>
  );
}

function ContinueCard({ entry }: { entry: ContinueEntry }) {
  const play = usePlay(); // per-card instance so only this card spins
  const { data: connector } = useConnectorStatus();
  const online = !!connector?.connected;
  const [browserOpen, setBrowserOpen] = useState(false);

  const pct =
    entry.kind === "resume" && entry.progress_ms != null && entry.duration_ms
      ? Math.min(100, (entry.progress_ms / entry.duration_ms) * 100)
      : null;

  return (
    <div className="flex w-72 shrink-0 gap-3 rounded-2xl border border-border-strong bg-surface/50 p-3 transition-colors hover:border-brand/40">
      <Link to={`/title/${entry.anilist_id}`} className="shrink-0">
        {entry.cover_url ? (
          <img
            src={entry.cover_url}
            alt=""
            loading="lazy"
            className="h-24 w-16 rounded-lg object-cover"
          />
        ) : (
          <div className="grid h-24 w-16 place-items-center rounded-lg bg-bg-elevated text-faint">
            <Tv className="size-5" />
          </div>
        )}
      </Link>
      <div className="flex min-w-0 flex-1 flex-col">
        <Link
          to={`/title/${entry.anilist_id}`}
          className="line-clamp-2 text-sm font-semibold leading-snug text-fg hover:text-brand-bright"
        >
          {entry.title}
        </Link>
        <div className="mt-0.5 flex items-center gap-1.5 text-xs text-muted">
          E{entry.ep_number}
          <span className="text-faint">· {entry.kind === "resume" ? "resume" : "up next"}</span>
          <ComprehensionPill pct={entry.comprehension_pct} size="sm" />
        </div>
        {pct != null && (
          <div className="mt-1.5 h-1 w-full overflow-hidden rounded-full bg-bg-elevated">
            <div className="h-full rounded-full bg-brand-bright" style={{ width: `${pct}%` }} />
          </div>
        )}
        <div className="mt-auto flex items-center gap-1.5 pt-2">
          {online ? (
            <>
              <Button
                size="sm"
                variant="play"
                loading={play.isPending}
                onClick={() =>
                  play.mutate({
                    episodeId: entry.episode_id,
                    seekMs: entry.kind === "resume" ? entry.progress_ms ?? undefined : undefined,
                  })
                }
              >
                <Play className="size-3.5 fill-current" />
                Play
              </Button>
              <Button
                size="icon-sm"
                variant="ghost"
                onClick={() => setBrowserOpen(true)}
                aria-label="Play in browser"
                title="Play in browser (fallback player)"
              >
                <MonitorPlay className="size-4" />
              </Button>
            </>
          ) : (
            <Button size="sm" variant="play" onClick={() => setBrowserOpen(true)}>
              <MonitorPlay className="size-3.5" />
              Play in browser
            </Button>
          )}
        </div>
      </div>
      {browserOpen && (
        <BrowserPlayer
          episodeId={entry.episode_id}
          title={`${entry.title} · E${entry.ep_number}`}
          onClose={() => setBrowserOpen(false)}
        />
      )}
    </div>
  );
}

// ── Category block (shared by both view modes) ─────────────────────────────
function CategoryBlock({
  group,
  view,
  collapsed,
  onToggle,
}: {
  group: Group;
  view: ViewMode;
  collapsed: boolean;
  onToggle: () => void;
}) {
  return (
    <section>
      <SectionHeader
        label={group.label}
        count={group.items.length}
        collapsed={collapsed}
        onToggle={onToggle}
      />
      {!collapsed &&
        (view === "compact" ? (
          <CompactTable items={group.items} />
        ) : (
          <div className="mt-3 grid grid-cols-2 gap-x-4 gap-y-7 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5 xl:grid-cols-6">
            {group.items.map((t) => (
              <PosterCard key={t.anilist_id} title={t} />
            ))}
          </div>
        ))}
    </section>
  );
}

function SectionHeader({
  label,
  count,
  collapsed,
  onToggle,
}: {
  label: string;
  count: number;
  collapsed: boolean;
  onToggle: () => void;
}) {
  return (
    <button
      onClick={onToggle}
      aria-expanded={!collapsed}
      className="group flex w-full items-center justify-between gap-3 rounded-lg border border-border bg-surface/60 px-4 py-2.5 text-left transition-colors hover:border-border-strong hover:bg-surface"
    >
      <span className="flex items-center gap-2.5">
        <ChevronDown
          className={cn("size-4 text-faint transition-transform", collapsed && "-rotate-90")}
        />
        <span className="text-sm font-semibold uppercase tracking-wide text-fg">{label}</span>
        <span className="rounded-full bg-bg-elevated px-2 py-0.5 text-xs font-medium tabular-nums text-muted">
          {count}
        </span>
      </span>
      <span className="text-xs text-faint transition-colors group-hover:text-muted">
        {collapsed ? "expand" : "collapse"}
      </span>
    </button>
  );
}

// ── Full (poster) view ─────────────────────────────────────────────────────
function PosterCard({ title }: { title: Title }) {
  const name = titleName(title);
  const sub = titleSub(title);
  return (
    <Link
      to={`/title/${title.anilist_id}`}
      className="group flex flex-col focus-visible:outline-none"
    >
      <div className="relative aspect-[2/3] overflow-hidden rounded-xl border border-border bg-surface ring-brand/0 transition-all duration-200 group-hover:-translate-y-1 group-hover:border-border-strong group-hover:shadow-2xl group-hover:shadow-black/40 group-focus-visible:ring-2 group-focus-visible:ring-brand">
        {title.cover_url ? (
          <img
            src={title.cover_url}
            alt={name}
            loading="lazy"
            className="size-full object-cover transition-transform duration-300 group-hover:scale-[1.04]"
          />
        ) : (
          <div className="grid size-full place-items-center bg-gradient-to-br from-surface to-bg-elevated">
            <Tv className="size-8 text-faint" />
          </div>
        )}

        {/* gradient scrim */}
        <div className="pointer-events-none absolute inset-x-0 bottom-0 h-2/5 bg-gradient-to-t from-black/80 to-transparent opacity-90" />

        {/* comprehension badge — opaque overlay so it stays legible over the art */}
        <div className="absolute left-2 top-2">
          <ComprehensionPill pct={title.avg_comprehension} variant="overlay" />
        </div>

        {/* episode count */}
        {title.episode_count_local > 0 && (
          <div className="absolute right-2 top-2 rounded-md border border-white/10 bg-black/55 px-1.5 py-0.5 text-[0.65rem] font-medium text-white/90 backdrop-blur-md">
            {title.episode_count_local} ep
          </div>
        )}

        {/* status pill bottom */}
        {title.mal_status && (
          <div className="absolute inset-x-2 bottom-2">
            <span className="inline-flex items-center rounded-md border border-white/10 bg-black/50 px-1.5 py-0.5 text-[0.62rem] font-medium uppercase tracking-wide text-white/85 backdrop-blur-md">
              {malStatusLabel(title.mal_status)}
            </span>
          </div>
        )}
      </div>

      <div className="mt-2.5 px-0.5">
        <p className="line-clamp-2 text-sm font-medium leading-snug text-fg transition-colors group-hover:text-brand-bright">
          {name}
        </p>
        {sub && <p className="mt-0.5 line-clamp-1 text-xs text-faint">{sub}</p>}
      </div>
    </Link>
  );
}

// ── Compact (table) view ────────────────────────────────────────────────────
function CompactTable({ items }: { items: Title[] }) {
  const [hover, setHover] = useState<{ title: Title; rect: DOMRect } | null>(null);
  const timers = useRef<{ show?: ReturnType<typeof setTimeout>; hide?: ReturnType<typeof setTimeout> }>(
    {},
  );

  const show = (title: Title, rect: DOMRect) => {
    clearTimeout(timers.current.hide);
    timers.current.show = setTimeout(() => setHover({ title, rect }), 150);
  };
  const scheduleHide = () => {
    clearTimeout(timers.current.show);
    timers.current.hide = setTimeout(() => setHover(null), 140);
  };
  const keepOpen = () => clearTimeout(timers.current.hide);

  useEffect(
    () => () => {
      clearTimeout(timers.current.show);
      clearTimeout(timers.current.hide);
    },
    [],
  );

  return (
    <div className="mt-3 overflow-hidden rounded-xl border border-border bg-surface/40">
      <table className="w-full border-collapse text-sm">
        <thead>
          <tr className="border-b border-border text-left text-[0.7rem] uppercase tracking-wide text-faint">
            <th className="w-10 py-2 pl-4 pr-1 text-right font-medium">#</th>
            <th className="py-2 pr-3 font-medium">Title</th>
            <th className="w-16 px-2 py-2 text-center font-medium">Score</th>
            <th className="w-28 px-2 py-2 text-center font-medium">Comprehension</th>
            <th className="hidden w-24 px-2 py-2 text-right font-medium sm:table-cell">Episodes</th>
            <th className="hidden w-24 py-2 pl-2 pr-4 font-medium md:table-cell">Type</th>
          </tr>
        </thead>
        <tbody>
          {items.map((t, i) => (
            <CompactRow
              key={t.anilist_id}
              title={t}
              index={i + 1}
              onEnter={(rect) => show(t, rect)}
              onLeave={scheduleHide}
            />
          ))}
        </tbody>
      </table>

      {hover && (
        <TitleHoverCard
          title={hover.title}
          anchor={hover.rect}
          onMouseEnter={keepOpen}
          onMouseLeave={scheduleHide}
        />
      )}
    </div>
  );
}

function CompactRow({
  title,
  index,
  onEnter,
  onLeave,
}: {
  title: Title;
  index: number;
  onEnter: (rect: DOMRect) => void;
  onLeave: () => void;
}) {
  const navigate = useNavigate();
  const to = `/title/${title.anilist_id}`;
  const name = titleName(title);
  const sub = titleSub(title);
  const prog = title.mal_progress ?? 0;
  const total = title.total_episodes ?? (title.episode_count_local || null);

  return (
    <tr
      onMouseEnter={(e) => onEnter(e.currentTarget.getBoundingClientRect())}
      onMouseLeave={onLeave}
      onClick={() => navigate(to)}
      className="group cursor-pointer border-b border-border/40 transition-colors last:border-0 hover:bg-surface-hover/70"
    >
      <td className="py-2 pl-4 pr-1 text-right text-xs tabular-nums text-faint">{index}</td>
      <td className="min-w-0 py-2 pr-3">
        <Link
          to={to}
          onClick={(e) => e.stopPropagation()}
          className="line-clamp-1 font-medium text-fg transition-colors group-hover:text-brand-bright"
        >
          {name}
        </Link>
        {sub && <p className="line-clamp-1 text-xs text-faint">{sub}</p>}
      </td>
      <td className="px-2 py-2 text-center tabular-nums">
        {title.mal_score ? (
          <span className="inline-flex items-center gap-1 font-semibold text-fg">
            <Star className="size-3 fill-comp-amber text-comp-amber" />
            {title.mal_score}
          </span>
        ) : (
          <span className="text-faint">–</span>
        )}
      </td>
      <td className="px-2 py-2">
        <div className="flex justify-center">
          <ComprehensionPill pct={title.avg_comprehension} size="sm" />
        </div>
      </td>
      <td className="hidden px-2 py-2 text-right text-xs tabular-nums text-muted sm:table-cell">
        {total != null ? `${prog} / ${total}` : prog || "—"}
      </td>
      <td className="hidden py-2 pl-2 pr-4 text-xs text-muted md:table-cell">
        {formatType(title.format)}
      </td>
    </tr>
  );
}

/** Floating preview card shown when hovering a compact row. */
function TitleHoverCard({
  title,
  anchor,
  onMouseEnter,
  onMouseLeave,
}: {
  title: Title;
  anchor: DOMRect;
  onMouseEnter: () => void;
  onMouseLeave: () => void;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [pos, setPos] = useState<{ top: number; left: number } | null>(null);

  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const cw = el.offsetWidth;
    const ch = el.offsetHeight;
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    // Prefer just right of the row's title area; flip to the left edge if it
    // would overflow, then clamp fully into the viewport.
    let left = anchor.left + 52;
    if (left + cw + 12 > vw) left = anchor.right - cw;
    left = Math.max(12, Math.min(left, vw - cw - 12));
    let top = Math.max(12, Math.min(anchor.top - 16, vh - ch - 12));
    setPos({ top, left });
  }, [anchor]);

  const name = titleName(title);
  const synopsis = stripHtml(title.description);
  const prog = title.mal_progress ?? 0;
  const total = title.total_episodes ?? (title.episode_count_local || null);

  return createPortal(
    <div
      ref={ref}
      onMouseEnter={onMouseEnter}
      onMouseLeave={onMouseLeave}
      style={{
        top: pos?.top ?? anchor.top,
        left: pos?.left ?? anchor.left,
        visibility: pos ? "visible" : "hidden",
      }}
      className="animate-fade-in fixed z-[65] w-96 max-w-[calc(100vw-24px)] overflow-hidden rounded-2xl border border-border-strong bg-bg-elevated/95 shadow-2xl shadow-black/60 backdrop-blur-xl"
    >
      <div className="flex gap-3.5 p-3.5">
        <div className="w-24 shrink-0 overflow-hidden rounded-lg border border-border">
          {title.cover_url ? (
            <img src={title.cover_url} alt="" className="aspect-[2/3] w-full object-cover" />
          ) : (
            <div className="grid aspect-[2/3] w-full place-items-center bg-surface">
              <Tv className="size-6 text-faint" />
            </div>
          )}
        </div>
        <div className="min-w-0 flex-1">
          <p className="line-clamp-2 text-sm font-semibold leading-snug text-fg">{name}</p>
          {title.native && (
            <p className="font-jp mt-0.5 line-clamp-1 text-xs text-faint">{title.native}</p>
          )}
          <div className="mt-2 flex flex-wrap items-center gap-x-2 gap-y-1 text-[0.7rem] text-muted">
            {title.format && <Badge variant="solid">{formatType(title.format)}</Badge>}
            {title.year && <span>{title.year}</span>}
            {total != null && <span>{total} eps</span>}
          </div>
          <div className="mt-2.5 flex flex-wrap items-center gap-2.5">
            {title.mal_score ? (
              <span className="inline-flex items-center gap-1 text-xs text-muted">
                <Star className="size-3 fill-comp-amber text-comp-amber" />
                <span className="font-semibold text-fg">{title.mal_score}</span>/ 10
              </span>
            ) : null}
            <ComprehensionPill pct={title.avg_comprehension} size="sm" showLabel />
          </div>
        </div>
      </div>

      {synopsis && (
        <p className="line-clamp-4 px-3.5 text-xs leading-relaxed text-muted">{synopsis}</p>
      )}

      <div className="mt-3 flex items-center justify-between gap-2 border-t border-border p-3">
        <span className="inline-flex items-center gap-1.5 text-[0.7rem] text-faint">
          <span className="uppercase tracking-wide">{malStatusLabel(title.mal_status)}</span>
          {prog > 0 && total ? <span>· {prog}/{total} watched</span> : null}
        </span>
        <Button asChild size="sm" variant="primary">
          <Link to={`/title/${title.anilist_id}`}>Open</Link>
        </Button>
      </div>
    </div>,
    document.body,
  );
}

// ── Controls ────────────────────────────────────────────────────────────────
function ViewToggle({ value, onChange }: { value: ViewMode; onChange: (v: ViewMode) => void }) {
  const opts = [
    { v: "full" as const, icon: LayoutGrid, label: "Full" },
    { v: "compact" as const, icon: Rows3, label: "Compact" },
  ];
  return (
    <div className="inline-flex items-center gap-0.5 rounded-lg border border-border-strong bg-surface p-0.5">
      {opts.map(({ v, icon: Icon, label }) => (
        <button
          key={v}
          onClick={() => onChange(v)}
          aria-pressed={value === v}
          title={`${label} view`}
          className={cn(
            "inline-flex items-center gap-1.5 rounded-md px-2.5 py-1.5 text-xs font-medium transition-colors",
            value === v ? "bg-bg-elevated text-fg shadow-sm" : "text-muted hover:text-fg",
          )}
        >
          <Icon className="size-4" />
          <span className="hidden sm:inline">{label}</span>
        </button>
      ))}
    </div>
  );
}

function SortMenu({ value, onChange }: { value: LibrarySort; onChange: (v: LibrarySort) => void }) {
  const current = LIBRARY_SORTS.find((s) => s.value === value) ?? LIBRARY_SORTS[0];
  return (
    <DropdownMenu.Root>
      <DropdownMenu.Trigger asChild>
        <button className="inline-flex items-center gap-1.5 rounded-lg border border-border-strong bg-surface px-3 py-1.5 text-xs font-medium text-fg transition-colors hover:border-faint hover:bg-surface-hover">
          <ArrowUpDown className="size-3.5 text-faint" />
          <span className="text-muted">Sort</span>
          <span>{current.label}</span>
          <ChevronDown className="size-3.5 text-faint" />
        </button>
      </DropdownMenu.Trigger>
      <DropdownMenu.Portal>
        <DropdownMenu.Content
          align="end"
          sideOffset={8}
          className="z-50 w-44 overflow-hidden rounded-xl border border-border bg-bg-elevated/95 p-1 shadow-2xl backdrop-blur-xl"
        >
          <DropdownMenu.RadioGroup value={value} onValueChange={(v) => onChange(v as LibrarySort)}>
            {LIBRARY_SORTS.map((s) => (
              <DropdownMenu.RadioItem
                key={s.value}
                value={s.value}
                className="flex cursor-pointer select-none items-center justify-between rounded-lg px-2.5 py-1.5 text-sm text-muted outline-none transition-colors data-[highlighted]:bg-surface-hover data-[highlighted]:text-fg data-[state=checked]:font-medium data-[state=checked]:text-fg"
              >
                {s.label}
                <DropdownMenu.ItemIndicator>
                  <Check className="size-4 text-brand-bright" />
                </DropdownMenu.ItemIndicator>
              </DropdownMenu.RadioItem>
            ))}
          </DropdownMenu.RadioGroup>
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
  );
}

// ── Skeletons ────────────────────────────────────────────────────────────────
function PosterGridSkeleton() {
  return (
    <div className="grid grid-cols-2 gap-x-4 gap-y-7 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5 xl:grid-cols-6">
      {Array.from({ length: 12 }).map((_, i) => (
        <div key={i} className="flex flex-col">
          <Skeleton className="aspect-[2/3] w-full rounded-xl" />
          <Skeleton className="mt-2.5 h-4 w-4/5" />
          <Skeleton className="mt-1.5 h-3 w-3/5" />
        </div>
      ))}
    </div>
  );
}

function CompactSkeleton() {
  return (
    <div className="space-y-5">
      {Array.from({ length: 2 }).map((_, g) => (
        <div key={g}>
          <Skeleton className="h-10 w-full rounded-lg" />
          <div className="mt-3 space-y-2 rounded-xl border border-border p-3">
            {Array.from({ length: 6 }).map((_, i) => (
              <Skeleton key={i} className="h-6 w-full" />
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}

function Chip({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      onClick={onClick}
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full border px-3.5 py-1.5 text-xs font-medium transition-all",
        active
          ? "border-brand bg-brand/20 text-brand-bright"
          : "border-border-strong bg-surface text-muted hover:border-faint hover:text-fg",
      )}
    >
      {children}
    </button>
  );
}

function ChipCount({ children }: { children: React.ReactNode }) {
  return <span className="text-[0.65rem] opacity-60">{children}</span>;
}
