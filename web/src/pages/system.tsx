import { useState } from "react";
import {
  Activity,
  Bot,
  ChevronDown,
  Database,
  HardDrive,
  Captions,
  Link2,
  ListChecks,
  MonitorPlay,
  RotateCw,
  Cpu,
  DownloadCloud,
  X,
} from "lucide-react";
import {
  useHealth,
  useJobs,
  useJobStats,
  useRetryJob,
  useRetryErrorJobs,
  useCancelJob,
} from "@/lib/hooks";
import { PageHeader } from "@/components/layout/page-header";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { ErrorState } from "@/components/ui/error-state";
import { cn, formatBytes, formatRelative, formatRelativeUnix } from "@/lib/utils";
import type { HealthReport, JobRow, JobState } from "@/lib/types";

export function SystemPage() {
  return (
    <div className="animate-fade-in">
      <PageHeader
        title="System"
        subtitle="Health of every moving part, plus the background job queue."
      />
      <HealthGrid />
      <JobsPanel />
    </div>
  );
}

// ── Health ──────────────────────────────────────────────────────────────────
function Dot({ ok }: { ok: boolean | undefined }) {
  return (
    <span
      className={cn(
        "inline-block size-2 shrink-0 rounded-full",
        ok === undefined ? "bg-faint" : ok ? "bg-comp-green" : "bg-comp-red",
      )}
    />
  );
}

function DetailRow({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-2 text-xs">
      <span className="text-faint">{label}</span>
      <span className="text-right font-medium tabular-nums text-muted">{value}</span>
    </div>
  );
}

function HealthCard({
  icon: Icon,
  name,
  ok,
  children,
}: {
  icon: React.ComponentType<{ className?: string }>;
  name: string;
  ok: boolean | undefined;
  children?: React.ReactNode;
}) {
  return (
    <div className="rounded-xl border border-border bg-surface/50 p-3.5">
      <div className="mb-2 flex items-center gap-2">
        <Icon className="size-4 text-brand-bright" />
        <span className="text-sm font-semibold text-fg">{name}</span>
        <span className="ml-auto inline-flex items-center gap-1.5 text-[0.7rem] text-muted">
          <Dot ok={ok} />
          {ok === undefined ? "unknown" : ok ? "ok" : "problem"}
        </span>
      </div>
      <div className="space-y-1">{children}</div>
    </div>
  );
}

function HealthGrid() {
  const { data, isLoading, isError, refetch } = useHealth();

  if (isLoading) {
    return (
      <div className="mb-8 grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        {Array.from({ length: 6 }).map((_, i) => (
          <Skeleton key={i} className="h-28 rounded-xl" />
        ))}
      </div>
    );
  }
  if (isError || !data) {
    return (
      <div className="mb-8">
        <ErrorState message="The health endpoint is unreachable." onRetry={() => refetch()} />
      </div>
    );
  }

  const c: HealthReport["components"] = data.components ?? {};
  const usage = c.anthropic?.usage;

  return (
    <div className="mb-8 grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
      <HealthCard icon={Database} name="Database" ok={c.db?.ok}>
        <DetailRow label="Size" value={formatBytes(c.db?.size_bytes)} />
        <DetailRow label="WAL" value={formatBytes(c.db?.wal_bytes)} />
      </HealthCard>

      <HealthCard icon={HardDrive} name="Disk" ok={c.disk?.ok}>
        <DetailRow
          label="Free"
          value={
            c.disk?.free_gb != null
              ? `${c.disk.free_gb.toFixed(0)} / ${c.disk.total_gb?.toFixed(0) ?? "?"} GB`
              : "—"
          }
        />
      </HealthCard>

      <HealthCard icon={ListChecks} name="Jobs" ok={c.jobs?.ok}>
        <DetailRow
          label="Queue"
          value={`${c.jobs?.queued ?? 0} queued · ${c.jobs?.running ?? 0} running`}
        />
        <DetailRow
          label="Stuck"
          value={`${c.jobs?.error ?? 0} error · ${c.jobs?.parked ?? 0} parked`}
        />
        {c.jobs?.oldest_queued && (
          <DetailRow label="Oldest queued" value={formatRelative(c.jobs.oldest_queued)} />
        )}
      </HealthCard>

      <HealthCard icon={Cpu} name="Tokenizer" ok={c.tokenizer?.ok}>
        <DetailRow label="Migaku ext" value={c.tokenizer?.ext_version ?? "—"} />
      </HealthCard>

      <HealthCard icon={DownloadCloud} name="Transmission" ok={c.transmission?.ok}>
        <DetailRow label="Daemon" value={c.transmission?.ok ? "reachable" : "offline"} />
      </HealthCard>

      <HealthCard icon={MonitorPlay} name="Connector" ok={c.connector?.ok}>
        <DetailRow
          label="Status"
          value={
            c.connector?.connected
              ? `connected${c.connector.migaku ? " · Migaku ✓" : ""}`
              : "offline"
          }
        />
        <DetailRow
          label="Version"
          value={`${c.connector?.version ?? "—"}${c.connector?.ext_version ? ` · ext ${c.connector.ext_version}` : ""}`}
        />
        {c.connector?.last_seen != null && (
          <DetailRow label="Last seen" value={formatRelativeUnix(c.connector.last_seen)} />
        )}
      </HealthCard>

      <HealthCard icon={Link2} name="MyAnimeList" ok={c.mal?.ok}>
        <DetailRow
          label="Auth"
          value={c.mal?.authed ? "connected" : c.mal?.configured ? "not authed" : "not configured"}
        />
        {c.mal?.expires_in_days != null && (
          <DetailRow label="Token expires" value={`${c.mal.expires_in_days}d`} />
        )}
        <DetailRow label="Last sync" value={formatRelativeUnix(c.mal?.last_sync)} />
      </HealthCard>

      <HealthCard icon={Captions} name="jimaku" ok={c.jimaku?.ok}>
        <DetailRow label="Token" value={c.jimaku?.configured ? "configured" : "missing"} />
      </HealthCard>

      <HealthCard icon={Bot} name="Anthropic" ok={c.anthropic?.ok}>
        <DetailRow label="API key" value={c.anthropic?.configured ? "configured" : "missing"} />
        {usage &&
          Object.entries(usage).map(([model, u]) => (
            <DetailRow
              key={model}
              label={model.replace(/^claude-/, "")}
              value={`${u.calls} calls · ${((u.in + u.out) / 1000).toFixed(1)}k tok`}
            />
          ))}
      </HealthCard>
    </div>
  );
}

// ── Jobs ────────────────────────────────────────────────────────────────────
const JOB_STATES: JobState[] = ["queued", "running", "done", "error", "parked", "cancelled"];

const STATE_BADGE: Record<string, "default" | "solid" | "success" | "warning" | "danger" | "outline"> = {
  queued: "default",
  running: "solid",
  done: "success",
  error: "danger",
  parked: "warning",
  cancelled: "outline",
};

function JobsPanel() {
  const [state, setState] = useState("");
  const [type, setType] = useState("");
  const stats = useJobStats();
  const { data: jobs, isLoading } = useJobs(state, type, 100);
  const retryErrors = useRetryErrorJobs();

  const errorCount = stats.data?.states?.error ?? 0;

  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex flex-wrap items-center gap-3">
          <CardTitle className="flex items-center gap-2">
            <Activity className="size-4 text-brand-bright" />
            Job queue
          </CardTitle>
          {errorCount > 0 && (
            <Button
              variant="secondary"
              size="sm"
              className="ml-auto"
              onClick={() => retryErrors.mutate(type || undefined)}
              loading={retryErrors.isPending}
              title={type ? `Re-queue every errored "${type}" job` : "Re-queue every errored job"}
            >
              <RotateCw className="size-3.5" />
              Retry all errors ({errorCount})
            </Button>
          )}
        </div>

        {/* state chips + type filter */}
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <button
            onClick={() => setState("")}
            className={cn(
              "rounded-full border px-3 py-1 text-xs font-medium transition-colors",
              state === ""
                ? "border-brand bg-brand/20 text-brand-bright"
                : "border-border-strong bg-surface text-muted hover:text-fg",
            )}
          >
            All
          </button>
          {JOB_STATES.map((s) => {
            const n = stats.data?.states?.[s] ?? 0;
            return (
              <button
                key={s}
                onClick={() => setState(state === s ? "" : s)}
                className={cn(
                  "inline-flex items-center gap-1.5 rounded-full border px-3 py-1 text-xs font-medium transition-colors",
                  state === s
                    ? "border-brand bg-brand/20 text-brand-bright"
                    : "border-border-strong bg-surface text-muted hover:text-fg",
                  n === 0 && state !== s && "opacity-50",
                )}
              >
                {s}
                <span className="tabular-nums opacity-70">{n}</span>
              </button>
            );
          })}

          {stats.data?.registered && stats.data.registered.length > 0 && (
            <div className="relative ml-auto">
              <select
                value={type}
                onChange={(e) => setType(e.target.value)}
                aria-label="Filter by job type"
                className="h-8 appearance-none rounded-lg border border-border-strong bg-surface pl-3 pr-8 text-xs font-medium text-fg transition-colors hover:border-faint focus-visible:border-brand focus-visible:outline-none"
              >
                <option value="">All types</option>
                {stats.data.registered.map((t) => (
                  <option key={t} value={t}>
                    {t}
                  </option>
                ))}
              </select>
              <ChevronDown className="pointer-events-none absolute right-2.5 top-1/2 size-3.5 -translate-y-1/2 text-faint" />
            </div>
          )}
        </div>
      </CardHeader>

      <CardContent className="pt-0">
        {isLoading ? (
          <div className="space-y-2">
            {Array.from({ length: 4 }).map((_, i) => (
              <Skeleton key={i} className="h-10 w-full rounded-lg" />
            ))}
          </div>
        ) : !jobs || jobs.length === 0 ? (
          <p className="py-8 text-center text-sm text-muted">
            No jobs{state ? ` in "${state}"` : ""}
            {type ? ` of type "${type}"` : ""}.
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full border-collapse text-sm">
              <thead>
                <tr className="border-b border-border text-left text-[0.7rem] uppercase tracking-wide text-faint">
                  <th className="w-16 py-2 pr-2 font-medium">ID</th>
                  <th className="py-2 pr-3 font-medium">Type</th>
                  <th className="w-24 px-2 py-2 font-medium">State</th>
                  <th className="w-14 px-2 py-2 text-right font-medium">Att.</th>
                  <th className="hidden py-2 pl-3 font-medium lg:table-cell">Last error</th>
                  <th className="w-24 px-2 py-2 text-right font-medium">Updated</th>
                  <th className="w-20 py-2 pl-2" />
                </tr>
              </thead>
              <tbody>
                {jobs.map((j) => (
                  <JobRowView key={j.id} job={j} />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function JobRowView({ job }: { job: JobRow }) {
  const retry = useRetryJob();
  const cancel = useCancelJob();
  const canRetry = job.state === "error" || job.state === "parked" || job.state === "cancelled";
  const canCancel = job.state === "queued" || job.state === "running";
  return (
    <tr className="border-b border-border/40 align-top transition-colors last:border-0 hover:bg-surface-hover/50">
      <td className="py-2 pr-2 text-xs tabular-nums text-faint">#{job.id}</td>
      <td className="py-2 pr-3">
        <p className="font-medium text-fg">{job.type}</p>
        {job.last_error && (
          <p className="mt-0.5 line-clamp-1 max-w-64 text-[0.7rem] text-comp-red/80 lg:hidden">
            {job.last_error}
          </p>
        )}
      </td>
      <td className="px-2 py-2">
        <Badge variant={STATE_BADGE[job.state] ?? "default"}>{job.state}</Badge>
      </td>
      <td className="px-2 py-2 text-right text-xs tabular-nums text-muted">{job.attempts}</td>
      <td className="hidden max-w-80 py-2 pl-3 lg:table-cell">
        {job.last_error ? (
          <p className="line-clamp-2 text-[0.7rem] leading-snug text-comp-red/80" title={job.last_error}>
            {job.last_error}
          </p>
        ) : (
          <span className="text-xs text-faint">—</span>
        )}
      </td>
      <td className="px-2 py-2 text-right text-xs text-faint">{formatRelative(job.updated_at)}</td>
      <td className="py-1.5 pl-2">
        <div className="flex justify-end gap-1">
          {canRetry && (
            <Button
              variant="ghost"
              size="icon-sm"
              onClick={() => retry.mutate(job.id)}
              disabled={retry.isPending}
              aria-label="Retry job"
              title="Retry job"
              className="text-faint hover:text-brand-bright"
            >
              <RotateCw className="size-3.5" />
            </Button>
          )}
          {canCancel && (
            <Button
              variant="ghost"
              size="icon-sm"
              onClick={() => cancel.mutate(job.id)}
              disabled={cancel.isPending}
              aria-label="Cancel job"
              title="Cancel job"
              className="text-faint hover:text-comp-red"
            >
              <X className="size-3.5" />
            </Button>
          )}
        </div>
      </td>
    </tr>
  );
}
