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
"""


def connect() -> sqlite3.Connection:
    settings.mimi_lab_db.parent.mkdir(parents=True, exist_ok=True)
    cx = sqlite3.connect(str(settings.mimi_lab_db), check_same_thread=False)
    cx.row_factory = sqlite3.Row
    cx.execute("PRAGMA journal_mode=WAL")
    cx.execute("PRAGMA foreign_keys=ON")
    cx.execute("PRAGMA busy_timeout=5000")
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
]


def _migrate(cx: sqlite3.Connection) -> None:
    """Apply additive column migrations to an existing DB. Idempotent; never
    drops or rewrites. Safe to run on every startup."""
    for table, column, ddl in _MIGRATIONS:
        cols = {r["name"] for r in cx.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            cx.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def init_db() -> None:
    settings.ensure_dirs()
    cx = connect()
    try:
        cx.executescript(SCHEMA)
        _migrate(cx)
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
