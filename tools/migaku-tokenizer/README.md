# migaku-tokenizer — Migaku's own analyzer, in pure Node

Runs Migaku's bundled **Kotlin/JS analyzer** (the real Kuromoji + statistical
models + dictForm pipeline) **entirely in Node** — no browser, no network — to
produce **Migaku-exact** tokens (`surface`, `dictForm`, `reading`, `pos`,
`pitch`). Using these tokens, our comprehension matches Migaku's own value
almost exactly (see Results), because tokenizer divergence was the sole cause of
the old gap.

## Why
Our server-side "aligned" comprehension used `fugashi`+UniDic, which segments
differently than Migaku. Reusing Migaku's own tokenizer removes that source of
drift.

## How it works
The analyzer is one ~6 MB file: `assets/player-store-<hash>.js`, Kotlin/JS
compiled, self-contained. It boots an in-process **"Core"** exposing a JSON-RPC
API (methods registered by dotted name, e.g. `ParsingMgr.parseTokensMany`,
`CatalogMgr.calculateComprehensionScoreFromText`). We:

1. **`shims.mjs`** — install the browser/runtime globals the bundle expects:
   `self/window/document` (a tiny fake-DOM), `require` (via `createRequire`),
   `fake-indexeddb`, an **OPFS-over-disk** `navigator.storage` (`opfs.mjs`), and
   — the key unlock — an **`http(s).request` interception** that serves the
   bundle's dictionary requests (it builds a hostless URL `///core/fst.bin` in
   its isNode path) straight from the extension's `core/` dir on disk.
2. **`core.mjs`** — patch a copy of the bundle to re-export its internal, already
   wired Core JSON-RPC client (`Inr`), import it, and expose `call(method,params)`.
   Mangled names (`Inr`/`Anr`/`Tnr`) are re-derived by regex per build, so it
   survives extension updates.
3. **`index.mjs`** — `tokenizeLine(text)` / `tokenizeLines(texts)`.

## Usage
```bash
cd tools/migaku-tokenizer
node index.mjs "彼は学校に行きたくなかった。"   # → tokens (行く lemmatized, readings, pitch)
```

## Validation

Validate segmentation and comprehension against the repository's synthetic
TestShow fixture. Deployment-specific scores, vocabulary totals, and library
benchmarks are intentionally omitted.

## Caveats
- **Per-line only.** `parseTokensMany` with many texts at once routes through a
  Worker (`postMessage`) that isn't shimmed; call one line at a time (in-process).
- **Brittleness.** Depends on the extension's bundle internals; mangled names are
  re-derived per build, but a major Migaku rewrite could require updating the
  regexes / shims. Pin a known-good extension version if stability matters.
- **Residual <1 pt** vs Migaku is the formula/ignored-denominator nuance; the
  native `CatalogMgr.calculateComprehensionScoreFromText` would zero it out but
  needs the known-set loaded into Core's `WordListMgr` first.

## Pipeline integration (shipped)
Tokenize+join is the comprehension source (the native `CatalogMgr` scorer is
blocked offline because it needs Core's SQLite DB, which is gated on Firebase
authentication that never resolves in a logged-out process).

- **Sidecar** `server.mjs` — long-lived HTTP service that loads Core once and
  batch-tokenizes subtitle lines.
  `GET /health`, `POST /tokenize {lines,lang}`. Run as LaunchAgent
  `com.mimilab.tokenizer` on :8788 (`deploy/install-launchd.sh`).
- **Python client** `app/learn/migaku_tok.py` — `tokenize_lines(texts)`; returns
  None if the sidecar is down (caller falls back to fugashi).
- **Ingest** (`app/subs/service.py`) — batch-tokenizes each episode via the sidecar,
  stores `token_source='migaku-local'` (punctuation dropped to match the denominator);
  falls back to fugashi `'local'` if the sidecar is unavailable.
- **Comprehension** (`app/learn/service.py`) — `comprehension_aligned` now reads
  `migaku-local` tokens, so the score is computed over Migaku-exact dictForms.
- **Backfill** `tools/backfill_migaku_comprehension.py` — re-tokenize + recompute
  existing episodes (`--episode N | --anilist N | --all [--force]`; skips
  `source='exact'`). Validate against a local synthetic fixture before a full run.

**Deployment:** the sidecar can run as a LaunchAgent
`com.mimilab.tokenizer` (:8788), the API was restarted on the new code, and the
backfills can re-tokenize and recompute a configured library while preserving
`source='exact'` episodes. Deployment-specific counts and scores are intentionally omitted.
Re-deploy/refresh with `bash deploy/install-launchd.sh`; re-backfill with
`tools/backfill_migaku_comprehension.py --all`.

## Byte-exact (not shipped — blocked)
`CatalogMgr.calculateComprehensionScoreFromText` would give Migaku's exact formula,
but reading known-status needs `WordListMgr` → Core's SQLite DB, which won't init
offline (gated on `Firebase_waitForAuthCheckDone`). Seeding the synced DB blob into
IndexedDB (`seed.mjs`, `loadCore({seedDb:true})`) is implemented and correct but
isn't reached because of the upstream auth gate. Tokenize+join lands within rounding
anyway, so this was deprioritized.
The dormant native-scorer path requires `MIGAKU_SRS_GZ` to point to a
user-provided database file; it has no repository-local fallback.
