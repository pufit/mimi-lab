import { type ReactNode } from "react";
import { Routes, Route, matchPath, useLocation } from "react-router-dom";
import { usePageTitle, useServerEvents } from "./lib/hooks";
import { AppShell } from "./components/layout/app-shell";
import { ErrorBoundary } from "./components/error-boundary";
import { LibraryPage } from "./pages/library";
import { TitlePage } from "./pages/title";
import { NextPage } from "./pages/next";
import { StudyPage } from "./pages/study";
import { ReviewPage } from "./pages/study-review";
import { StackPage } from "./pages/study-stack";
import { CardDetailPage } from "./pages/study-card";
import { StudyStatsPage } from "./pages/study-stats";
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

/**
 * `chromeless` routes render without the mobile top bar and without the
 * `pt-20`/`pb-20` content padding, so a sticky bottom bar (the review rating
 * buttons) can sit flush against the viewport edge (design §8.1).
 */
const ROUTES: { path: string; title: string; el: ReactNode; chromeless?: boolean }[] = [
  { path: "/", title: "Library", el: <LibraryPage /> },
  { path: "/next", title: "Watch next", el: <NextPage /> },
  { path: "/study", title: "Study", el: <StudyPage /> },
  { path: "/study/review", title: "Review", el: <ReviewPage />, chromeless: true },
  { path: "/study/stack", title: "Stack", el: <StackPage /> },
  { path: "/study/cards/:id", title: "Card", el: <CardDetailPage /> },
  { path: "/study/stats", title: "Study stats", el: <StudyStatsPage /> },
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
  const { pathname } = useLocation();
  const chromeless = ROUTES.some((r) => r.chromeless && matchPath(r.path, pathname) !== null);
  return (
    <>
      <div className="app-aurora" />
      <AppShell chromeless={chromeless}>
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
