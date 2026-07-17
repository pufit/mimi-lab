import { useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import {
  MonitorPlay,
  BookMarked,
  RefreshCw,
  Link2,
  FolderTree,
  KeyRound,
  CheckCircle2,
  XCircle,
  RotateCw,
  TrendingUp,
  ExternalLink,
  Copy,
  Eye,
  EyeOff,
  Languages,
  Sparkles,
  Stethoscope,
  FlaskConical,
} from "lucide-react";
import {
  useConnectorStatus,
  useConnectorSetup,
  useConnectorSelfcheck,
  useEnglishConfig,
  useSetEnglishSource,
  useKnownSummary,
  useKnownGrowth,
  useKnownSync,
  useMalStatus,
  useMalSync,
  useMigakuDriftCheck,
} from "@/lib/hooks";
import { MAL_AUTH_URL } from "@/lib/api";
import { PageHeader } from "@/components/layout/page-header";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { toast } from "@/components/ui/toast";
import { cn, formatDateUnix, formatRelative, formatRelativeUnix } from "@/lib/utils";
import type { MigakuDriftReport, SelfcheckResult } from "@/lib/types";

/** Copy text to the clipboard, with a fallback for non-secure (plain-http) origins
 *  where navigator.clipboard is unavailable (e.g. plain HTTP on a LAN host). */
async function copyText(text: string): Promise<boolean> {
  try {
    if (window.isSecureContext && navigator.clipboard) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    /* fall through to the legacy path */
  }
  try {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.top = "0";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    ta.setSelectionRange(0, text.length);
    const ok = document.execCommand("copy");
    document.body.removeChild(ta);
    return ok;
  } catch {
    return false;
  }
}

export function SettingsPage() {
  // MAL OAuth redirects back here with ?mal=connected — toast once, strip the param.
  const [searchParams, setSearchParams] = useSearchParams();
  const handledMalRedirect = useRef(false);
  useEffect(() => {
    if (handledMalRedirect.current) return;
    if (searchParams.get("mal") === "connected") {
      handledMalRedirect.current = true;
      toast.success("MyAnimeList connected");
      const next = new URLSearchParams(searchParams);
      next.delete("mal");
      setSearchParams(next, { replace: true });
    }
  }, [searchParams, setSearchParams]);

  return (
    <div className="animate-fade-in">
      <PageHeader title="Settings" subtitle="Connections, known-words, and library configuration." />
      <div className="grid gap-6 lg:grid-cols-2">
        <ConnectorCard />
        <KnownWordsCard />
        <IntegrationHealthCard />
        <EnglishSubsCard />
        <MalCard />
        <PathsCard />
      </div>
    </div>
  );
}

function StatusRow({ ok, label }: { ok: boolean; label: string }) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 text-sm font-medium",
        ok ? "text-comp-green" : "text-faint",
      )}
    >
      {ok ? <CheckCircle2 className="size-4" /> : <XCircle className="size-4" />}
      {label}
    </span>
  );
}

function ConnectorCard() {
  const { data: status, isLoading, refetch, isFetching } = useConnectorStatus();
  const { data: setup } = useConnectorSetup();
  const connected = !!status?.connected;
  const migaku = !!status?.migaku;
  const devices = status?.devices ?? [];
  const installCmd = setup?.install_cmd ?? "";

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <MonitorPlay className="size-4 text-brand-bright" />
          Connector
        </CardTitle>
        <CardDescription>
          Runs on the machine where you watch — holds the CDP link to your Chrome + Migaku and
          plays episodes on one click.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="flex items-center justify-between gap-2">
          {isLoading ? (
            <Skeleton className="h-6 w-40" />
          ) : (
            <div className="flex flex-wrap items-center gap-x-4 gap-y-1">
              <StatusRow ok={connected} label={connected ? "Connected" : "Offline"} />
              <StatusRow
                ok={connected && migaku}
                label={connected ? (migaku ? "Migaku ✓" : "Migaku not logged in") : "Migaku —"}
              />
              {status?.ext_version && (
                <Badge variant="outline" title="Migaku extension version seen by the Connector">
                  ext v{status.ext_version}
                </Badge>
              )}
            </div>
          )}
          <Button variant="secondary" size="sm" onClick={() => refetch()} loading={isFetching}>
            <RefreshCw className="size-3.5" />
            Check
          </Button>
        </div>

        {!connected && !isLoading && (
          <p className="rounded-lg border border-border bg-bg-elevated/50 px-3 py-2 text-xs text-muted">
            Start the Connector on the machine where you watch (Chrome + Migaku). Play and live
            comprehension need it; the rest of the app works without it.
          </p>
        )}

        {devices.length > 0 && (
          <div className="space-y-1.5 rounded-lg border border-border bg-bg-elevated/40 p-3">
            <p className="text-xs font-medium text-muted">
              Devices
              <span className="ml-1.5 font-normal text-faint">
                — run the Connector on every machine you watch from; they stay connected together.
                Pick a Play target in the sidebar chip (or leave it on Auto).
              </span>
            </p>
            {devices.map((d) => (
              <div
                key={d.device_id}
                className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-md border border-border/60 bg-bg-elevated/60 px-2.5 py-1.5"
              >
                <span
                  className={cn(
                    "inline-flex size-2 shrink-0 rounded-full",
                    d.connected ? "bg-comp-green" : "bg-faint",
                  )}
                />
                <span className="text-xs font-medium text-fg">
                  {d.device_name || d.device_id}
                </span>
                {status?.default_device_id === d.device_id && (
                  <Badge variant="outline" title="Where an untargeted Play goes">
                    default
                  </Badge>
                )}
                <span className="text-[0.7rem] text-faint">
                  {d.connected
                    ? d.migaku
                      ? "Migaku ✓"
                      : d.chrome
                        ? "Chrome up · no Migaku"
                        : "no Chrome"
                    : `offline · seen ${formatRelativeUnix(d.last_seen)}`}
                </span>
                {d.ext_version && (
                  <span className="text-[0.7rem] text-faint">ext v{d.ext_version}</span>
                )}
                <span className="ml-auto font-mono text-[0.65rem] text-faint">{d.device_id}</span>
              </div>
            ))}
          </div>
        )}

        {setup && (
          <div className="space-y-2 rounded-lg border border-border bg-bg-elevated/40 p-3">
            <p className="text-xs font-medium text-muted">Set up on the machine where you watch</p>
            <p className="text-[0.7rem] text-faint">
              Run this one line there — it installs the Connector and auto-updates on each launch:
            </p>
            <CopyRow label="Install" value={installCmd} mono />
            {!setup.has_token && (
              <p className="text-[0.7rem] text-comp-amber">
                No token set — add <code className="rounded bg-bg-elevated px-1">MIMI_LAB_TOKEN</code> to the
                server .env before exposing it beyond localhost.
              </p>
            )}
            <details className="pt-0.5">
              <summary className="cursor-pointer text-[0.7rem] text-faint hover:text-muted">
                Manual setup / details
              </summary>
              <div className="mt-2 space-y-2">
                <CopyRow label="Server" value={setup.server_url} />
                {setup.has_token && <CopyRow label="Token" value={setup.token} secret />}
              </div>
            </details>
            <p className="pt-1 text-[0.7rem] text-faint">
              Needs Node + Chrome with Migaku (Early Access) on that machine. The Connector relays media
              over loopback, so a plain-http server on your LAN works — no HTTPS or certs needed. See{" "}
              <code className="rounded bg-bg-elevated px-1">connector/README.md</code>.
            </p>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function CopyRow({
  label,
  value,
  secret = false,
  mono = false,
}: {
  label: string;
  value: string;
  secret?: boolean;
  mono?: boolean;
}) {
  const [copied, setCopied] = useState(false);
  const [show, setShow] = useState(!secret);
  const copy = async () => {
    if (await copyText(value)) {
      setCopied(true);
      setTimeout(() => setCopied(false), 1200);
    }
  };
  return (
    <div className="flex items-center gap-2">
      <span className="w-14 shrink-0 text-[0.7rem] uppercase tracking-wide text-faint">{label}</span>
      <code
        className={cn(
          "min-w-0 flex-1 truncate rounded bg-bg-elevated px-2 py-1 text-xs text-fg",
          mono && "font-mono",
        )}
      >
        {show ? value : "•".repeat(Math.min(28, value.length))}
      </code>
      {secret && (
        <button
          onClick={() => setShow((s) => !s)}
          className="shrink-0 text-faint transition-colors hover:text-fg"
          aria-label={show ? "Hide token" : "Show token"}
        >
          {show ? <EyeOff className="size-3.5" /> : <Eye className="size-3.5" />}
        </button>
      )}
      <button
        onClick={copy}
        className="shrink-0 text-faint transition-colors hover:text-fg"
        aria-label={`Copy ${label}`}
      >
        {copied ? <CheckCircle2 className="size-3.5 text-comp-green" /> : <Copy className="size-3.5" />}
      </button>
    </div>
  );
}

function KnownWordsCard() {
  const { data, isLoading } = useKnownSummary();
  const growth = useKnownGrowth(7);
  const sync = useKnownSync();

  const stats = [
    { label: "Known", value: data?.known, color: "var(--color-comp-green)" },
    { label: "Learning", value: data?.learning, color: "var(--color-comp-amber)" },
    { label: "Unknown", value: data?.unknown, color: "var(--color-comp-red)" },
    { label: "Ignored", value: data?.ignored, color: "var(--color-faint)" },
  ];

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <BookMarked className="size-4 text-brand-bright" />
          Known Words
        </CardTitle>
        <CardDescription>
          Synced from Migaku&apos;s WordList — the authoritative set behind comprehension.
        </CardDescription>
      </CardHeader>
      <CardContent>
        {isLoading ? (
          <Skeleton className="h-20 w-full" />
        ) : (
          <div className="grid grid-cols-4 gap-2">
            {stats.map((s) => (
              <div
                key={s.label}
                className="rounded-lg border border-border bg-bg-elevated/50 p-3 text-center"
              >
                <p className="text-xl font-bold tabular-nums" style={{ color: s.color }}>
                  {(s.value ?? 0).toLocaleString()}
                </p>
                <p className="mt-0.5 text-[0.7rem] uppercase tracking-wide text-faint">
                  {s.label}
                </p>
              </div>
            ))}
          </div>
        )}
        {growth.data && growth.data.delta > 0 && (
          <p className="mt-3 flex items-center gap-1.5 text-sm font-medium text-comp-green">
            <TrendingUp className="size-4" />
            +{growth.data.delta.toLocaleString()} known words in the last {growth.data.days} days
          </p>
        )}
        <div className="mt-4 flex items-center justify-between">
          <p className="text-xs text-faint">
            Updated {formatRelative(data?.updated_at)}
            {data?.total ? ` · ${data.total.toLocaleString()} total` : ""}
          </p>
          <Button
            variant="primary"
            size="sm"
            onClick={() => sync.mutate()}
            loading={sync.isPending}
          >
            <RotateCw className="size-3.5" />
            Sync from Migaku
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}

function EnglishSubsCard() {
  const { data, isLoading } = useEnglishConfig();
  const setSource = useSetEnglishSource();
  const source = data?.source ?? "human";

  const opts = [
    {
      value: "human" as const,
      label: "Human subtitles",
      hint: "Embedded softsub from the release, else the official subs via AnimeTosho.",
      ready: true,
      warn: undefined,
    },
    {
      value: "llm" as const,
      label: "Machine translation",
      hint: `Translate the Japanese with Claude (${data?.translation_model ?? "Sonnet"}).`,
      ready: !!data?.anthropic_configured,
      warn:
        data && !data.anthropic_configured ? "Set ANTHROPIC_API_KEY to enable." : undefined,
    },
  ];

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Languages className="size-4 text-brand-bright" />
          English Subtitles
        </CardTitle>
        <CardDescription>
          A native-language reference track shown beside the Japanese study line — turn on
          &ldquo;secondary subtitles&rdquo; in Migaku&apos;s own settings to display it. Japanese
          stays the study language.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        {isLoading ? (
          <Skeleton className="h-24 w-full" />
        ) : !data?.enabled ? (
          <p className="rounded-lg border border-border bg-bg-elevated/50 px-3 py-2 text-xs text-muted">
            English subtitles are disabled. Set{" "}
            <code className="rounded bg-bg-elevated px-1">ENGLISH_SUBS_ENABLED=true</code> in the
            server .env.
          </p>
        ) : (
          <div className="grid gap-2">
            {opts.map((o) => {
              const active = source === o.value;
              return (
                <button
                  key={o.value}
                  disabled={setSource.isPending || (!o.ready && !active)}
                  onClick={() => !active && setSource.mutate(o.value)}
                  className={cn(
                    "rounded-lg border px-3 py-2.5 text-left transition-colors",
                    active
                      ? "border-brand-bright bg-brand-bright/10"
                      : "border-border bg-bg-elevated/40 hover:bg-bg-elevated/70",
                    !o.ready && !active && "cursor-not-allowed opacity-60",
                  )}
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="inline-flex items-center gap-2 text-sm font-medium text-fg">
                      {o.value === "llm" ? (
                        <Sparkles className="size-3.5 text-brand-bright" />
                      ) : (
                        <BookMarked className="size-3.5 text-faint" />
                      )}
                      {o.label}
                    </span>
                    {active && <CheckCircle2 className="size-4 text-comp-green" />}
                  </div>
                  <p className="mt-0.5 text-[0.7rem] text-faint">{o.hint}</p>
                  {o.warn && <p className="mt-1 text-[0.7rem] text-comp-amber">{o.warn}</p>}
                </button>
              );
            })}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function MalCard() {
  const { data, isError, isLoading } = useMalStatus();
  const sync = useMalSync();
  const authed = !isError && !!data?.authed;
  const configured = !isError && !!data?.configured;

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Link2 className="size-4 text-brand-bright" />
          MyAnimeList
        </CardTitle>
        <CardDescription>Two-way sync of your list, status, score, and progress.</CardDescription>
      </CardHeader>
      <CardContent className="flex items-center justify-between gap-3">
        {isLoading ? (
          <Skeleton className="h-6 w-32" />
        ) : (
          <div className="flex flex-wrap items-center gap-x-4 gap-y-1">
            <StatusRow ok={authed} label={authed ? "Connected" : "Not connected"} />
            {authed && data?.synced_titles != null && (
              <span className="text-xs text-faint">
                {data.synced_titles.toLocaleString()} titles synced
              </span>
            )}
          </div>
        )}
        <div className="flex shrink-0 items-center gap-2">
          {authed && (
            <Button variant="secondary" size="sm" onClick={() => sync.mutate()} loading={sync.isPending}>
              <RotateCw className="size-3.5" />
              Sync now
            </Button>
          )}
          <Button variant={authed ? "secondary" : "primary"} size="sm" asChild>
            <a href={MAL_AUTH_URL}>
              <ExternalLink className="size-3.5" />
              {authed ? "Reconnect" : "Connect MyAnimeList"}
            </a>
          </Button>
        </div>
      </CardContent>
      {!isLoading && (
        <CardContent className="pt-0">
          {!configured && !authed ? (
            <p className="text-xs text-comp-amber">
              Set <code className="rounded bg-bg-elevated px-1">MAL_CLIENT_ID</code> /{" "}
              <code className="rounded bg-bg-elevated px-1">MAL_CLIENT_SECRET</code> in the server
              .env to enable the OAuth flow.
            </p>
          ) : (
            <p className="text-xs text-faint">
              Last sync {formatRelativeUnix(data?.last_sync)}
              {authed && data?.expires_at != null && (
                <>
                  {" "}
                  · token expires {formatDateUnix(data.expires_at)}
                  {data.has_refresh_token ? " (auto-refreshes)" : ""}
                </>
              )}
            </p>
          )}
        </CardContent>
      )}
    </Card>
  );
}

// ── Integration health (drift check + full self-check) ──────────────────
function DriftSummary({ report }: { report: MigakuDriftReport }) {
  const ok = report.ok !== false;
  const rest = Object.fromEntries(Object.entries(report).filter(([k]) => k !== "ok"));
  return (
    <div className="space-y-2 rounded-lg border border-border bg-bg-elevated/50 p-3">
      <StatusRow ok={ok} label={ok ? "Tokenizer in sync with Migaku" : "Drift detected"} />
      {Object.keys(rest).length > 0 && (
        <pre className="max-h-44 overflow-auto rounded-md bg-black/30 p-2.5 text-[0.68rem] leading-relaxed text-muted">
          {JSON.stringify(rest, null, 2)}
        </pre>
      )}
    </div>
  );
}

function SelfcheckSummary({ result }: { result: SelfcheckResult }) {
  const checks: { key: keyof SelfcheckResult["checks"]; label: string }[] = [
    { key: "played", label: "Played an episode" },
    { key: "tokenized", label: "Tokenized subtitles" },
    { key: "panel_scraped", label: "Scraped the Migaku panel" },
  ];
  return (
    <div className="space-y-2 rounded-lg border border-border bg-bg-elevated/50 p-3">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1.5">
        {checks.map((c) => (
          <StatusRow key={c.key} ok={!!result.checks?.[c.key]} label={c.label} />
        ))}
        {result.ext_version && <Badge variant="outline">ext v{result.ext_version}</Badge>}
      </div>
      {result.error && <p className="text-xs text-comp-red">{result.error}</p>}
      {result.drift && Object.keys(result.drift).length > 0 && (
        <pre className="max-h-44 overflow-auto rounded-md bg-black/30 p-2.5 text-[0.68rem] leading-relaxed text-muted">
          {JSON.stringify(result.drift, null, 2)}
        </pre>
      )}
    </div>
  );
}

function IntegrationHealthCard() {
  const { data: connector } = useConnectorStatus();
  const drift = useMigakuDriftCheck();
  const selfcheck = useConnectorSelfcheck();
  const connectorOnline = !!connector?.connected;

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Stethoscope className="size-4 text-brand-bright" />
          Integration health
        </CardTitle>
        <CardDescription>
          Verify that comprehension scoring still agrees with Migaku&apos;s tokenizer, and that
          the full play → tokenize → scrape loop works end to end.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="space-y-2">
          <div className="flex items-center justify-between gap-3">
            <div>
              <p className="text-sm font-medium text-fg">Tokenizer drift check</p>
              <p className="text-[0.7rem] text-faint">
                Fast local test — compares our tokenization against Migaku&apos;s reference set.
              </p>
            </div>
            <Button
              variant="secondary"
              size="sm"
              onClick={() => drift.mutate()}
              loading={drift.isPending}
            >
              <Stethoscope className="size-3.5" />
              Run check
            </Button>
          </div>
          {drift.data && <DriftSummary report={drift.data} />}
        </div>

        <div className="space-y-2 border-t border-border pt-4">
          <div className="flex items-center justify-between gap-3">
            <div>
              <p className="text-sm font-medium text-fg">Full self-check</p>
              <p className="text-[0.7rem] text-faint">
                End-to-end smoke test through the Connector + Migaku — takes up to 90 seconds.
              </p>
            </div>
            <Button
              variant="secondary"
              size="sm"
              onClick={() => selfcheck.mutate()}
              loading={selfcheck.isPending}
              disabled={!connectorOnline}
              title={
                connectorOnline
                  ? "Play a probe clip through Migaku and verify every stage"
                  : "Connector offline — start it on the machine where you watch"
              }
            >
              <FlaskConical className="size-3.5" />
              {selfcheck.isPending ? "Running (≤90s)…" : "Run self-check"}
            </Button>
          </div>
          {!connectorOnline && (
            <p className="text-[0.7rem] text-comp-amber">
              Connector offline — the self-check needs Chrome + Migaku running on the watching
              machine.
            </p>
          )}
          {selfcheck.data && <SelfcheckSummary result={selfcheck.data} />}
        </div>
      </CardContent>
    </Card>
  );
}

function PathsCard() {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <FolderTree className="size-4 text-brand-bright" />
          Library & Sources
        </CardTitle>
        <CardDescription>Where media lives and which services feed the pipeline.</CardDescription>
      </CardHeader>
      <CardContent className="space-y-2.5 text-sm">
        <InfoRow icon={FolderTree} label="Library" value="Library/<Title>/Season NN/" />
        <InfoRow icon={KeyRound} label="Subtitles" value="jimaku.cc (token configured server-side)" />
        <InfoRow icon={FolderTree} label="Clips" value="/clips/line_<id>.{jpg,m4a}" />
        <InfoRow icon={KeyRound} label="Acquisition" value="nyaa.si → Transmission" />
        <p className="pt-1 text-xs text-faint">
          Secrets (jimaku token, MAL credentials, paths) are managed server-side in{" "}
          <code className="rounded bg-bg-elevated px-1 py-0.5 text-[0.7rem]">.env</code>.
        </p>
      </CardContent>
    </Card>
  );
}

function InfoRow({
  icon: Icon,
  label,
  value,
}: {
  icon: React.ComponentType<{ className?: string }>;
  label: string;
  value: string;
}) {
  return (
    <div className="flex items-center justify-between gap-3 rounded-lg border border-border bg-bg-elevated/50 px-3 py-2">
      <span className="inline-flex items-center gap-2 text-muted">
        <Icon className="size-3.5 text-faint" />
        {label}
</span>
      <code className="truncate text-xs text-fg">{value}</code>
    </div>
  );
}
