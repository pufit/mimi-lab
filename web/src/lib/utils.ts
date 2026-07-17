import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

/** Best display title: prefer English, fall back to romaji, then native. */
export function titleName(t: {
  english?: string | null;
  romaji?: string | null;
  native?: string | null;
}): string {
  return t.english || t.romaji || t.native || "Untitled";
}

export function titleSub(t: {
  english?: string | null;
  romaji?: string | null;
}): string | null {
  // Show the romaji as a secondary line when English is the primary.
  if (t.english && t.romaji && t.english !== t.romaji) return t.romaji;
  return null;
}

// ── Comprehension color scale ──────────────────────────────────────────
// <50 red · 50–75 amber · 75–90 lime · 90+ green
export type CompTier = "red" | "amber" | "lime" | "green";

export function compTier(pct: number): CompTier {
  if (pct < 50) return "red";
  if (pct < 75) return "amber";
  if (pct < 90) return "lime";
  return "green";
}

const TIER_VAR: Record<CompTier, string> = {
  red: "var(--color-comp-red)",
  amber: "var(--color-comp-amber)",
  lime: "var(--color-comp-lime)",
  green: "var(--color-comp-green)",
};

export function compColor(pct: number): string {
  return TIER_VAR[compTier(pct)];
}

/** Inline styles for a comprehension pill (tinted bg + colored text/border). */
export function compPillStyle(pct: number): React.CSSProperties {
  const c = compColor(pct);
  return {
    color: c,
    backgroundColor: `color-mix(in oklab, ${c} 16%, transparent)`,
    borderColor: `color-mix(in oklab, ${c} 35%, transparent)`,
  };
}

export function formatPct(pct?: number | null): string {
  if (pct == null) return "—";
  return `${Math.round(pct)}%`;
}

export function formatMs(ms?: number | null): string {
  if (ms == null) return "0:00";
  const total = Math.floor(ms / 1000);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const mm = h > 0 ? String(m).padStart(2, "0") : String(m);
  const ss = String(s).padStart(2, "0");
  return h > 0 ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
}

export function formatBytes(bytes?: number | null): string {
  if (bytes == null || bytes <= 0) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let v = bytes;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v.toFixed(v >= 100 || i === 0 ? 0 : 1)} ${units[i]}`;
}

export function formatRelative(iso?: string | null): string {
  if (!iso) return "never";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return iso;
  const diff = Date.now() - then;
  const sec = Math.floor(diff / 1000);
  if (sec < 60) return "just now";
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min}m ago`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return `${hr}h ago`;
  const day = Math.floor(hr / 24);
  if (day < 30) return `${day}d ago`;
  return new Date(iso).toLocaleDateString();
}

/** Relative time from a unix-SECONDS timestamp (MAL/connector timestamps). */
export function formatRelativeUnix(sec?: number | null): string {
  if (sec == null || sec <= 0) return "never";
  return formatRelative(new Date(sec * 1000).toISOString());
}

/** Absolute local date from a unix-SECONDS timestamp. */
export function formatDateUnix(sec?: number | null): string {
  if (sec == null || sec <= 0) return "—";
  return new Date(sec * 1000).toLocaleDateString();
}

/** Strip AniList HTML tags from a synopsis for plain rendering. */
export function stripHtml(html?: string | null): string {
  if (!html) return "";
  return html
    .replace(/<br\s*\/?>/gi, "\n")
    .replace(/<[^>]+>/g, "")
    .replace(/&nbsp;/g, " ")
    .replace(/&amp;/g, "&")
    .replace(/&quot;/g, '"')
    .replace(/&#039;/g, "'")
    .trim();
}

// ── MAL status presentation ────────────────────────────────────────────
const MAL_LABELS: Record<string, string> = {
  watching: "Watching",
  completed: "Completed",
  on_hold: "On Hold",
  dropped: "Dropped",
  plan_to_watch: "Plan to Watch",
};

export function malStatusLabel(s?: string | null): string {
  if (!s) return "—";
  return MAL_LABELS[s] ?? s.replace(/_/g, " ");
}

export const MAL_FILTERS: { value: string; label: string }[] = [
  { value: "watching", label: "Watching" },
  { value: "plan_to_watch", label: "Plan" },
  { value: "completed", label: "Completed" },
  { value: "on_hold", label: "On Hold" },
  { value: "dropped", label: "Dropped" },
];

// Display order for the Library's status groups:
// planned · watching · watched · suspended · dropped.
export const MAL_CATEGORY_ORDER = [
  "plan_to_watch",
  "watching",
  "completed",
  "on_hold",
  "dropped",
] as const;

/** Friendly label for an AniList media format ("TV" → "TV", "TV_SHORT" → "TV Short"). */
const FORMAT_LABELS: Record<string, string> = {
  TV: "TV",
  TV_SHORT: "TV Short",
  MOVIE: "Movie",
  SPECIAL: "Special",
  OVA: "OVA",
  ONA: "ONA",
  MUSIC: "Music",
};

export function formatType(fmt?: string | null): string {
  if (!fmt) return "—";
  return FORMAT_LABELS[fmt] ?? fmt.replace(/_/g, " ");
}

// ── Library sorting ─────────────────────────────────────────────────────
export type LibrarySort = "name" | "score" | "comprehension";

export const LIBRARY_SORTS: { value: LibrarySort; label: string }[] = [
  { value: "name", label: "Name" },
  { value: "score", label: "My score" },
  { value: "comprehension", label: "Comprehension" },
];

type SortableTitle = {
  english?: string | null;
  romaji?: string | null;
  native?: string | null;
  mal_score?: number | null;
  avg_comprehension?: number | null;
};

/**
 * Comparator for a chosen library sort. Name is ascending (A→Z); score and
 * comprehension are descending (best first) with missing values sorted last,
 * always breaking ties by name so the order is stable and predictable.
 */
export function librarySortComparator(sort: LibrarySort) {
  const byName = (a: SortableTitle, b: SortableTitle) =>
    titleName(a).localeCompare(titleName(b), undefined, { sensitivity: "base" });
  return (a: SortableTitle, b: SortableTitle): number => {
    if (sort === "score") {
      const d = (b.mal_score || 0) - (a.mal_score || 0);
      return d !== 0 ? d : byName(a, b);
    }
    if (sort === "comprehension") {
      const d = (b.avg_comprehension ?? -1) - (a.avg_comprehension ?? -1);
      return d !== 0 ? d : byName(a, b);
    }
    return byName(a, b);
  };
}

export function ratingLabel(rating?: string | null): string | null {
  if (!rating) return null;
  return rating
    .replace(/_/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}
