import { type ReactNode } from "react";
import { Routes, Route, useLocation } from "react-router-dom";
import { usePageTitle, useServerEvents } from "./lib/hooks";
import { AppShell } from "./components/layout/app-shell";
import { ErrorBoundary } from "./components/error-boundary";
import { LibraryPage } from "./pages/library";
import { TitlePage } from "./pages/title";
import { NextPage } from "./pages/next";
import { StudyPage } from "./pages/study";
import { MomentsPage } from "./pages/moments";
import { StatsPage } from "./pages/stats";
import { TranscriptPage } from "./pages/transcript";
import { AcquirePage } from "./pages/acquire";
import { QueuePage } from "./pages/queue";
import { SystemPage } from "./pages/system";
import { SettingsPage } from "./pages/settings";
import { NotFoundPage } from "./pages/not-found";

/**
 * Per-route error boundary + document.title. Keyed by pathname so a crash on
 * one page never bleeds into the next route, and one broken page can't blank
 * the whole shell/nav.
 */
function RouteBoundary({ title, children }: { title: string; children: ReactNode }) {
  usePageTitle(title);
  const { pathname } = useLocation();
  return <ErrorBoundary key={pathname}>{children}</ErrorBoundary>;
}

const ROUTES: { path: string; title: string; el: ReactNode }[] = [
  { path: "/", title: "Library", el: <LibraryPage /> },
  { path: "/next", title: "Watch next", el: <NextPage /> },
  { path: "/study", title: "Study", el: <StudyPage /> },
  { path: "/title/:id", title: "Title", el: <TitlePage /> },
  { path: "/moments", title: "Moments", el: <MomentsPage /> },
  { path: "/stats", title: "Stats", el: <StatsPage /> },
  { path: "/transcript/:episodeId", title: "Transcript", el: <TranscriptPage /> },
  { path: "/acquire", title: "Acquire", el: <AcquirePage /> },
  { path: "/queue", title: "Queue", el: <QueuePage /> },
  { path: "/system", title: "System", el: <SystemPage /> },
  { path: "/settings", title: "Settings", el: <SettingsPage /> },
  { path: "*", title: "Not found", el: <NotFoundPage /> },
];

export function App() {
  useServerEvents(); // one live SSE connection → invalidate queries on push
  return (
    <>
      <div className="app-aurora" />
      <AppShell>
        <Routes>
          {ROUTES.map((r) => (
            <Route
              key={r.path}
              path={r.path}
              element={<RouteBoundary title={r.title}>{r.el}</RouteBoundary>}
            />
          ))}
        </Routes>
      </AppShell>
    </>
  );
}
