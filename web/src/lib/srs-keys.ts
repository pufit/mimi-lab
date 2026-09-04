// TanStack Query keys for the SRS surfaces (design §8.2).
//
// Every key starts with "srs" so one `invalidateQueries({queryKey: ["srs"]})`
// prefix match covers the whole feature — the SSE branch in `hooks.ts` relies on
// that, and on `queryKey[1]` being the surface name (it excludes "queue", which
// is fetched imperatively, and "cards" while a stack move is in flight).

export const qkSrs = {
  all: ["srs"] as const,
  summary: ["srs", "summary"] as const,
  queue: ["srs", "queue"] as const,
  cards: (state: string, q: string, sort: string, order: string) =>
    ["srs", "cards", state, q, sort, order] as const,
  card: (id: number) => ["srs", "card", id] as const,
  candidates: (status: string, q: string, sort: string) =>
    ["srs", "candidates", status, q, sort] as const,
  generation: ["srs", "generation"] as const,
  settings: ["srs", "settings"] as const,
  stats: (days: number) => ["srs", "stats", days] as const,
};

/** Prefix of every card-list key — use for optimistic `setQueriesData`. */
export const qkSrsCardsAll = ["srs", "cards"] as const;
