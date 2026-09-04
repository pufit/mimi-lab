import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import {
  ArrowDownToLine,
  ArrowUpDown,
  Check,
  CircleSlash,
  Clock,
  Layers,
  Pause,
  Search,
  Sparkles,
  Zap,
} from "lucide-react";
import { PageHeader } from "@/components/layout/page-header";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { EmptyState } from "@/components/ui/empty-state";
import { ErrorState } from "@/components/ui/error-state";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { toast } from "@/components/ui/toast";
import { StudyTabs } from "@/components/srs/study-tabs";
import { StackRow } from "@/components/srs/stack-row";
import type { StackTab } from "@/components/srs/stack-row";
import { CandidatesTable } from "@/components/srs/candidates-table";
import { SwapMomentDialog } from "@/components/srs/swap-moment-dialog";
import { EditCardDialog } from "@/components/srs/edit-card-dialog";
import { srsApi } from "@/lib/srs-api";
import {
  errMsg,
  useBulkAction,
  useCandidates,
  useGenerateCards,
  useMoveCard,
  useResortStack,
  useSrsCards,
  useSrsSummary,
} from "@/lib/srs-hooks";
import { qkSrs } from "@/lib/srs-keys";
import type { SrsAction, SrsBulkResult, SrsCard, SrsResortBy } from "@/lib/srs-types";

type Tab = StackTab | "upnext";

const TAB_STATE: Record<StackTab, string> = {
  stack: "new",
  studying: "studying",
  known: "known",
  parked: "parked",
};

const DEFAULT_SORT: Record<StackTab, string> = {
  stack: "queue",
  studying: "due",
  known: "created",
  parked: "created",
};

const SORTS = [
  { value: "queue", label: "Stack order" },
  { value: "due", label: "Due date" },
  { value: "created", label: "Newest first" },
  { value: "lemma", label: "Alphabetical" },
  { value: "score", label: "Score" },
  { value: "rank", label: "Frequency rank" },
];

const RESORTS: { value: SrsResortBy; label: string; hint: string }[] = [
  { value: "score", label: "Best first", hint: "The pipeline's own ranking" },
  { value: "frequency", label: "Most common words", hint: "By JPDB frequency rank" },
  { value: "leverage", label: "Unlocks most episodes", hint: "Comprehension leverage" },
  { value: "show", label: "Grouped by show", hint: "One series at a time" },
  { value: "random", label: "Shuffle", hint: "Mix it up" },
];

const BULK: { action: SrsAction; label: string; icon: typeof Zap }[] = [
  { action: "study_next", label: "Study next", icon: Zap },
  { action: "bottom", label: "Bottom", icon: ArrowDownToLine },
  { action: "bury", label: "Skip today", icon: Clock },
  { action: "suspend", label: "Park", icon: Pause },
  { action: "known", label: "Already know", icon: Check },
  { action: "reject", label: "Not worth it", icon: CircleSlash },
];

const PAGE = 50;

export function StackPage() {
  const [params, setParams] = useSearchParams();
  const tabParam = (params.get("tab") ?? "stack") as Tab;
  const tab: Tab = ["stack", "studying", "known", "parked", "upnext"].includes(tabParam)
    ? tabParam
    : "stack";

  const [q, setQ] = useState("");
  const [query, setQuery] = useState("");
  const [sort, setSort] = useState<string>(DEFAULT_SORT.stack);
  const [page, setPage] = useState(0);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [swapCard, setSwapCard] = useState<SrsCard | null>(null);
  const [editCard, setEditCard] = useState<SrsCard | null>(null);
  const [resortBy, setResortBy] = useState<SrsResortBy | null>(null);
  const [keepTop, setKeepTop] = useState(10);
  const lastPicked = useRef<number | null>(null);

  const qc = useQueryClient();
  const summary = useSrsSummary();
  const move = useMoveCard();
  const bulk = useBulkAction();
  const resort = useResortStack();
  const generate = useGenerateCards();

  useEffect(() => {
    const t = setTimeout(() => setQuery(q.trim()), 300);
    return () => clearTimeout(t);
  }, [q]);

  useEffect(() => {
    setPage(0);
    setSelected(new Set());
  }, [tab, query, sort]);

  const setTab = (next: string) => {
    const p = new URLSearchParams(params);
    if (next === "stack") p.delete("tab");
    else p.set("tab", next);
    setParams(p, { replace: true });
    if (next !== "upnext") setSort(DEFAULT_SORT[next as StackTab]);
  };

  const isCardsTab = tab !== "upnext";
  const cardsTab = (isCardsTab ? tab : "stack") as StackTab;
  const wholeStack = cardsTab === "stack";

  const list = useSrsCards({
    state: isCardsTab ? TAB_STATE[cardsTab] : "new",
    q: query,
    sort,
    order: "asc",
    limit: wholeStack ? 0 : PAGE,
    offset: wholeStack ? 0 : page * PAGE,
  });

  const items = useMemo(() => (isCardsTab ? (list.data?.items ?? []) : []), [isCardsTab, list.data]);
  const total = list.data?.total ?? 0;

  // Drag-and-drop only makes sense on the untouched, whole stack.
  const dnd = wholeStack && sort === "queue" && query === "";
  const [dragIndex, setDragIndex] = useState<number | null>(null);
  const [over, setOver] = useState<{ index: number; where: "above" | "below" } | null>(null);

  const doMove = useCallback(
    (from: number, to: number) => {
      const card = items[from];
      if (!card || from === to) return;
      move.mutate({ card_id: card.id, position: Math.max(0, Math.min(items.length - 1, to)) });
    },
    [items, move],
  );

  const onDrop = useCallback(
    (index: number) => {
      const from = dragIndex;
      setDragIndex(null);
      const target = over?.index === index ? over : { index, where: "above" as const };
      setOver(null);
      if (from == null) return;
      const insert = target.where === "above" ? target.index : target.index + 1;
      doMove(from, from < insert ? insert - 1 : insert);
    },
    [dragIndex, over, doMove],
  );

  const toggle = useCallback(
    (id: number, shiftKey: boolean) => {
      setSelected((prev) => {
        const next = new Set(prev);
        if (shiftKey && lastPicked.current != null) {
          const a = items.findIndex((c) => c.id === lastPicked.current);
          const b = items.findIndex((c) => c.id === id);
          if (a >= 0 && b >= 0) {
            for (let i = Math.min(a, b); i <= Math.max(a, b); i++) next.add(items[i].id);
            lastPicked.current = id;
            return next;
          }
        }
        if (next.has(id)) next.delete(id);
        else next.add(id);
        lastPicked.current = id;
        return next;
      });
    },
    [items],
  );

  /** The exact inverse of a bulk action, from `SrsBulkResult.previous` (§8.6). */
  const undoBulk = useCallback(
    async (action: SrsAction, res: SrsBulkResult) => {
      try {
        if (action === "study_next" || action === "bottom") {
          const rows = res.previous
            .filter((p) => p.queue_pos != null)
            .sort((a, b) => (a.queue_pos ?? 0) - (b.queue_pos ?? 0));
          // `queue_pos` is the dense 1-based stack position; `/srs/stack/move`
          // takes an absolute 0-based index into the list the card has already
          // been removed from — hence the -1.
          for (const p of rows) await srsApi.move(p.card_id, Math.max(0, p.queue_pos! - 1));
        } else {
          const inverse: SrsAction =
            action === "bury" ? "unbury" : action === "reject" ? "restore" : "resume";
          await srsApi.bulk(
            res.previous.map((p) => p.card_id),
            inverse,
          );
        }
        toast.success("Put back");
      } catch (e) {
        toast.error("Couldn't undo", errMsg(e));
      } finally {
        qc.invalidateQueries({ queryKey: qkSrs.all, predicate: (query2) => query2.queryKey[1] !== "queue" });
      }
    },
    [qc],
  );

  const runBulk = (action: SrsAction) => {
    const ids = [...selected];
    if (ids.length === 0) return;
    bulk.mutate(
      { ids, action },
      {
        onSuccess: (res) => {
          setSelected(new Set());
          toast({
            title: `${res.updated} card${res.updated === 1 ? "" : "s"} updated`,
            description: BULK.find((b) => b.action === action)?.label,
            duration: 6000,
            action: { label: "Undo", onClick: () => void undoBulk(action, res) },
          });
        },
      },
    );
  };

  const states = summary.data?.states;
  // Up next was the only tab without a badge; one cheap count query fixes it.
  const candidateTotal = useCandidates({ limit: 1 }).data?.total ?? null;
  const counts: Record<Tab, number | null> = {
    stack: states ? states.new : null,
    studying: states ? states.learning + states.review + states.relearning : null,
    known: states ? states.known : null,
    parked: states ? states.suspended + states.rejected : null,
    upnext: candidateTotal,
  };

  // The dashed rule sits under the last "study next" row.
  const separatorAt = useMemo(() => {
    if (!wholeStack) return -1;
    let last = -1;
    for (let i = 0; i < items.length; i++) if (items[i].study_now) last = i;
    return last >= 0 && last < items.length - 1 ? last : -1;
  }, [items, wholeStack]);

  const allSelected = items.length > 0 && selected.size === items.length;

  return (
    <div className="animate-fade-in pb-24">
      <PageHeader
        title="Stack"
        subtitle="Everything you are about to learn, are learning, and have parked."
        actions={
          <>
            <DropdownMenu.Root>
              <DropdownMenu.Trigger asChild>
                <Button variant="secondary" disabled={!wholeStack}>
                  <ArrowUpDown className="size-4" />
                  Re-sort
                </Button>
              </DropdownMenu.Trigger>
              <DropdownMenu.Portal>
                <DropdownMenu.Content
                  align="end"
                  sideOffset={6}
                  className="z-50 w-64 rounded-xl border border-border bg-bg-elevated/95 p-1.5 shadow-2xl shadow-black/60 backdrop-blur-xl"
                >
                  {RESORTS.map((r) => (
                    <DropdownMenu.Item
                      key={r.value}
                      onSelect={() => setResortBy(r.value)}
                      className="cursor-pointer select-none rounded-lg px-2.5 py-1.5 text-sm text-muted outline-none transition-colors data-[highlighted]:bg-surface-hover data-[highlighted]:text-fg"
                    >
                      <p>{r.label}</p>
                      <p className="text-[0.7rem] text-faint">{r.hint}</p>
                    </DropdownMenu.Item>
                  ))}
                </DropdownMenu.Content>
              </DropdownMenu.Portal>
            </DropdownMenu.Root>
            <Button
              variant="primary"
              onClick={() => generate.mutate({})}
              loading={generate.isPending || !!summary.data?.generation.running}
              disabled={!summary.data?.generation.llm_available}
            >
              <Sparkles className="size-4" />
              Generate more
            </Button>
          </>
        }
      />

      <StudyTabs />

      <Tabs value={tab} onValueChange={setTab}>
        <div className="flex flex-wrap items-center gap-3">
          <TabsList>
            {(
              [
                ["stack", "Stack"],
                ["studying", "Studying"],
                ["known", "Known"],
                ["parked", "Parked"],
                ["upnext", "Up next"],
              ] as const
            ).map(([value, label]) => (
              <TabsTrigger key={value} value={value}>
                {label}
                {counts[value] != null && (
                  <span className="tabular-nums text-faint">
                    {counts[value]!.toLocaleString()}
                  </span>
                )}
              </TabsTrigger>
            ))}
          </TabsList>

          {isCardsTab && (
            <>
              <div className="relative min-w-0 flex-1 sm:max-w-sm">
                <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-faint" />
                <Input
                  value={q}
                  onChange={(e) => setQ(e.target.value)}
                  placeholder="Search word, meaning or sentence…"
                  className="h-9 pl-9"
                />
              </div>
              <select
                value={sort}
                onChange={(e) => setSort(e.target.value)}
                aria-label="Sort cards"
                className="h-9 rounded-lg border border-border-strong bg-surface px-3 text-xs font-medium text-fg outline-none focus:border-brand"
              >
                {SORTS.map((s) => (
                  <option key={s.value} value={s.value}>
                    {s.label}
                  </option>
                ))}
              </select>
            </>
          )}
        </div>

        {(["stack", "studying", "known", "parked"] as StackTab[]).map((t) => (
          <TabsContent key={t} value={t}>
            {list.isLoading && (
              <div className="space-y-2">
                {Array.from({ length: 8 }).map((_, i) => (
                  <Skeleton key={i} className="h-14 w-full rounded-lg" />
                ))}
              </div>
            )}

            {list.isError && <ErrorState message={errMsg(list.error)} onRetry={() => list.refetch()} />}

            {list.data && items.length === 0 && (
              <EmptyState
                icon={Layers}
                title={query ? "Nothing matches that" : "Nothing here yet"}
                description={
                  query
                    ? "Try the dictionary form of the word, or a word from the English meaning."
                    : t === "stack"
                      ? "Generate cards or import the curated deck to fill the stack."
                      : t === "studying"
                        ? "Cards land here the moment you start reviewing them."
                        : t === "known"
                          ? "Words you graduate — or mark as known — collect here."
                          : "Cards you park or turn down end up here."
                }
              />
            )}

            {items.length > 0 && (
              <>
                <div className="mb-2 flex items-center gap-3 px-3 text-xs text-faint">
                  <input
                    type="checkbox"
                    checked={allSelected}
                    onChange={() =>
                      setSelected(allSelected ? new Set() : new Set(items.map((c) => c.id)))
                    }
                    aria-label="Select every card in this list"
                    className="size-4 accent-[var(--color-brand)]"
                  />
                  <span>
                    {items.length.toLocaleString()} shown
                    {total > items.length ? ` of ${total.toLocaleString()}` : ""}
                    {dnd && " · drag a row, or Alt+↑/↓ on a focused row, to reorder"}
                  </span>
                </div>

                <div className="overflow-hidden rounded-xl border border-border bg-surface/40">
                  {items.map((card, i) => (
                    <StackRow
                      key={card.id}
                      card={card}
                      tab={t}
                      index={i}
                      selected={selected.has(card.id)}
                      onSelect={toggle}
                      dnd={dnd}
                      dragging={dragIndex === i}
                      insert={over?.index === i ? over.where : null}
                      onDragStart={setDragIndex}
                      onDragOver={(index, where) =>
                        setOver((prev) =>
                          prev?.index === index && prev.where === where ? prev : { index, where },
                        )
                      }
                      onDrop={onDrop}
                      onDragEnd={() => {
                        setDragIndex(null);
                        setOver(null);
                      }}
                      onNudge={dnd ? (index, delta) => doMove(index, index + delta) : undefined}
                      onSwapMoment={setSwapCard}
                      onEdit={setEditCard}
                      separator={i === separatorAt}
                    />
                  ))}
                </div>

                {!wholeStack && total > PAGE && (
                  <div className="mt-4 flex items-center justify-center gap-3">
                    <Button
                      variant="secondary"
                      size="sm"
                      disabled={page === 0}
                      onClick={() => setPage((p) => Math.max(0, p - 1))}
                    >
                      Previous
                    </Button>
                    <span className="text-xs tabular-nums text-faint">
                      {page * PAGE + 1}–{Math.min(total, (page + 1) * PAGE)} of{" "}
                      {total.toLocaleString()}
                    </span>
                    <Button
                      variant="secondary"
                      size="sm"
                      disabled={(page + 1) * PAGE >= total}
                      onClick={() => setPage((p) => p + 1)}
                    >
                      Next
                    </Button>
                  </div>
                )}
              </>
            )}
          </TabsContent>
        ))}

        <TabsContent value="upnext">
          <CandidatesTable />
        </TabsContent>
      </Tabs>

      {/* selection bar */}
      {selected.size > 0 && isCardsTab && (
        <div className="fixed inset-x-0 bottom-0 z-30 px-4 pb-4 md:pl-64">
          <div className="mx-auto flex max-w-[1500px] flex-wrap items-center gap-2 rounded-xl border border-brand/40 bg-bg-elevated/95 px-4 py-3 shadow-2xl shadow-black/50 backdrop-blur-xl">
            <p className="text-sm text-muted">
              <span className="font-semibold text-fg">{selected.size}</span> selected
            </p>
            <div className="ml-auto flex flex-wrap items-center gap-1.5">
              {BULK.map((b) => (
                <Button
                  key={b.action}
                  variant="secondary"
                  size="sm"
                  disabled={bulk.isPending}
                  onClick={() => runBulk(b.action)}
                >
                  <b.icon className="size-3.5" />
                  {b.label}
                </Button>
              ))}
              <Button variant="ghost" size="sm" onClick={() => setSelected(new Set())}>
                Clear
              </Button>
            </div>
          </div>
        </div>
      )}

      {/* re-sort confirmation */}
      <Dialog open={resortBy != null} onOpenChange={(o) => !o && setResortBy(null)}>
        <DialogContent className="w-[min(30rem,calc(100vw-2rem))]">
          <DialogHeader>
            <DialogTitle>
              Re-sort the stack — {RESORTS.find((r) => r.value === resortBy)?.label}
            </DialogTitle>
            <DialogDescription>
              This renumbers every card waiting in the stack. Cards you are already studying are
              untouched.
            </DialogDescription>
          </DialogHeader>
          <label className="flex items-center gap-3 text-sm text-muted">
            Keep the top
            <Input
              type="number"
              min={0}
              max={200}
              value={keepTop}
              onChange={(e) => setKeepTop(Math.max(0, Math.min(200, Number(e.target.value) || 0)))}
              className="h-9 w-24 tabular-nums"
            />
            cards where they are
          </label>
          <DialogFooter>
            <Button variant="ghost" onClick={() => setResortBy(null)}>
              Cancel
            </Button>
            <Button
              variant="primary"
              loading={resort.isPending}
              onClick={() => {
                if (!resortBy) return;
                resort.mutate({ by: resortBy, keep_top: keepTop });
                setResortBy(null);
              }}
            >
              Re-sort
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {swapCard && (
        <SwapMomentDialog
          card={swapCard}
          open={!!swapCard}
          onOpenChange={(o) => !o && setSwapCard(null)}
        />
      )}
      {editCard && (
        <EditCardDialog
          card={editCard}
          open={!!editCard}
          onOpenChange={(o) => !o && setEditCard(null)}
        />
      )}
    </div>
  );
}
