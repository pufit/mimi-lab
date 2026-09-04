"""SQLite access layer + schema. Single-file DB, WAL mode, FTS5 corpus.

Usage:
    from app.db import connect, init_db
    with connect() as cx:
        rows = cx.execute("select * from titles").fetchall()  # sqlite3.Row
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Iterator

from .config import settings

SCHEMA = r"""
CREATE TABLE IF NOT EXISTS titles (
  anilist_id        INTEGER PRIMARY KEY,
  mal_id            INTEGER,
  tvdb_id           INTEGER,
  kitsu_id          INTEGER,
  romaji            TEXT,
  english           TEXT,
  native            TEXT,
  format            TEXT,
  total_episodes    INTEGER,
  aired_episodes    INTEGER,
  episode_offset    INTEGER,
  season            TEXT,
  year              INTEGER,
  cover_url         TEXT,
  banner_url        TEXT,
  description       TEXT,
  status            TEXT,
  mal_status        TEXT,
  mal_score         INTEGER,
  mal_progress      INTEGER,
  mal_updated_at    TEXT,
  local_updated_at  TEXT,
  created_at        TEXT DEFAULT (datetime('now')),
  updated_at        TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS episodes (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  anilist_id        INTEGER NOT NULL REFERENCES titles(anilist_id) ON DELETE CASCADE,
  ep_number         INTEGER NOT NULL,
  absolute_number   INTEGER,
  title             TEXT,
  video_path        TEXT,
  codec             TEXT,
  container         TEXT,
  duration_ms       INTEGER,
  watched           INTEGER DEFAULT 0,
  watch_progress_ms INTEGER DEFAULT 0,
  comprehension_pct REAL,
  comprehension_rating TEXT,
  comprehension_source TEXT,
  new_word_count    INTEGER,
  created_at        TEXT DEFAULT (datetime('now')),
  updated_at        TEXT DEFAULT (datetime('now')),
  UNIQUE(anilist_id, ep_number)
);

CREATE TABLE IF NOT EXISTS downloads (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  nyaa_id           TEXT,
  title_guess       TEXT,
  magnet            TEXT,
  torrent_url       TEXT,
  qbt_hash          TEXT,
  state             TEXT DEFAULT 'queued',
  save_path         TEXT,
  linked_episode_id INTEGER REFERENCES episodes(id) ON DELETE SET NULL,
  anilist_id        INTEGER,
  ep_number         INTEGER,
  release_group     TEXT,
  resolution        TEXT,
  size_bytes        INTEGER,
  progress          REAL DEFAULT 0,
  kind              TEXT DEFAULT 'single',   -- 'single' (one episode) | 'batch' (season pack)
  season            INTEGER,                 -- season ordinal a batch fills (default 1)
  total_files       INTEGER,                 -- episodes a batch is expected to yield
  done_files        INTEGER DEFAULT 0,       -- episodes a batch has post-processed so far
  wanted_eps        TEXT,                    -- JSON list of selected episode numbers (NULL = all)
  files_selected    INTEGER DEFAULT 0,       -- 1 once a batch's file selection is applied to Transmission
  created_at        TEXT DEFAULT (datetime('now')),
  updated_at        TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS subtitles (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  episode_id        INTEGER NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
  source            TEXT DEFAULT 'jimaku',
  jimaku_entry_id   INTEGER,
  jimaku_file_id    INTEGER,
  jimaku_filename   TEXT,
  lang              TEXT DEFAULT 'ja',
  path              TEXT,
  format            TEXT,
  aligned           INTEGER DEFAULT 0,
  align_tool        TEXT,
  version           INTEGER DEFAULT 1,
  created_at        TEXT DEFAULT (datetime('now')),
  UNIQUE(episode_id, source, jimaku_file_id)
);

CREATE TABLE IF NOT EXISTS subtitle_lines (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  subtitle_id       INTEGER NOT NULL REFERENCES subtitles(id) ON DELETE CASCADE,
  episode_id        INTEGER NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
  idx               INTEGER NOT NULL,
  start_ms          INTEGER NOT NULL,
  end_ms            INTEGER NOT NULL,
  text              TEXT NOT NULL,
  text_furigana     TEXT,
  translation       TEXT
);
CREATE INDEX IF NOT EXISTS idx_lines_episode ON subtitle_lines(episode_id);
CREATE INDEX IF NOT EXISTS idx_lines_subtitle ON subtitle_lines(subtitle_id);

CREATE TABLE IF NOT EXISTS line_lemmas (
  line_id           INTEGER NOT NULL REFERENCES subtitle_lines(id) ON DELETE CASCADE,
  episode_id        INTEGER NOT NULL,
  lemma             TEXT NOT NULL,
  reading           TEXT,
  pos               TEXT,
  surface           TEXT,
  token_source      TEXT DEFAULT 'local'
);
CREATE INDEX IF NOT EXISTS idx_lemma ON line_lemmas(lemma);
CREATE INDEX IF NOT EXISTS idx_lemma_line ON line_lemmas(line_id);
CREATE INDEX IF NOT EXISTS idx_lemma_episode ON line_lemmas(episode_id);

CREATE VIRTUAL TABLE IF NOT EXISTS subtitle_fts USING fts5(
  text, line_id UNINDEXED, episode_id UNINDEXED, tokenize='trigram'
);

CREATE TABLE IF NOT EXISTS lemma_freq (
  lemma             TEXT PRIMARY KEY,
  rank              INTEGER
);

CREATE TABLE IF NOT EXISTS known_words (
  dict_form         TEXT NOT NULL,
  reading           TEXT NOT NULL DEFAULT '',
  status            TEXT NOT NULL,
  source            TEXT DEFAULT 'migaku',
  updated_at        TEXT DEFAULT (datetime('now')),
  PRIMARY KEY (dict_form, reading)
);
CREATE INDEX IF NOT EXISTS idx_known_status ON known_words(status);
CREATE INDEX IF NOT EXISTS idx_known_form ON known_words(dict_form);

-- Time series of the known-words set (one row per meaningful change) so we can
-- show vocabulary GROWTH ("+47 known this week") and re-rank the backlog as the
-- user learns. Migaku's WordList is otherwise a destructive snapshot.
CREATE TABLE IF NOT EXISTS known_snapshots (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  captured_at       TEXT DEFAULT (datetime('now')),
  known             INTEGER DEFAULT 0,
  learning          INTEGER DEFAULT 0,
  unknown           INTEGER DEFAULT 0,
  ignored           INTEGER DEFAULT 0,
  total             INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_known_snap_time ON known_snapshots(captured_at);

CREATE TABLE IF NOT EXISTS match_queue (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  filename          TEXT NOT NULL,
  parsed_json       TEXT,
  candidates_json   TEXT,
  chosen_anilist_id INTEGER,
  ep_number         INTEGER,
  state             TEXT DEFAULT 'pending',
  download_id       INTEGER REFERENCES downloads(id) ON DELETE SET NULL,
  created_at        TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS rss_follows (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  anilist_id        INTEGER,
  title             TEXT,
  query             TEXT NOT NULL,
  category          TEXT DEFAULT '1_2',
  trusted_only      INTEGER DEFAULT 1,
  resolution        TEXT DEFAULT '1080p',
  enabled           INTEGER DEFAULT 1,
  last_checked      TEXT,
  seen_json         TEXT DEFAULT '[]',
  created_at        TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS jobs (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  type              TEXT NOT NULL,
  payload_json      TEXT,
  state             TEXT DEFAULT 'queued',
  priority          INTEGER DEFAULT 100,   -- lower = runs first (interactive < background)
  attempts          INTEGER DEFAULT 0,
  run_after         TEXT DEFAULT (datetime('now')),
  last_error        TEXT,
  created_at        TEXT DEFAULT (datetime('now')),
  updated_at        TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state, run_after);
CREATE INDEX IF NOT EXISTS idx_jobs_dedup ON jobs(type, state);
-- NOTE: idx_jobs_ready references the `priority` column, which is added by
-- _migrate() for pre-existing DBs — so it is created in init_db() AFTER the
-- migration runs, not here (this script executes before _migrate).

CREATE TABLE IF NOT EXISTS kv (
  key               TEXT PRIMARY KEY,
  value             TEXT,
  updated_at        TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS events (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  kind              TEXT NOT NULL DEFAULT 'info',   -- info|success|warning|error
  category          TEXT,                           -- download|subtitle|match|comprehension|mal|system
  title             TEXT NOT NULL,
  detail            TEXT,
  meta_json         TEXT,
  read              INTEGER DEFAULT 0,
  created_at        TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_events_read ON events(read, created_at DESC);

-- Library-wide comprehension over time (the Stats dashboard). One row per
-- capture (daily periodic + after each known-words-triggered recompute);
-- episodes.comprehension_pct is overwritten in place, so history lives here.
CREATE TABLE IF NOT EXISTS comprehension_history (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  captured_at       TEXT DEFAULT (datetime('now')),
  scored_episodes   INTEGER DEFAULT 0,
  avg_pct           REAL,
  sweet_count       INTEGER DEFAULT 0,   -- 80..95
  almost_count      INTEGER DEFAULT 0,   -- 65..80
  easy_count        INTEGER DEFAULT 0,   -- >=95
  hard_count        INTEGER DEFAULT 0    -- <65
);
CREATE INDEX IF NOT EXISTS idx_compr_hist_time ON comprehension_history(captured_at);

-- Every completed viewing = one row. episodes.watched is a boolean and cannot
-- count re-watches (re-marking used to just move watched_at, rewriting the
-- stats timeline). This is the immersion log: first watches AND rewatches,
-- from the manual toggle, Connector telemetry, or the in-browser player.
CREATE TABLE IF NOT EXISTS watch_history (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  episode_id        INTEGER NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
  watched_at        TEXT DEFAULT (datetime('now')),
  source            TEXT DEFAULT 'manual',   -- manual | connector | browser
  is_rewatch        INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_watch_hist_time ON watch_history(watched_at);
CREATE INDEX IF NOT EXISTS idx_watch_hist_ep ON watch_history(episode_id);

-- ---------------------------------------------------------------------------
-- SRS (app/srs). Word knowledge state lives HERE, never in known_words:
-- known_words is a FULL-REPLACE mirror of Migaku's WordList
-- (known/service.py::upload_known_words) — rows absent from a push are deleted.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS srs_cards (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  lemma               TEXT NOT NULL,                   -- Migaku dictForm == line_lemmas.lemma == known_words.dict_form; up to MAX_CARDS_PER_LEMMA sibling cards per word, each from a different anime (2026-09-03; was UNIQUE — `_rebuild_srs_cards_multi` migrates)
  reading             TEXT,                            -- hiragana
  pos                 TEXT,                            -- noun|verb|adjective|adverb|expression|other (judge/curation), or UniDic pos1
  gloss               TEXT,                            -- JMdict first gloss line (learn._glosses_for)
  meaning_short       TEXT,                            -- sense USED in this moment, <=6 English words (judge/curated), editable
  meaning_full        TEXT,                            -- fuller dictionary-style meaning, editable
  why_clear           TEXT,                            -- one sentence naming the cues that reveal the meaning
  usage_note          TEXT,                            -- nuance / register / collocation (may be '')
  tags_json           TEXT,                            -- JSON list of free tags
  freq_rank           INTEGER,                         -- lemma_freq.rank (JPDB)
  source              TEXT NOT NULL DEFAULT 'auto',    -- auto | manual | curated-initial | confirm
  score               REAL,                            -- generation word score (§5.3) or curated stack_score
  clarity             REAL,                            -- judge clarity 0..1 of the primary moment; NULL = unjudged (manual)
  usefulness          REAL,                            -- judge usefulness 0..1
  priority            INTEGER,                         -- 1..5 (5 = learn first)
  -- primary moment: line_id is a SOFT reference; the snapshot columns make the card self-contained
  line_id             INTEGER REFERENCES subtitle_lines(id) ON DELETE SET NULL,
  episode_id          INTEGER,                         -- no FK on purpose (survives delete_title)
  anilist_id          INTEGER,
  show_title          TEXT,                            -- titles.romaji or english at creation
  ep_number           INTEGER,
  start_ms            INTEGER NOT NULL DEFAULT 0,      -- subtitle line timing (not the clip window)
  end_ms              INTEGER NOT NULL DEFAULT 0,
  text                TEXT NOT NULL DEFAULT '',
  norm_text           TEXT NOT NULL DEFAULT '',        -- census.norm_text(text): spaces/punctuation stripped; durable moment identity with episode_id (+start_ms tie-break)
  text_furigana       TEXT,                            -- server-escaped <ruby> html (subtitle_lines.text_furigana)
  translation         TEXT,
  translation_source  TEXT,                            -- human (subtitle track) | mt (Haiku translate_line of this line) | NULL (none)
  target_surface      TEXT NOT NULL DEFAULT '',        -- exact substring of text where the word appears (救って for 救う)
  tokens_json         TEXT,                            -- ordered tokens, §2.4
  context_json        TEXT,                            -- ±2 surrounding DIALOGUE lines snapshot, §2.4
  extend_json         TEXT,                            -- JSON [{line_id, idx, start_ms, end_ms, text, role}] every EXTRA line the moment includes, chronologically: role='evidence' (neighbours the meaning depends on, before AND after; shown on the card front) or role='continuation' (the cue the sentence runs into). The clip window covers them (§6.2). Legacy rows have no role/idx = continuation. Survives re-ingest (timings).
  alt_moment_ids_json TEXT,                            -- JSON [srs_moments.id,...] accepted alternate moments (best first); srs_moments ids are stable (soft line_id)
  -- clip files live in <clips_dir>/srs/<id>/ (§6)
  clip_status         TEXT NOT NULL DEFAULT 'pending', -- pending | ready | failed | no_source | missing
  clip_start_ms       INTEGER,
  clip_end_ms         INTEGER,
  clip_bytes          INTEGER,
  clip_error          TEXT,
  clip_version        INTEGER NOT NULL DEFAULT 1,      -- bumped on every swap/regenerate; srs_clip payload carries it (§6.6)
  clip_requested_at   TEXT,                            -- UTC when clip_status was last set to 'pending' (stale-pending re-enqueue)
  -- stack (meaningful only while state='new')
  state               TEXT NOT NULL DEFAULT 'new',     -- new|learning|review|relearning|known|suspended|rejected
  queue_pos           INTEGER,                         -- dense 1..N across state='new'; NULL otherwise
  study_now           INTEGER NOT NULL DEFAULT 0,      -- 1 = serve as the next new card regardless of new_per_day ("Study next")
  buried_until        TEXT,                            -- UTC 'YYYY-MM-DD HH:MM:SS'; hidden from queues until then
  -- scheduler (FSRS-6, §3)
  stability           REAL,
  difficulty          REAL,
  step                INTEGER,                         -- learning/relearning step index; NULL in review/known/suspended/new
  due_at              TEXT,                            -- UTC 'YYYY-MM-DD HH:MM:SS' (review cards floored to the day cutoff)
  last_review_at      TEXT,                            -- NULL while state='new' (reset by demotion/forget/unknown; history stays in srs_reviews)
  last_fail_day       TEXT,                            -- SRS day 'YYYY-MM-DD' of the last COUNTED failed repeat
  scheduled_days      INTEGER NOT NULL DEFAULT 0,      -- current interval in days (0 while in steps)
  reps                INTEGER NOT NULL DEFAULT 0,
  lapses              INTEGER NOT NULL DEFAULT 0,
  fail_count          INTEGER NOT NULL DEFAULT 0,      -- failed repeats since last reset (auto-demote counter)
  demoted_count       INTEGER NOT NULL DEFAULT 0,
  demoted_at          TEXT,
  introduced_at       TEXT,                            -- first rating of the CURRENT introduction (UTC); NULL while state='new'
  state_before_suspend TEXT,                           -- learning|relearning|review|new: where Resume returns the card (NULL otherwise)
  known_source        TEXT,                            -- srs | user | migaku   (when state='known')
  known_at            TEXT,
  suspend_reason      TEXT,                            -- user | auto_demotions | migaku_ignored
  migaku_status_seen  TEXT,                            -- known_words.status the last time reconcile ACTED on this card (edge-triggered reconcile, §5.10)
  notes               TEXT,
  created_at          TEXT DEFAULT (datetime('now')),
  updated_at          TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_srs_cards_state_due ON srs_cards(state, due_at);
CREATE INDEX IF NOT EXISTS idx_srs_cards_stack     ON srs_cards(state, queue_pos);
CREATE INDEX IF NOT EXISTS idx_srs_cards_line      ON srs_cards(line_id);        -- makes ON DELETE SET NULL cheap
CREATE INDEX IF NOT EXISTS idx_srs_cards_episode   ON srs_cards(episode_id);     -- relink after re-ingest (§2.7)
CREATE INDEX IF NOT EXISTS idx_srs_cards_lemma     ON srs_cards(lemma);          -- sibling lookups (multi-card words)
CREATE UNIQUE INDEX IF NOT EXISTS idx_srs_cards_lemma_moment ON srs_cards(lemma, episode_id, norm_text); -- one card per moment (create_card ON CONFLICT)

-- One row per rating. before_json = scheduler/stack snapshot for undo (§3.11). client_id makes POST /review idempotent.
CREATE TABLE IF NOT EXISTS srs_reviews (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  card_id             INTEGER NOT NULL REFERENCES srs_cards(id) ON DELETE CASCADE,
  client_id           TEXT UNIQUE,                     -- browser-generated uuid; NULL for server-side ratings
  reviewed_at         TEXT NOT NULL DEFAULT (datetime('now')),
  review_day          TEXT NOT NULL,                   -- SRS day (local, cutoff 04:00), 'YYYY-MM-DD'
  rating              INTEGER NOT NULL,                -- 1 Again | 2 Hard | 3 Good | 4 Easy
  state_before        TEXT NOT NULL,
  state_after         TEXT NOT NULL,
  scheduled_days      INTEGER NOT NULL DEFAULT 0,      -- interval granted by this review (0 in steps)
  elapsed_ms          INTEGER,                         -- time the card was on screen (client-measured)
  counted_fail        INTEGER NOT NULL DEFAULT 0,      -- 1 if this Again counted as a failed repeat
  demoted             INTEGER NOT NULL DEFAULT 0,      -- 1 if this review triggered auto-demotion
  undone              INTEGER NOT NULL DEFAULT 0,
  before_json         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_srs_reviews_card ON srs_reviews(card_id, id);
CREATE INDEX IF NOT EXISTS idx_srs_reviews_day  ON srs_reviews(review_day);

-- Census + word-level verdict cache. Rebuildable features; judge_* and user_flag persist across
-- census runs so a rejected word is never re-proposed and "skip"/"confirm known" stick.
-- NOT a knowledge-state table (no status column): knowledge is derived from srs_cards only.
CREATE TABLE IF NOT EXISTS srs_words (
  lemma               TEXT PRIMARY KEY,
  reading             TEXT,
  gloss               TEXT,
  pos1                TEXT,                            -- UniDic pos1 of the lemma's head token (fugashi)
  pos2                TEXT,
  freq_rank           INTEGER,
  occ                 INTEGER NOT NULL DEFAULT 0,      -- unknown-token occurrences in downloaded episodes
  eps                 INTEGER NOT NULL DEFAULT 0,      -- distinct downloaded episodes
  titles              INTEGER NOT NULL DEFAULT 0,
  title_concentration REAL,                            -- max occ in one title / occ (name detector, §5.2 F7)
  iplus1_lines        INTEGER NOT NULL DEFAULT 0,      -- lines with 0 other unknowns
  moment_lines        INTEGER NOT NULL DEFAULT 0,      -- lines with <=2 other unknowns (the widened pool)
  leverage_crossings  INTEGER NOT NULL DEFAULT 0,
  next_watch_hits     INTEGER NOT NULL DEFAULT 0,      -- occurrences in the top-3 sweet_spot() episodes
  migaku_status       TEXT,                            -- known_words.status snapshot (LEARNING bonus)
  in_unwatched        INTEGER NOT NULL DEFAULT 0,
  standalone_ratio    REAL,                            -- single-kanji forms: share of occurrences not glued to another kanji (§5.2 F2)
  canonical_of        TEXT,                            -- set on a kana variant folded into its kanji form (§5.5 variant merge); never proposed itself
  score               REAL,
  best_moment_ids_json TEXT,                           -- JSON [srs_moments.id,...] top-12 moments by heuristic (§5.4)
  census_at           TEXT,
  judge_status        TEXT NOT NULL DEFAULT 'unjudged',-- unjudged | filtered | pending | accepted | rejected | probably_known | error
                                                       --   filtered = dropped by a census heuristic (re-evaluated at every census); rejected = judge or user verdict (final)
  judge_reason        TEXT,                            -- judge: fragment|fixed_expression|function_word|name|interjection|tokenizer_error|too_basic|no_clear_moment|odd_register|other|duplicate_of:<lemma>|user_skip
                                                       -- filtered: name|function_word|fragment|kana_no_gloss|rare|lyrics_only|no_moment|variant_of:<lemma>
  judge_note          TEXT,
  judged_at           TEXT,
  judged_moment_lines INTEGER,                         -- moment_lines at judge time (re-judge no_clear_moment when it grows by >=3)
  judge_attempts      INTEGER NOT NULL DEFAULT 0,
  user_flag           TEXT,                            -- NULL | skip | unskip | confirm_known
  card_id             INTEGER,                         -- srs_cards.id when a card exists (soft)
  updated_at          TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_srs_words_judge ON srs_words(judge_status, score DESC);

-- Judge cache + alternates + pending work + user blocklist: every (word, moment) the generator
-- has scored/judged. Identity is CONTENT, not rowid: subtitle re-ingest deletes and re-inserts
-- every line of an episode with new ids, so line_id is a SOFT reference re-attached by relink
-- (§2.7). Paid verdicts, alternates and user_rejected markers therefore survive re-alignment.
CREATE TABLE IF NOT EXISTS srs_moments (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  lemma               TEXT NOT NULL,
  line_id             INTEGER REFERENCES subtitle_lines(id) ON DELETE SET NULL,
  episode_id          INTEGER NOT NULL,                -- no FK (survives delete_title; rows of a deleted episode are pruned by the audit)
  idx                 INTEGER,
  start_ms            INTEGER NOT NULL,
  end_ms              INTEGER NOT NULL,
  norm_text           TEXT NOT NULL,                   -- census.norm_text(text)
  text                TEXT NOT NULL,
  translation         TEXT,                            -- as seen at judge time
  translation_source  TEXT,                            -- human | mt | NULL
  translation_shared  INTEGER NOT NULL DEFAULT 0,      -- 1 = identical to a neighbouring cue's translation (time-aligned EN track, §5.4)
  target_surface      TEXT,
  other_unknowns      INTEGER,                         -- distinct OTHER unknown content lemmas in the line at scoring time
  line_score          REAL,                            -- heuristic (§5.4)
  run_id              INTEGER,                         -- srs_generation_runs.id that planned it
  batch_no            INTEGER,
  judged_at           TEXT,                            -- NULL = pending
  judge_model         TEXT,
  clarity             REAL,                            -- 0..1 (clamped after parsing, §5.5)
  translation_renders_word INTEGER,                    -- 0/1
  clean_utterance     INTEGER,                         -- 0/1 (single complete cue, not rolled/lyrics/merged)
  accepted            INTEGER,                         -- 0/1 (§5.5 acceptance)
  verdict             TEXT,                            -- accept | reject | user | user_rejected
  note                TEXT,
  UNIQUE(lemma, episode_id, norm_text)                 -- idempotent planning; a text repeated inside one episode is one moment (repeats are down-weighted anyway)
);
CREATE INDEX IF NOT EXISTS idx_srs_moments_lemma ON srs_moments(lemma, clarity DESC);
CREATE INDEX IF NOT EXISTS idx_srs_moments_run   ON srs_moments(run_id, batch_no, judged_at);
CREATE INDEX IF NOT EXISTS idx_srs_moments_line  ON srs_moments(line_id);            -- makes ON DELETE SET NULL cheap (verified: without it every deleted line SCANs srs_moments)
CREATE INDEX IF NOT EXISTS idx_srs_moments_ep    ON srs_moments(episode_id, norm_text); -- relink

CREATE TABLE IF NOT EXISTS srs_generation_runs (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  trigger             TEXT NOT NULL,                   -- manual|auto|topup|known_sync|ingest|word|import
  state               TEXT NOT NULL DEFAULT 'planning',-- planning|judging|done|error|nothing_to_do
  want                INTEGER,                         -- cards requested
  words_scored        INTEGER DEFAULT 0,
  words_planned       INTEGER DEFAULT 0,
  batches_planned     INTEGER DEFAULT 0,
  batches_done        INTEGER DEFAULT 0,
  moments_judged      INTEGER DEFAULT 0,
  accepted            INTEGER DEFAULT 0,
  rejected            INTEGER DEFAULT 0,
  cards_created       INTEGER DEFAULT 0,
  llm_calls           INTEGER DEFAULT 0,
  llm_in_tokens       INTEGER DEFAULT 0,               -- delta of kv llm.usage.<model>.in around each call (approximate under concurrency)
  llm_out_tokens      INTEGER DEFAULT 0,
  corpus_mark         TEXT,                            -- JSON corpus mark at plan time (§5.9)
  error               TEXT,
  started_at          TEXT DEFAULT (datetime('now')),
  finished_at         TEXT,
  heartbeat_at        TEXT                             -- updated by every srs_judge_batch of the run (stale-run sweep, §5.8)
);
"""


def connect() -> sqlite3.Connection:
    settings.mimi_lab_db.parent.mkdir(parents=True, exist_ok=True)
    cx = sqlite3.connect(str(settings.mimi_lab_db), check_same_thread=False)
    cx.row_factory = sqlite3.Row
    cx.execute("PRAGMA journal_mode=WAL")
    cx.execute("PRAGMA foreign_keys=ON")
    # 5 s was enough when the only concurrent writers were the API and one job
    # worker. The SRS adds several (3 job workers cutting clips, the review/stack
    # mutations, the curated import): at 5 s a burst produced "database is locked"
    # in the worker claim/finish UPDATEs, which strands jobs in 'running'. SQLite
    # queues writers, so a long timeout costs nothing when there is no contention.
    cx.execute("PRAGMA busy_timeout=30000")
    return cx


@contextmanager
def cursor() -> Iterator[sqlite3.Connection]:
    cx = connect()
    try:
        yield cx
        cx.commit()
    finally:
        cx.close()


# Additive column migrations for already-created DBs (CREATE TABLE IF NOT EXISTS
# never alters an existing table). Each entry: (table, column, DDL type+default).
# Applied idempotently — a column already present is skipped.
_MIGRATIONS: list[tuple[str, str, str]] = [
    ("downloads", "kind", "TEXT DEFAULT 'single'"),
    ("downloads", "season", "INTEGER"),
    ("downloads", "total_files", "INTEGER"),
    ("downloads", "done_files", "INTEGER DEFAULT 0"),
    ("downloads", "wanted_eps", "TEXT"),
    ("downloads", "files_selected", "INTEGER DEFAULT 0"),
    # objective alignment confidence (0..1) from the self-verifying aligner, plus
    # the reference kind it was scored against ('en'|'vad'|None) so a later pass
    # knows whether a better reference has since become available.
    ("subtitles", "align_score", "REAL"),
    ("subtitles", "align_ref", "TEXT"),
    # the staged, already-transcoded .mp4 for an unmatched download (in
    # inbox/processed/). Confirming a manual match moves THIS file into the
    # Library — without it, confirm couldn't find the file (it's renamed to .mp4
    # and lives outside library_dir) and orphaned every staged import.
    ("match_queue", "processed_path", "TEXT"),
    # job priority lane (lower = runs first); interactive work jumps transcodes.
    ("jobs", "priority", "INTEGER DEFAULT 100"),
    # when an episode was marked watched (manual toggle or playback telemetry) —
    # drives the Stats page's watched-per-day series.
    ("episodes", "watched_at", "TEXT"),
    # how many episodes of this season have actually AIRED (AniList
    # nextAiringEpisode.episode - 1; == total_episodes once FINISHED). This is
    # the ceiling for a legitimately season-relative episode number: a file
    # numbered above it cannot be relative, so it must be absolute
    # (cross-season) numbering. Unlike total_episodes it is known even for an
    # ongoing show whose final length AniList doesn't publish yet.
    ("titles", "aired_episodes", "INTEGER"),
    # absolute -> relative episode offset for a sequel (episodes aired before
    # this season). Learned from the release/subtitle numbering itself and
    # validated against aired_episodes; see match.infer_episode_offset.
    ("titles", "episode_offset", "INTEGER"),
    # SRS evidence lines (SRS_DESIGN §5.7/§6.2 "Evidence lines"): the neighbouring
    # subtitle line ids the meaning of the target word depends on, as returned by
    # the judge / the curation file. JSON list of ints (NULL = never judged, [] =
    # the sentence is self-sufficient). Carried into srs_cards.extend_json when a
    # card is created or swapped onto this moment.
    ("srs_moments", "evidence_line_ids_json", "TEXT"),
    # tokens spent by the translation-blind clarity gate (Opus 5) per generation
    # run, separate from the writer's so the run's cost line prices both models.
    ("srs_generation_runs", "blind_in_tokens", "INTEGER DEFAULT 0"),
    ("srs_generation_runs", "blind_out_tokens", "INTEGER DEFAULT 0"),
]


def _migrate(cx: sqlite3.Connection) -> None:
    """Apply additive column migrations to an existing DB. Idempotent; never
    drops or rewrites. Safe to run on every startup."""
    for table, column, ddl in _MIGRATIONS:
        cols = {r["name"] for r in cx.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            cx.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def _rebuild_srs_cards_multi(cx: sqlite3.Connection) -> bool:
    """One-time table rebuild: drop the `lemma UNIQUE` constraint of `srs_cards`
    (multi-card words, 2026-09-03). SQLite cannot drop a column constraint in
    place, so this follows the documented 12-step procedure: new table from the
    current SCHEMA → copy every common column → drop old → rename → recreate
    the srs_cards indexes → `foreign_key_check`, all inside one transaction with
    foreign keys OFF (srs_reviews/srs_words reference srs_cards ON DELETE
    CASCADE — with FKs on, the DROP would wipe the review history). Idempotent:
    returns False when the table already lacks the UNIQUE constraint.
    """
    import re as _re

    row = cx.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='srs_cards'"
    ).fetchone()
    if row is None or not _re.search(r"lemma\s+TEXT\s+NOT\s+NULL\s+UNIQUE", row["sql"]):
        return False
    m = _re.search(r"CREATE TABLE IF NOT EXISTS srs_cards \((.*?)\n\);", SCHEMA, _re.S)
    if not m:
        raise RuntimeError("srs_cards DDL not found in SCHEMA")
    body = m.group(1)
    index_stmts = _re.findall(
        r"CREATE (?:UNIQUE )?INDEX IF NOT EXISTS \w+\s+ON srs_cards\([^)]*\);", SCHEMA)

    cx.commit()                                  # PRAGMA foreign_keys is a no-op inside a txn
    old_iso = cx.isolation_level
    cx.isolation_level = None                    # manual transaction control
    try:
        cx.execute("PRAGMA foreign_keys=OFF")
        cx.execute("BEGIN IMMEDIATE")
        try:
            cx.execute(f"CREATE TABLE srs_cards__new ({body}\n)")
            old_cols = [r["name"] for r in cx.execute("PRAGMA table_info(srs_cards)")]
            new_cols = {r["name"] for r in cx.execute("PRAGMA table_info(srs_cards__new)")}
            common = ", ".join(c for c in old_cols if c in new_cols)
            n_before = cx.execute("SELECT COUNT(*) FROM srs_cards").fetchone()[0]
            cx.execute(f"INSERT INTO srs_cards__new ({common}) SELECT {common} FROM srs_cards")
            cx.execute("DROP TABLE srs_cards")
            cx.execute("ALTER TABLE srs_cards__new RENAME TO srs_cards")
            for stmt in index_stmts:
                cx.execute(stmt)
            n_after = cx.execute("SELECT COUNT(*) FROM srs_cards").fetchone()[0]
            if n_after != n_before:
                raise RuntimeError(f"srs_cards rebuild lost rows: {n_before} → {n_after}")
            bad = cx.execute("PRAGMA foreign_key_check").fetchall()
            if bad:
                raise RuntimeError(f"foreign_key_check failed after srs_cards rebuild: "
                                   f"{[tuple(b) for b in bad[:3]]}")
            cx.execute("COMMIT")
        except Exception:
            cx.execute("ROLLBACK")
            raise
    finally:
        cx.execute("PRAGMA foreign_keys=ON")
        cx.isolation_level = old_iso
    return True


def init_db() -> None:
    settings.ensure_dirs()
    cx = connect()
    try:
        cx.executescript(SCHEMA)
        _migrate(cx)
        _rebuild_srs_cards_multi(cx)
        # indexes that depend on migrated columns (created AFTER the column exists
        # on pre-existing DBs, where _migrate adds it).
        cx.execute("CREATE INDEX IF NOT EXISTS idx_jobs_ready ON jobs(state, priority, run_after)")
        # Hot-query indexes added late (one-time build cost on an existing DB):
        #  - surface: moments_search's `lemma=? OR surface=?` was a full SCAN of
        #    line_lemmas, so lookup latency grew linearly with corpus size.
        #  - linked_episode_id: list_episodes runs correlated download-state
        #    subqueries per episode row.
        #  - video_path: scan_library's per-file "already imported?" check.
        cx.execute("CREATE INDEX IF NOT EXISTS idx_line_lemmas_surface ON line_lemmas(surface)")
        cx.execute("CREATE INDEX IF NOT EXISTS idx_downloads_linked_ep ON downloads(linked_episode_id)")
        cx.execute("CREATE INDEX IF NOT EXISTS idx_episodes_video_path ON episodes(video_path)")
        # seed/heal: any watched episode with no viewing on record gets a
        # first-watch row (its watched_at is the best date we have). Idempotent
        # per episode and runs every boot — the original all-or-nothing guard
        # ("skip if watch_history has ANY row") never backfilled anything,
        # because dev-testing rows already existed when it first ran.
        cx.execute(
            "INSERT INTO watch_history(episode_id, watched_at, source, is_rewatch) "
            "SELECT ep.id, COALESCE(ep.watched_at, ep.updated_at, datetime('now')), 'manual', 0 "
            "FROM episodes ep WHERE ep.watched=1 "
            "AND NOT EXISTS (SELECT 1 FROM watch_history wh WHERE wh.episode_id = ep.id)"
        )
        cx.commit()
    finally:
        cx.close()


def kv_get(key: str, default=None):
    with cursor() as cx:
        row = cx.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default


def kv_set(key: str, value: str) -> None:
    with cursor() as cx:
        cx.execute(
            "INSERT INTO kv(key,value,updated_at) VALUES(?,?,datetime('now')) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=datetime('now')",
            (key, value),
        )
