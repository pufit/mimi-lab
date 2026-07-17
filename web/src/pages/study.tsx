import { useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { GraduationCap, RefreshCw, Package, Unlock, ChevronRight } from "lucide-react";
import { useLeverage, useRefreshLeverage } from "@/lib/hooks";
import { api, exportApkg } from "@/lib/api";
import { PageHeader } from "@/components/layout/page-header";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { EmptyState } from "@/components/ui/empty-state";
import { ErrorState } from "@/components/ui/error-state";
import { Skeleton } from "@/components/ui/skeleton";
import { toast } from "@/components/ui/toast";
import { cn, formatRelative, formatRelativeUnix } from "@/lib/utils";
import type { LeverageWord } from "@/lib/types";

const computedLabel = (v?: string | number | null) =>
  v == null ? "—" : typeof v === "number" ? formatRelativeUnix(v) : formatRelative(v);

export function StudyPage() {
  const { data, isLoading, isError, refetch } = useLeverage();
  const refresh = useRefreshLeverage();
  const navigate = useNavigate();

  const words = useMemo(() => data?.words ?? [], [data]);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [exporting, setExporting] = useState(false);

  const toggle = (lemma: string) =>
    setSelected((s) => {
      const next = new Set(s);
      next.has(lemma) ? next.delete(lemma) : next.add(lemma);
      return next;
    });
  const allSelected = words.length > 0 && selected.size === words.length;
  const toggleAll = () =>
    setSelected(allSelected ? new Set() : new Set(words.map((w) => w.lemma)));

  // For each selected word grab its best (i+1) example line, then build a deck.
  const doExport = async () => {
    const picked = words.filter((w) => selected.has(w.lemma));
    if (picked.length === 0 || exporting) return;
    setExporting(true);
    const tid = toast.loading("Collecting example sentences…", `0 / ${picked.length}`);
    try {
      const lineIds: number[] = [];
      let missed = 0;
      for (let i = 0; i < picked.length; i++) {
        try {
          const ms = await api.moments({ q: picked[i].lemma, limit: 1, sort: "iplus1" });
          if (ms[0]) lineIds.push(ms[0].line_id);
          else missed++;
        } catch {
          missed++;
        }
        toast.update(tid, { description: `${i + 1} / ${picked.length}` });
      }
      if (lineIds.length === 0) {
        toast.update(tid, {
          title: "No example lines found",
          description: "None of the selected words appear in your ingested subtitles.",
          variant: "error",
        });
        return;
      }
      toast.update(tid, {
        title: "Building Anki deck…",
        description: `${lineIds.length} card(s) with media`,
      });
      await exportApkg(lineIds, "Mimi Lab Study Queue");
      toast.update(tid, {
        title: "Deck downloaded",
        description:
          missed > 0
            ? `${lineIds.length} cards · ${missed} word(s) had no example line`
            : `${lineIds.length} cards — import the .apkg into Anki`,
        variant: "success",
      });
    } catch (e) {
      toast.update(tid, {
        title: "Export failed",
        description: e instanceof Error ? e.message : undefined,
        variant: "error",
      });
    } finally {
      setExporting(false);
    }
  };

  const band = data?.band ?? [60, 80];

  return (
    <div className="animate-fade-in">
      <PageHeader
        title="Study queue"
        subtitle="These words unlock the most content at your level — learning the top ones moves whole episodes into your 80%+ sweet spot."
      />

      {/* meta line */}
      {data && (
        <div className="mb-5 flex flex-wrap items-center gap-x-4 gap-y-2 text-sm text-muted">
          <span>
            <span className="font-semibold text-fg">{data.episodes_in_band}</span> episodes in
            the {band[0]}–{band[1]}% band
          </span>
          <span className="text-faint">· computed {computedLabel(data.computed_at)}</span>
          <Button
            variant="ghost"
            size="sm"
            onClick={() => refresh.mutate()}
            loading={refresh.isPending}
            title="Recompute the leverage analysis from scratch"
          >
            <RefreshCw className="size-3.5" />
            Refresh
          </Button>
        </div>
      )}

      {isLoading && <StudySkeleton />}
      {isError && <ErrorState onRetry={() => refetch()} />}

      {data && words.length === 0 && (
        <EmptyState
          icon={GraduationCap}
          title="Analyze your library first"
          description="The study queue needs scored episodes in the 60–80% comprehension band. Run Library → Analyze comprehension, then come back."
        />
      )}

      {words.length > 0 && (
        <>
          <div className="overflow-hidden rounded-xl border border-border bg-surface/40">
            <table className="w-full border-collapse text-sm">
              <thead>
                <tr className="border-b border-border text-left text-[0.7rem] uppercase tracking-wide text-faint">
                  <th className="w-10 py-2.5 pl-4 pr-1">
                    <input
                      type="checkbox"
                      checked={allSelected}
                      onChange={toggleAll}
                      aria-label="Select all words"
                      className="size-4 accent-[var(--color-brand)]"
                    />
                  </th>
                  <th className="w-10 py-2.5 pr-2 text-right font-medium">#</th>
                  <th className="py-2.5 pr-3 font-medium">Word</th>
                  <th className="hidden py-2.5 pr-3 font-medium md:table-cell">Gloss</th>
                  <th className="w-20 px-2 py-2.5 text-right font-medium" title="Occurrences in your library">
                    Occurs
                  </th>
                  <th className="hidden w-16 px-2 py-2.5 text-right font-medium sm:table-cell" title="Episodes it appears in">
                    Eps
                  </th>
                  <th className="w-36 py-2.5 pl-2 pr-4 text-right font-medium" title="Episodes pushed over the target if you learn everything up to here">
                    Unlocks
                  </th>
                </tr>
              </thead>
              <tbody>
                {words.map((w, i) => (
                  <WordRow
                    key={w.lemma}
                    word={w}
                    rank={i + 1}
                    checked={selected.has(w.lemma)}
                    onToggle={() => toggle(w.lemma)}
                    onOpen={() => navigate(`/moments?q=${encodeURIComponent(w.lemma)}`)}
                  />
                ))}
              </tbody>
            </table>
          </div>

          {/* sticky export bar */}
          {selected.size > 0 && (
            <div className="sticky bottom-4 z-30 mt-4 flex items-center justify-between gap-3 rounded-xl border border-brand/40 bg-bg-elevated/95 px-4 py-3 shadow-2xl shadow-black/50 backdrop-blur-xl">
              <p className="text-sm text-muted">
                <span className="font-semibold text-fg">{selected.size}</span> word
                {selected.size === 1 ? "" : "s"} selected
              </p>
              <div className="flex items-center gap-2">
                <Button variant="ghost" size="sm" onClick={() => setSelected(new Set())}>
                  Clear
                </Button>
                <Button variant="primary" size="sm" onClick={doExport} loading={exporting}>
                  <Package className="size-3.5" />
                  Export {selected.size} to Anki (.apkg)
                </Button>
              </div>
            </div>
          )}
        </>
      )}
    </div>
  );
}

function WordRow({
  word,
  rank,
  checked,
  onToggle,
  onOpen,
}: {
  word: LeverageWord;
  rank: number;
  checked: boolean;
  onToggle: () => void;
  onOpen: () => void;
}) {
  const showReading = word.reading && word.reading !== word.lemma;
  return (
    <tr
      onClick={onOpen}
      className={cn(
        "group cursor-pointer border-b border-border/40 transition-colors last:border-0 hover:bg-surface-hover/70",
        checked && "bg-brand/5",
      )}
      title={`See every "${word.lemma}" moment in your library`}
    >
      <td className="py-2.5 pl-4 pr-1" onClick={(e) => e.stopPropagation()}>
        <input
          type="checkbox"
          checked={checked}
          onChange={onToggle}
          aria-label={`Select ${word.lemma}`}
          className="size-4 accent-[var(--color-brand)]"
        />
      </td>
      <td className="py-2.5 pr-2 text-right text-xs tabular-nums text-faint">{rank}</td>
      <td className="py-2.5 pr-3">
        <div className="flex items-center gap-2">
          <span className="font-jp text-base font-medium leading-tight text-fg">
            {showReading ? (
              <ruby>
                {word.lemma}
                <rt>{word.reading}</rt>
              </ruby>
            ) : (
              word.lemma
            )}
          </span>
          {word.freq_rank != null && (
            <Badge variant="outline" className="tabular-nums" title="Frequency rank">
              #{word.freq_rank.toLocaleString()}
            </Badge>
          )}
          <ChevronRight className="size-3.5 text-faint opacity-0 transition-opacity group-hover:opacity-100" />
        </div>
        {word.gloss && (
          <p className="mt-0.5 line-clamp-1 text-xs text-muted md:hidden">{word.gloss}</p>
        )}
      </td>
      <td className="hidden max-w-64 py-2.5 pr-3 md:table-cell">
        <p className="line-clamp-1 text-xs text-muted">{word.gloss ?? "—"}</p>
      </td>
      <td className="px-2 py-2.5 text-right text-xs tabular-nums text-muted">
        {word.occurrences.toLocaleString()}
      </td>
      <td className="hidden px-2 py-2.5 text-right text-xs tabular-nums text-muted sm:table-cell">
        {word.episodes}
      </td>
      <td className="py-2.5 pl-2 pr-4 text-right">
        <span className="inline-flex items-center gap-1.5 text-xs">
          <Unlock className="size-3 text-comp-green" />
          <span className="font-semibold tabular-nums text-comp-green">+{word.crossings}</span>
          <span className="tabular-nums text-faint" title="Cumulative episodes unlocked through this rank">
            Σ {word.cumulative_unlocked}
          </span>
        </span>
      </td>
    </tr>
  );
}

function StudySkeleton() {
  return (
    <div className="overflow-hidden rounded-xl border border-border p-3">
      <div className="space-y-2">
        {Array.from({ length: 10 }).map((_, i) => (
          <Skeleton key={i} className="h-9 w-full" />
        ))}
      </div>
    </div>
  );
}
