import { useState } from "react";
import {
  Search,
  DownloadCloud,
  ShieldCheck,
  ArrowDownToLine,
  Users,
  HardDrive,
  AlertTriangle,
  Plus,
  Trash2,
  Rss,
  Loader2,
  CheckCircle2,
  Layers,
  RotateCw,
  X,
} from "lucide-react";
import {
  useAcquireSearch,
  useDownloads,
  useAddDownload,
  useCancelDownload,
  useRetryDownload,
  useFollows,
  useAddFollow,
  useDeleteFollow,
  useQbt,
} from "@/lib/hooks";
import {
  DONE_STATES,
  TERMINAL_STATES,
  isFailedState,
  failedStateLabel,
} from "@/lib/download-states";
import { PageHeader } from "@/components/layout/page-header";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { cn, formatBytes } from "@/lib/utils";
import type { Download, NyaaResult, RssFollow } from "@/lib/types";

export function AcquirePage() {
  return (
    <div className="animate-fade-in">
      <PageHeader
        title="Acquire"
        subtitle="Search nyaa.si, queue torrents to Transmission, and follow shows for automatic grabs."
      />
      <QbtBanner />

      <div className="grid gap-6 lg:grid-cols-[1.6fr_1fr]">
        <NyaaSearch />
        <div className="space-y-6">
          <DownloadsPanel />
          <FollowsPanel />
        </div>
      </div>
    </div>
  );
}

function QbtBanner() {
  const { data, isLoading } = useQbt();
  if (isLoading || data?.available) return null;
  return (
    <div className="mb-6 flex items-center gap-3 rounded-xl border border-comp-amber/30 bg-comp-amber/10 px-4 py-3">
      <AlertTriangle className="size-4 shrink-0 text-comp-amber" />
      <p className="text-sm text-comp-amber/90">
        <span className="font-semibold">Transmission is offline.</span> You can browse and queue,
        but downloads won&apos;t start until it&apos;s reachable.
      </p>
    </div>
  );
}

function NyaaSearch() {
  const [input, setInput] = useState("");
  const [query, setQuery] = useState("");
  const [trusted, setTrusted] = useState(true);
  const { data, isLoading, isError, isFetching } = useAcquireSearch(
    query,
    trusted,
    query.length > 0,
  );
  const addDownload = useAddDownload();

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    setQuery(input.trim());
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle>Search nyaa.si</CardTitle>
        <form onSubmit={submit} className="mt-3 flex flex-col gap-3 sm:flex-row sm:items-center">
          <div className="relative flex-1">
            <Search className="pointer-events-none absolute left-3.5 top-1/2 size-4 -translate-y-1/2 text-faint" />
            <Input
              value={input}
              onChange={(e) => setInput(e.target.value)}
              placeholder="e.g. Example Series 1080p"
              className="pl-10"
            />
          </div>
          <label className="flex select-none items-center gap-2 text-sm text-muted">
            <input
              type="checkbox"
              checked={trusted}
              onChange={(e) => setTrusted(e.target.checked)}
              className="size-4 accent-[var(--color-brand)]"
            />
            Trusted only
          </label>
          <Button type="submit" variant="primary" loading={isFetching && query.length > 0}>
            <Search className="size-4" />
            Search
          </Button>
        </form>
      </CardHeader>
      <CardContent>
        {!query && (
          <p className="py-8 text-center text-sm text-muted">
            Enter a query to search nyaa.si (Anime · English-translated).
          </p>
        )}
        {query && isLoading && (
          <div className="space-y-2">
            {Array.from({ length: 5 }).map((_, i) => (
              <Skeleton key={i} className="h-12 w-full rounded-lg" />
            ))}
          </div>
        )}
        {query && isError && (
          <p className="py-8 text-center text-sm text-comp-red">
            Search failed — nyaa may be unreachable.
          </p>
        )}
        {query && data && data.length === 0 && (
          <p className="py-8 text-center text-sm text-muted">No results for “{query}”.</p>
        )}
        {data && data.length > 0 && (
          <div className="overflow-hidden rounded-lg border border-border">
            <table className="w-full text-sm">
              <thead className="bg-bg-elevated/60 text-left text-xs uppercase tracking-wide text-faint">
                <tr>
                  <th className="px-3 py-2 font-medium">Release</th>
                  <th className="hidden px-3 py-2 font-medium sm:table-cell">Size</th>
                  <th className="px-3 py-2 text-center font-medium">
                    <Users className="mx-auto size-3.5" />
                  </th>
                  <th className="px-3 py-2" />
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {data.map((r, i) => (
                  <NyaaRow
                    key={r.nyaa_id ?? `${r.title}-${i}`}
                    result={r}
                    onDownload={() => addDownload.mutate(r)}
                    busy={addDownload.isPending}
                  />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function NyaaRow({
  result,
  onDownload,
  busy,
}: {
  result: NyaaResult;
  onDownload: () => void;
  busy: boolean;
}) {
  return (
    <tr className="transition-colors hover:bg-surface-hover/50">
      <td className="px-3 py-2.5">
        <div className="flex items-start gap-2">
          {result.trusted && (
            <ShieldCheck
              className="mt-0.5 size-4 shrink-0 text-comp-green"
              aria-label="Trusted"
            />
          )}
          <span className="line-clamp-2 font-medium text-fg">{result.title}</span>
        </div>
      </td>
      <td className="hidden whitespace-nowrap px-3 py-2.5 text-muted sm:table-cell">
        {result.size ?? "—"}
      </td>
      <td className="px-3 py-2.5 text-center tabular-nums">
        <span
          className={cn(
            "font-medium",
            (result.seeders ?? 0) > 0 ? "text-comp-green" : "text-faint",
          )}
        >
          {result.seeders ?? 0}
        </span>
      </td>
      <td className="px-3 py-2.5 text-right">
        <Button
          variant="secondary"
          size="sm"
          onClick={onDownload}
          disabled={busy || (!result.magnet && !result.torrent_url)}
        >
          <ArrowDownToLine className="size-3.5" />
          Get
        </Button>
      </td>
    </tr>
  );
}

const DOWNLOADS_SHOWN = 50;

function DownloadsPanel() {
  const { data, isLoading } = useDownloads();
  const [showAll, setShowAll] = useState(false);
  // latest first — retries/new grabs surface at the top
  const all = (data ?? []).slice().sort((a, b) => b.id - a.id);
  const visible = showAll ? all : all.slice(0, DOWNLOADS_SHOWN);

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2">
          <DownloadCloud className="size-4 text-brand-bright" />
          Downloads
          {all.length > 0 && (
            <span className="text-xs font-normal text-faint">{all.length}</span>
          )}
        </CardTitle>
      </CardHeader>
      <CardContent className="pt-0">
        {isLoading ? (
          <div className="space-y-2">
            {Array.from({ length: 2 }).map((_, i) => (
              <Skeleton key={i} className="h-14 w-full rounded-lg" />
            ))}
          </div>
        ) : all.length === 0 ? (
          <p className="py-6 text-center text-sm text-muted">No active downloads.</p>
        ) : (
          <>
            <div className="space-y-2.5">
              {visible.map((d) => (
                <DownloadRow key={d.id} dl={d} />
              ))}
            </div>
            {!showAll && all.length > DOWNLOADS_SHOWN && (
              <Button
                variant="ghost"
                size="sm"
                className="mt-3 w-full"
                onClick={() => setShowAll(true)}
              >
                Show all ({all.length})
              </Button>
            )}
          </>
        )}
      </CardContent>
    </Card>
  );
}

function DownloadRow({ dl }: { dl: Download }) {
  const cancel = useCancelDownload();
  const retry = useRetryDownload();
  const pct = Math.round((dl.progress ?? 0) * 100);
  const state = dl.state.toLowerCase();
  const failed = isFailedState(state);
  const done = !failed && (DONE_STATES.has(state) || pct >= 100);
  const cancelled = state === "cancelled";
  const canCancel = !TERMINAL_STATES.has(state) && pct < 100;
  return (
    <div className="rounded-lg border border-border bg-bg-elevated/50 p-3">
      <div className="flex items-center gap-2">
        {cancelled ? (
          <X className="size-4 shrink-0 text-faint" />
        ) : failed ? (
          <AlertTriangle className="size-4 shrink-0 text-comp-red" />
        ) : done ? (
          <CheckCircle2 className="size-4 shrink-0 text-comp-green" />
        ) : (
          <Loader2 className="size-4 shrink-0 animate-spin text-brand-bright" />
        )}
        <p className="line-clamp-1 flex-1 text-sm font-medium text-fg">
          {dl.title_guess ?? "Download"}
        </p>
        <Badge
          variant={cancelled ? "outline" : failed ? "danger" : done ? "success" : "default"}
          title={failed ? `Download state: ${dl.state}` : undefined}
        >
          {failed ? failedStateLabel(state) : dl.state}
        </Badge>
        {failed && (
          <Button
            variant="ghost"
            size="icon-sm"
            onClick={() => retry.mutate(dl.id)}
            disabled={retry.isPending}
            aria-label="Retry download"
            title="Retry download"
            className="text-faint hover:text-brand-bright"
          >
            <RotateCw className="size-4" />
          </Button>
        )}
        {canCancel && (
          <Button
            variant="ghost"
            size="icon-sm"
            onClick={() => cancel.mutate(dl.id)}
            disabled={cancel.isPending}
            aria-label="Cancel download"
            title="Cancel download"
            className="text-faint hover:text-comp-red"
          >
            <X className="size-4" />
          </Button>
        )}
      </div>
      <div className="mt-2 flex items-center gap-2.5">
        <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-border">
          <div
            className={cn(
              "h-full rounded-full transition-all duration-500",
              failed ? "bg-comp-red" : done ? "bg-comp-green" : "bg-brand",
            )}
            style={{ width: `${Math.max(2, pct)}%` }}
          />
        </div>
        <span className="w-9 text-right text-xs tabular-nums text-muted">{pct}%</span>
      </div>
      {dl.kind === "batch" && dl.total_files != null && (
        <div className="mt-1.5 flex items-center gap-1.5 text-[0.7rem] text-brand-bright">
          <Layers className="size-3" />
          {state === "postprocessed"
            ? `${dl.done_files ?? dl.total_files}/${dl.total_files} episodes imported`
            : state === "completed"
              ? `Importing episodes · ${dl.done_files ?? 0}/${dl.total_files}`
              : `Season pack · ${dl.total_files} episodes`}
        </div>
      )}
      {(dl.resolution || dl.size_bytes) && (
        <div className="mt-1.5 flex items-center gap-2 text-[0.7rem] text-faint">
          {dl.resolution && <span>{dl.resolution}</span>}
          {dl.size_bytes != null && (
            <span className="inline-flex items-center gap-1">
              <HardDrive className="size-3" />
              {formatBytes(dl.size_bytes)}
            </span>
          )}
        </div>
      )}
    </div>
  );
}

function FollowsPanel() {
  const { data, isLoading } = useFollows();
  const addFollow = useAddFollow();
  const deleteFollow = useDeleteFollow();
  const [query, setQuery] = useState("");

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    const q = query.trim();
    if (!q) return;
    addFollow.mutate({ query: q, title: q }, { onSuccess: () => setQuery("") });
  };

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2">
          <Rss className="size-4 text-brand-bright" />
          Follows
        </CardTitle>
      </CardHeader>
      <CardContent className="pt-0">
        <form onSubmit={submit} className="mb-3 flex gap-2">
          <Input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="RSS query, e.g. Example Series 1080p"
            className="h-9 text-sm"
          />
          <Button type="submit" size="icon" variant="primary" loading={addFollow.isPending}>
            <Plus className="size-4" />
          </Button>
        </form>

        {isLoading ? (
          <div className="space-y-2">
            {Array.from({ length: 2 }).map((_, i) => (
              <Skeleton key={i} className="h-10 w-full rounded-lg" />
            ))}
          </div>
        ) : !data || data.length === 0 ? (
          <p className="py-4 text-center text-sm text-muted">No follows yet.</p>
        ) : (
          <ul className="space-y-2">
            {data.map((f) => (
              <FollowRow
                key={f.id}
                follow={f}
                onDelete={() => deleteFollow.mutate(f.id)}
                deleting={deleteFollow.isPending}
              />
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}

function FollowRow({
  follow,
  onDelete,
  deleting,
}: {
  follow: RssFollow;
  onDelete: () => void;
  deleting: boolean;
}) {
  return (
    <li className="flex items-center gap-2 rounded-lg border border-border bg-bg-elevated/50 px-3 py-2">
      <div className="min-w-0 flex-1">
        <p className="truncate text-sm font-medium text-fg">
          {follow.title || follow.query}
        </p>
        <div className="mt-0.5 flex items-center gap-1.5 text-[0.7rem] text-faint">
          <span className="truncate">{follow.query}</span>
          <Badge variant="outline">{follow.resolution}</Badge>
          {follow.trusted_only && (
            <ShieldCheck className="size-3 text-comp-green" aria-label="Trusted only" />
          )}
        </div>
      </div>
      <Button
        variant="ghost"
        size="icon-sm"
        onClick={onDelete}
        disabled={deleting}
        aria-label="Remove follow"
        className="text-faint hover:text-comp-red"
      >
        <Trash2 className="size-4" />
      </Button>
    </li>
  );
}
