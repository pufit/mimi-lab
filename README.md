# Mimi Lab

A self-hosted platform for **watching anime and learning Japanese**, built around
[Migaku](https://migaku.com)'s own browser player and comprehension engine.

Browse your MyAnimeList library → see how hard each show is *before* you watch →
one-click play in the real Migaku Player → search every word across your subtitle
library and jump back to the exact moment. Acquisition, subtitle matching, and
comprehension scoring all run themselves.

> Single-user, self-hosted. The server runs on a configured host; playback runs
> on any viewing device with the small **Connector** (Chrome + Migaku) — see
> `connector/README.md`.

---

## Features

- **MyAnimeList sync** — two-way OAuth sync of your list, statuses, scores, progress; titles enriched with AniList art.
- **Comprehension preview** — for any title, see the % of words you already know (reuses Migaku's own known-words + scoring). Works **without downloading** — it fetches subtitles from jimaku and scores them, so you can rank your backlog by difficulty.
- **One-click Play in Migaku** — clicking an episode loads + autoplays it in the real Migaku Player with all immersion features live (dictionary, furigana, SRS).
- **nyaa.si acquisition** — search / RSS-follow shows → a headless Transmission daemon downloads them → automatic transcode + organize.
- **jimaku.cc subtitles** — auto-matched by AniList ID, time-aligned, parsed into a searchable corpus.
- **English secondary subtitles** — an optional native-language reference track shown beside the Japanese study line in Migaku (enable "secondary subtitles" in Migaku's settings). Sourced from the release's embedded English softsub → AnimeTosho official track, or machine-translated with Claude (toggle in Settings). English is never tokenized — Japanese stays the study/comprehension language — and its text also fills each Moment's translation.
- **Moments** — search any Japanese word → every line in your library that uses it, each with a screenshot, audio clip, furigana, translation, "play from here", i+1 ("best for mining") sorting, on-demand translation, and Anki export (.apkg with media).
- **The closed watching loop** — playback telemetry from the Connector records progress, auto-marks episodes watched at 90% (→ MAL push), resumes where you left off, and re-syncs your Migaku known-words when a session ends → the backlog re-ranks itself against what you just learned.
- **Study queue** — unknown words ranked by *leverage*: which words move the most 60–80% episodes into your 80%+ sweet spot. Your library as a curriculum.
- **Stats** — known-words growth, comprehension-over-time, and watch activity (daily snapshots).
- **Transcript reader** — any episode's full subtitle script with per-token known-status coloring (pre-read before watching).
- **In-browser fallback player** — Connector offline? Stream the episode with soft subs in the browser; progress still counts.
- **Automation** — a job pipeline runs the whole chain (download → transcode → subtitle fetch → comprehension) hands-off, with an in-app notification feed, a late-subtitle retry sweep, a jobs/System page, aggregated health checks, nightly DB backups, and Migaku-update drift guards (tokenizer baseline + end-to-end self-check).

## How comprehension works (the Migaku reuse)

Migaku is a browser extension; it only attaches to Netflix, YouTube, and its own
Player page. So instead of building a player, we **drive Migaku's own player** over
the Chrome DevTools Protocol, and we **reuse its comprehension engine**:

- **Known words** are read directly from Migaku's IndexedDB `WordList` (the same
  KNOWN/LEARNING/UNKNOWN/IGNORED statuses Migaku uses).
- **Comprehension** is tokenized by **Migaku's own analyzer running headless in Node**
  (a sidecar that loads Migaku's bundled Kotlin/JS engine — no browser) and joined
  against Migaku's known-set, so the score closely tracks Migaku's. If the
  sidecar is down, ingest falls back to a local fugashi tokenizer. An *exact* tier
  also exists: opening an episode lets the Connector scrape Migaku's own published
  number and store it verbatim. See **`tools/migaku-tokenizer/`**.

The only piece that runs on the viewing machine is a small **Connector** (Node):
it holds the external-CDP link to your Chrome+Migaku and dials out to the server,
which streams the media on demand. Everything else is server-side. On Play, the
Connector injects the server's media URL into Migaku's Player — Migaku fetches +
plays + tokenizes.

---

## How downloads get matched to a show

Every finished download is post-processed (`media.postprocess`) and linked to an
AniList title before it lands in the Library. There are two paths:

- **Started from a show / episode page** — the per-episode **Download** ("fast
  download") and **Download season**. These pin the chosen `anilist_id` (+ episode)
  onto the `downloads` row *before* the torrent starts (`acquire.download_episode` /
  `download_batch`), so post-processing **trusts that id** and never re-guesses the
  show from the filename. This is the reliable path — prefer it whenever the title
  is already known.
- **Loose / RSS-followed downloads** with no title pinned — the release filename is
  matched by **`match.match_file()`** (anitopy → AniList → ranked candidates). A
  *confident* top candidate is linked automatically; anything below the confidence
  bar goes to the **manual match queue** with its ranked candidates as suggestions
  (it is never silently linked to a low-confidence guess).

Batch (season-pack) imports place each file against the pack's title; a file that
doesn't fit the season (a stray multi-season/movie file) is re-resolved *by filename*
on its own (`_resolve_title(..., trust_row=False)`), not forced into the pack.

The matcher's only entrypoint is `match_file()`. The post-processor trusts the id
a show-page download pins on the row and calls `match_file` directly for loose
downloads, so a wrong name fails loudly instead of degrading to "unmatched."

---

## How subtitle tracks are validated

Every subtitle track that gets **displayed**, stored as a display track, or
**trusted as a JA-alignment reference** must pass one shared gate —
`subs.service.sub_display_sane()`:

1. **Runtime fit** — the track's *body* (95th-percentile cue onset) may not
   start >60s past the video's runtime (rejects wrong-episode / wrong-cut /
   double-length rips, while tolerating a tiny over-shifted OP/ED/preview tail
   — cues past the video's end can never display anyway).
2. **No excessive self-overlap** — at most 25% of the subtitled time may be covered by 2+
   simultaneous cues. This catches what a range check never can: a track
   containing the same dialogue **twice at conflicting timings** (bad
   double-timed rips, an aligner *folding* an over-long wrong-cut track into
   range, a botched merge, bilingual two-languages-per-line files). The
   documented heuristic tolerates limited benign overlap, including SDH
   stacking and dual-speaker cues, while rejecting sustained conflicting cues.

The gate runs at **every acceptance point** (embedded extraction, embedded
re-blessing, AnimeTosho post-align, MT output, the JA reference-trust decision)
and — because files can be rewritten out-of-band — once more **at serve time**
for the optional English track (mtime-cached; serving no English beats serving
wrong English). AnimeTosho candidates download + align at an isolated `.cand`
path and are promoted onto the served `.en.srt` only after passing, so a bad or
interrupted fetch can never clobber (or pose as) an accepted track. The JA
ingest additionally drops non-Japanese *twins* (bilingual rips time both
language copies identically) and records a warning event if a study corpus
still self-overlaps.

Structure is validated on every acceptance and never assumed from provenance.
`tools/heal_subtitle_tracks.py` sweeps and repairs existing data that predates the
gate.

---

## Running it

### Services (managed by launchd, auto-start on login)

| Service | launchd label | Port | What it does |
|---|---|---|---|
| API + Web UI | `com.mimilab.server` | 8000 | FastAPI backend, serves the SPA, streams media, relays Play |
| Transmission | `com.mimilab.transmission` | 9091 | headless torrent daemon |
| Tokenizer | `com.mimilab.tokenizer` | 8788 | runs Migaku's own analyzer in Node; tokenizes subtitle lines for comprehension (`tools/migaku-tokenizer/`) |

The **Connector** is not a server service — it runs on the machine where you watch
(see `connector/README.md`).

Install / update the launchd agents:
```bash
./deploy/install-launchd.sh      # writes + loads the three agents
./deploy/restart.sh [server|tokenizer|transmission|all]   # restart THROUGH launchd
./deploy/uninstall-launchd.sh    # remove them
```
> When a launchd agent is loaded, use `deploy/restart.sh` instead of starting a
> second manual `uvicorn` process on the same port.

Then open **http://localhost:8000**. The bundled launchd configuration is
loopback-only. Before designing any LAN exposure, require a long random
`MIMI_LAB_TOKEN`, configure the bind address and host/origin allowlists
together, and review the authentication limitations described below; do not
expose the current default deployment to an untrusted network.

### The Connector (on the machine where you watch)
Playback runs on your machine, driven by the Connector. It launches a dedicated
Chrome profile (Migaku Early Access, logged in once) and dials out to the server:
```bash
cd connector && npm install
node index.mjs --server http://<server-ip>:8000 --token <MIMI_LAB_TOKEN>
```
The Server URL + token are shown on the Lab's **Settings → Connector** page. For
a remote server, use the deployment's configured TLS endpoint. See
`connector/README.md`.

### Manual run (instead of launchd)
```bash
./run.sh        # starts the API/UI on :8000
# Transmission daemon, if not under launchd:
transmission-daemon -f -g data/transmission -w data/inbox -T -p 9091 --rpc-bind-address 127.0.0.1
```

### First-time setup
```bash
cp .env.example .env          # then fill in JIMAKU_TOKEN + MAL_CLIENT_ID/SECRET
uv venv --python 3.13 .venv && uv pip install -e .   # python deps
.venv/bin/python -m app.learn.data.build_data        # JMdict glosses
.venv/bin/python tools/load_freq.py                  # frequency list
(cd tools/migaku-tokenizer && npm ci)                # tokenizer sidecar
cd web && pnpm install && pnpm build && cd ..        # build the SPA
```
- **MyAnimeList:** open `http://localhost:8000/api/mal/auth` once and approve, then `POST /api/mal/sync`.
- **Known words:** `POST /api/known/sync` asks the Connector to push your Migaku WordList (needs the Connector connected).

---

## Project layout
```
app/            FastAPI backend (Python 3.13)
  config.py       settings (.env)
  db.py           SQLite schema + access (WAL + FTS5)
  models.py       pydantic API models
  main.py         app entrypoint + SPA serving
  security.py     signed media tokens + shared-secret auth (Connector + uploads)
  catalog/  match/  subs/  learn/  known/  acquire/  media/  mal/  analyze/  watch/  connector/  events/  jobs/
connector/      Node app on the VIEWING machine: CDP→Migaku inject, WS to server, known-words + comprehension push
web/            React + Vite + Tailwind + shadcn SPA (built → web/dist, served by FastAPI)
harness/        CDP probe scripts (the proven Migaku integration; remote-inject-test.mjs seeds the Connector)
deploy/         launchd install/uninstall scripts
tools/          frequency loader; migaku-tokenizer/ (Migaku's analyzer in Node + sidecar); backfill_migaku_comprehension.py
lib/TestShow/   sample clip + JP subs (test fixture)
data/           runtime: SQLite DB, inbox, clips, transmission config, logs (gitignored)
```

## The modules (API under `/api/<module>`)
| Module | Responsibility |
|---|---|
| `catalog` | titles/episodes, library scan, AniList enrichment |
| `match` | filename → AniList (anitopy + ranked candidates + manual queue), Fribb ID map |
| `subs` | jimaku fetch → align (alass/ffsubsync) → ingest → tokenized corpus; English secondary track (embedded softsub / AnimeTosho / Claude MT) + per-line translation merge; display-sanity gate (`sub_display_sane`) on every accepted/served/reference track |
| `learn` | tokenize (**Migaku sidecar** → Migaku-exact tokens, fugashi/UniDic fallback), comprehension (aligned + exact), Moments search, clip extraction |
| `known` | receive Migaku's WordList pushed by the Connector |
| `acquire` | nyaa search/RSS, Transmission client, downloads + follows |
| `media` | ffprobe → remux/transcode (VideoToolbox) → organize; range-capable media streaming for the Connector |
| `mal` | MyAnimeList OAuth (PKCE) + pull/push sync |
| `analyze` | comprehension-without-download (fetch subs by AniList ID + score) |
| `watch` | relay one-click Play to the Connector (resolves media URLs + signed tokens) |
| `connector` | WebSocket relay to the user-side Connector + status/setup |
| `events` | in-app notification feed (pipeline reports here) |
| `jobs` | DB-backed job queue + APScheduler periodics |

## Tech stack
Python 3.13 · FastAPI · SQLite (WAL + FTS5) · fugashi + UniDic · jmdict-simplified ·
ffsubsync · ffmpeg/VideoToolbox · Transmission · React 19 + Vite + Tailwind + shadcn ·
Node + playwright-core (Connector).

## Testing
Most module selftests write to the configured database and filesystem paths.
Point `MIMI_LAB_DB`, `LIBRARY_DIR`, `INBOX_DIR`, and `CLIPS_DIR` at a
temporary directory before running them; never run the full module suite
against a live library.

```bash
.venv/bin/python -m app.connector.selftest   # WS relay + range media + uploads (scratch DB, no Chrome)
.venv/bin/python integration_test.py          # read-only smoke against the RUNNING server
.venv/bin/python -m app.match.selftest        # per-module selftests
.venv/bin/python -m app.learn.selftest
cd connector && node selftest.mjs             # Connector config checks
curl -X POST localhost:8000/api/connector/selfcheck   # full Migaku smoke (Connector must be up)
```

Optional live service sections are explicit opt-ins and skip when unset:

- AniList/Fribb: `MIMI_TEST_ANILIST_FILENAME`, `MIMI_TEST_ANILIST_ID`, `MIMI_TEST_MAL_ID`
- Jimaku: `MIMI_TEST_JIMAKU_ANILIST_ID`, `MIMI_TEST_JIMAKU_EPISODE`, and optional `MIMI_TEST_JIMAKU_PREFER`
- Nyaa search: `MIMI_TEST_NYAA_QUERY`
- Nyaa batch: `MIMI_TEST_NYAA_BATCH_ANILIST_ID`, `MIMI_TEST_NYAA_BATCH_TITLE`, `MIMI_TEST_NYAA_BATCH_ALT_TITLE`, `MIMI_TEST_NYAA_BATCH_EPISODES`

## Docs
- **`connector/README.md`** — installing + running the Connector on your viewing device.
- **`tools/migaku-tokenizer/README.md`** — how Migaku's analyzer runs headless in Node, the sidecar, the pipeline integration, and the backfill.

## Notes / attribution
- Dictionary glosses are **JMdict** (EDRDG, CC BY-SA 4.0) via `scriptin/jmdict-simplified`.
- Frequency data is the JPDB list via `Kuuuube/yomitan-dictionaries`.
- ID mapping is `Fribb/anime-lists`.
- Repository license: **not yet selected**. Resolve the root license, legacy
  subpackage ISC metadata, and third-party data terms before publishing.
