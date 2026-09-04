import { type ReactNode } from "react";
import { NavLink } from "react-router-dom";
import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import {
  Activity,
  BarChart3,
  GraduationCap,
  LibraryBig,
  Search,
  DownloadCloud,
  ListChecks,
  Settings,
  Sparkles,
  Bell,
  CheckCheck,
  Inbox,
  Target,
} from "lucide-react";
import { cn, formatRelative } from "@/lib/utils";
import {
  useQueue,
  useConnectorStatus,
  useEvents,
  useUnreadCount,
  useMarkEventsRead,
  useTargetDevice,
  setTargetDevice,
} from "@/lib/hooks";
import { useSrsSummary } from "@/lib/srs-hooks";
import type { AppEvent } from "@/lib/types";

const NAV = [
  { to: "/", label: "Library", icon: LibraryBig, end: true },
  { to: "/next", label: "Watch next", icon: Target, end: false },
  { to: "/study", label: "Study", icon: GraduationCap, end: false },
  { to: "/moments", label: "Moments", icon: Search, end: false },
  { to: "/stats", label: "Stats", icon: BarChart3, end: false },
  { to: "/acquire", label: "Acquire", icon: DownloadCloud, end: false },
  { to: "/queue", label: "Queue", icon: ListChecks, end: false },
  { to: "/system", label: "System", icon: Activity, end: false },
  { to: "/settings", label: "Settings", icon: Settings, end: false },
];

function ConnectorDot() {
  const { data } = useConnectorStatus();
  const target = useTargetDevice();
  const online = !!data?.connected;
  const migaku = !!data?.migaku;
  const devices = (data?.devices ?? []).filter((d) => d.connected);
  const multi = devices.length > 1;
  // A remembered target that's gone offline still shows (marked) so the user
  // sees why plays fail and can switch back to Auto.
  const targetOffline = !!target && !devices.some((d) => d.device_id === target);
  return (
    <div className="rounded-lg border border-border bg-bg-elevated/60 px-3 py-2">
      <div className="flex items-center gap-2">
        <span className="relative flex size-2">
          {online && (
            <span className="absolute inline-flex size-full animate-ping rounded-full bg-comp-green opacity-60" />
          )}
          <span
            className={cn(
              "relative inline-flex size-2 rounded-full",
              online ? "bg-comp-green" : "bg-faint",
            )}
          />
        </span>
        <div className="min-w-0">
          <p className="text-[0.7rem] font-medium leading-tight text-fg">Connector</p>
          <p className="truncate text-[0.65rem] leading-tight text-faint">
            {online
              ? multi
                ? `${devices.length} devices · ${migaku ? "Migaku ✓" : "No Migaku"}`
                : migaku
                  ? "Ready · Migaku ✓"
                  : "No Migaku"
              : "Offline"}
          </p>
        </div>
      </div>
      {(multi || targetOffline) && (
        <select
          value={target}
          onChange={(e) => setTargetDevice(e.target.value)}
          title="Which device Play / sync should target"
          className="mt-1.5 w-full rounded-md border border-border bg-bg-elevated px-1.5 py-1 text-[0.65rem] text-fg outline-none focus:border-brand"
        >
          <option value="">Auto (best device)</option>
          {devices.map((d) => (
            <option key={d.device_id} value={d.device_id}>
              {(d.device_name || d.device_id) + (d.migaku ? "" : " · no Migaku")}
            </option>
          ))}
          {targetOffline && (
            <option value={target}>{target} (offline)</option>
          )}
        </select>
      )}
    </div>
  );
}

function QueueBadge() {
  const { data } = useQueue();
  const count = data?.length ?? 0;
  if (count === 0) return null;
  return (
    <span className="ml-auto grid h-5 min-w-5 place-items-center rounded-full bg-brand px-1.5 text-[0.65rem] font-semibold text-white">
      {count}
    </span>
  );
}

/** Learning + review cards waiting right now — same pattern as `QueueBadge`. */
function StudyBadge() {
  const { data } = useSrsSummary();
  const count = (data?.due_learning ?? 0) + (data?.due_review ?? 0);
  if (count === 0) return null;
  return (
    <span
      className="ml-auto grid h-5 min-w-5 place-items-center rounded-full bg-brand px-1.5 text-[0.65rem] font-semibold text-white"
      title={`${data?.due_learning ?? 0} learning · ${data?.due_review ?? 0} review due`}
    >
      {count > 99 ? "99+" : count}
    </span>
  );
}

function kindDot(kind: AppEvent["kind"]) {
  const c =
    kind === "error"
      ? "bg-red-500"
      : kind === "warning"
        ? "bg-amber-500"
        : kind === "success"
          ? "bg-comp-green"
          : "bg-brand-bright";
  return <span className={cn("mt-1.5 size-2 shrink-0 rounded-full", c)} />;
}

function NotificationBell() {
  const { data: unread } = useUnreadCount();
  const { data: events } = useEvents(false, 30);
  const markRead = useMarkEventsRead();
  const count = unread ?? 0;
  return (
    <DropdownMenu.Root
      onOpenChange={(open) => {
        if (!open && count > 0) markRead.mutate({ all: true });
      }}
    >
      <DropdownMenu.Trigger asChild>
        <button
          aria-label="Notifications"
          className="relative grid size-9 place-items-center rounded-lg text-muted transition-colors hover:bg-surface-hover hover:text-fg"
        >
          <Bell className="size-[1.15rem]" />
          {count > 0 && (
            <span className="absolute -right-0.5 -top-0.5 grid h-4 min-w-4 place-items-center rounded-full bg-brand px-1 text-[0.6rem] font-semibold text-white">
              {count > 99 ? "99+" : count}
            </span>
          )}
        </button>
      </DropdownMenu.Trigger>
      <DropdownMenu.Portal>
        <DropdownMenu.Content
          align="end"
          sideOffset={8}
          className="z-50 w-80 overflow-hidden rounded-xl border border-border bg-bg-elevated/95 shadow-2xl backdrop-blur-xl"
        >
          <div className="flex items-center justify-between border-b border-border px-3 py-2.5">
            <span className="text-sm font-semibold text-fg">Notifications</span>
            <button
              onClick={() => markRead.mutate({ all: true })}
              className="flex items-center gap-1 text-[0.7rem] text-muted transition-colors hover:text-brand-bright"
            >
              <CheckCheck className="size-3.5" /> Mark all read
            </button>
          </div>
          <div className="max-h-96 overflow-y-auto">
            {!events || events.length === 0 ? (
              <div className="flex flex-col items-center gap-2 px-4 py-10 text-center">
                <Inbox className="size-6 text-faint" />
                <p className="text-sm text-faint">No notifications yet</p>
              </div>
            ) : (
              events.map((e) => (
                <div
                  key={e.id}
                  className={cn(
                    "flex gap-2.5 border-b border-border/50 px-3 py-2.5 last:border-0",
                    !e.read && "bg-brand/5",
                  )}
                >
                  {kindDot(e.kind)}
                  <div className="min-w-0 flex-1">
                    <p className="text-sm leading-tight text-fg">{e.title}</p>
                    {e.detail && (
                      <p className="mt-0.5 truncate text-xs text-muted">{e.detail}</p>
                    )}
                    <p className="mt-0.5 text-[0.65rem] text-faint">
                      {formatRelative(e.created_at)}
                    </p>
                  </div>
                </div>
              ))
            )}
          </div>
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
  );
}

/**
 * `chromeless` (set by `App.tsx` for `/study/review`, design §8.1) hides the
 * mobile `h-14` header and drops the `pt-20`/`pb-20` content padding so a
 * sticky rating bar can sit at the bottom edge of the viewport. The desktop
 * sidebar stays — it never overlaps the content column.
 */
export function AppShell({
  children,
  chromeless = false,
}: {
  children: ReactNode;
  chromeless?: boolean;
}) {
  return (
    <div className="flex min-h-screen">
      {/* Fixed sidebar */}
      <aside className="fixed inset-y-0 left-0 z-40 hidden w-60 flex-col border-r border-border bg-bg-elevated/70 px-4 py-5 backdrop-blur-xl md:flex">
        <div className="mb-8 flex items-center gap-2.5 px-2">
          <div className="grid size-9 place-items-center rounded-xl bg-gradient-to-br from-brand-bright to-brand-dim shadow-lg shadow-brand/30">
            <Sparkles className="size-5 text-white" />
          </div>
          <div className="leading-tight">
            <p className="text-sm font-semibold tracking-tight text-fg">Mimi Lab</p>
            <p className="text-[0.65rem] text-faint">Watch · Learn · Immerse</p>
          </div>
          <div className="ml-auto">
            <NotificationBell />
          </div>
        </div>

        <nav className="flex flex-1 flex-col gap-1">
          {NAV.map(({ to, label, icon: Icon, end }) => (
            <NavLink
              key={to}
              to={to}
              end={end}
              className={({ isActive }) =>
                cn(
                  "group flex items-center gap-3 rounded-lg px-3 py-2 text-sm font-medium transition-colors",
                  isActive
                    ? "bg-brand/15 text-fg"
                    : "text-muted hover:bg-surface-hover hover:text-fg",
                )
              }
            >
              {({ isActive }) => (
                <>
                  <Icon
                    className={cn(
                      "size-[1.15rem] shrink-0 transition-colors",
                      isActive ? "text-brand-bright" : "text-faint group-hover:text-muted",
                    )}
                  />
                  {label}
                  {label === "Queue" && <QueueBadge />}
                  {label === "Study" && <StudyBadge />}
                </>
              )}
            </NavLink>
          ))}
        </nav>

        <div className="mt-4">
          <ConnectorDot />
        </div>
      </aside>

      {/* Mobile top bar (hidden on chromeless routes) */}
      <header
        className={cn(
          "fixed inset-x-0 top-0 z-40 flex h-14 items-center gap-2 border-b border-border bg-bg-elevated/90 px-4 backdrop-blur-xl md:hidden",
          chromeless && "hidden",
        )}
      >
        <div className="grid size-7 place-items-center rounded-lg bg-gradient-to-br from-brand-bright to-brand-dim">
          <Sparkles className="size-4 text-white" />
        </div>
        <span className="text-sm font-semibold">Mimi Lab</span>
        <div className="ml-auto" />
        <NotificationBell />
        <nav className="flex items-center gap-1">
          {NAV.map(({ to, label, icon: Icon, end }) => (
            <NavLink
              key={to}
              to={to}
              end={end}
              aria-label={label}
              className={({ isActive }) =>
                cn(
                  "grid size-9 place-items-center rounded-lg transition-colors",
                  isActive ? "bg-brand/15 text-brand-bright" : "text-muted hover:bg-surface-hover",
                )
              }
            >
              <Icon className="size-5" />
            </NavLink>
          ))}
        </nav>
      </header>

      {/* Main content */}
      <main className="min-w-0 flex-1 md:pl-60">
        <div
          className={cn(
            "mx-auto max-w-[1500px]",
            chromeless
              ? "min-h-screen px-4 md:px-6"
              : "px-5 pb-20 pt-20 md:px-8 md:pt-8",
          )}
        >
          {children}
        </div>
      </main>
    </div>
  );
}
