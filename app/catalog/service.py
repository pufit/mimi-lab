"""Catalog service — the anime library model.

Owns titles + episodes CRUD, library scan, and the derived grid stats.
Resolution of files->AniList titles lives in `app.match`; `scan_library`
calls into it (lazy import to avoid an import cycle, since match.confirm
calls back into upsert_title here).
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from ..config import settings
from ..db import connect, cursor
from ..models import Episode, Title

log = logging.getLogger("mimi_lab.catalog")

VIDEO_EXTS = {".mp4", ".mkv"}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _clean_description(desc: Optional[str]) -> Optional[str]:
    """AniList descriptions carry <br>/<i> HTML; keep it light for the grid."""
    if not desc:
        return desc
    return (
        desc.replace("<br>", "\n")
        .replace("<br/>", "\n")
        .replace("<br />", "\n")
        .strip()
    )


def _to_int(v) -> Optional[int]:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# titles
# ---------------------------------------------------------------------------
def upsert_title(anilist: dict) -> int:
    """Upsert a title from an AniList Media dict. Fills mal_id/tvdb_id from the
    Fribb idmap when available. Returns the anilist_id.

    Accepts either a raw AniList GraphQL Media dict (nested title{}, coverImage{})
    or an already-flattened dict (romaji/english/cover_url keys).
    """
    anilist_id = _to_int(anilist.get("id") or anilist.get("anilist_id"))
    if anilist_id is None:
        raise ValueError("upsert_title requires an AniList id")

    # accept nested or flat shapes
    title = anilist.get("title") or {}
    romaji = anilist.get("romaji") or title.get("romaji")
    english = anilist.get("english") or title.get("english")
    native = anilist.get("native") or title.get("native")

    cover = anilist.get("cover_url")
    if not cover:
        cover = (anilist.get("coverImage") or {}).get("large")
    banner = anilist.get("banner_url") or anilist.get("bannerImage")

    fmt = anilist.get("format")
    total_eps = _to_int(anilist.get("episodes") or anilist.get("total_episodes"))
    aired_eps = _to_int(anilist.get("aired_episodes"))
    year = _to_int(anilist.get("seasonYear") or anilist.get("year"))
    season = anilist.get("season")
    status = anilist.get("status")
    description = _clean_description(anilist.get("description"))

    mal_id = _to_int(anilist.get("idMal") or anilist.get("mal_id"))
    tvdb_id = _to_int(anilist.get("tvdb_id"))

    # backfill MAL/TVDB ids from the Fribb idmap (lazy import — match owns it)
    if mal_id is None or tvdb_id is None:
        try:
            from ..match import service as match_service

            if mal_id is None:
                mal_id = match_service.mal_id_for(anilist_id)
            if tvdb_id is None:
                tvdb_id = match_service.tvdb_id_for(anilist_id)
        except Exception as e:  # idmap not downloaded yet, etc.
            log.debug("idmap backfill skipped for %s: %s", anilist_id, e)

    with cursor() as cx:
        cx.execute(
            """
            INSERT INTO titles
              (anilist_id, mal_id, tvdb_id, romaji, english, native, format,
               total_episodes, aired_episodes, season, year, cover_url,
               banner_url, description, status, local_updated_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, datetime('now'), datetime('now'))
            ON CONFLICT(anilist_id) DO UPDATE SET
              mal_id        = COALESCE(excluded.mal_id, titles.mal_id),
              tvdb_id       = COALESCE(excluded.tvdb_id, titles.tvdb_id),
              romaji        = COALESCE(excluded.romaji, titles.romaji),
              english       = COALESCE(excluded.english, titles.english),
              native        = COALESCE(excluded.native, titles.native),
              format        = COALESCE(excluded.format, titles.format),
              total_episodes= COALESCE(excluded.total_episodes, titles.total_episodes),
              aired_episodes= COALESCE(excluded.aired_episodes, titles.aired_episodes),
              season        = COALESCE(excluded.season, titles.season),
              year          = COALESCE(excluded.year, titles.year),
              cover_url     = COALESCE(excluded.cover_url, titles.cover_url),
              banner_url    = COALESCE(excluded.banner_url, titles.banner_url),
              description   = COALESCE(excluded.description, titles.description),
              status        = COALESCE(excluded.status, titles.status),
              -- only a payload that actually carried AniList metadata counts as
              -- a refresh; a MAL-pull stub (ids + title string) must not make a
              -- stale row look freshly synced.
              local_updated_at = CASE WHEN excluded.total_episodes IS NOT NULL
                                       OR excluded.status IS NOT NULL
                                      THEN datetime('now')
                                      ELSE titles.local_updated_at END,
              updated_at    = datetime('now')
            """,
            (
                anilist_id, mal_id, tvdb_id, romaji, english, native, fmt,
                total_eps, aired_eps, season, year, cover, banner, description,
                status,
            ),
        )
    return anilist_id


# AniList statuses that can still change (episode counts, airing progress). A
# title in one of these is re-fetched by `refresh_stale_titles`; FINISHED /
# CANCELLED rows are final and never re-queried.
_LIVE_STATUSES = ("RELEASING", "NOT_YET_RELEASED", "HIATUS")


def refresh_title_meta(anilist_id: int) -> bool:
    """Re-fetch one title's AniList metadata. Best-effort; returns True on success.

    Called before a batch subtitle import: episode counts and airing progress
    are what decide relative-vs-absolute numbering, and a row first seen while
    the show was NOT_YET_RELEASED carries neither.
    """
    try:
        from ..match import service as match_service

        media = match_service.anilist_media_by_id(int(anilist_id))
        if not media:
            return False
        upsert_title(media)
        return True
    except Exception as e:
        log.warning("refresh_title_meta(%s) failed: %s", anilist_id, e)
        return False


def refresh_stale_titles(max_titles: int = 200) -> dict:
    """Re-fetch AniList metadata for every title that isn't finished, plus any
    stub row (ids + a MAL title string, no cover/romaji — renders as a blank card).

    `upsert_title` has always handled updates, but nothing ever called it again
    for a title that already looked complete — so a show first seen as
    NOT_YET_RELEASED kept `total_episodes = NULL` and `status =
    'NOT_YET_RELEASED'` forever, silently disabling every episode-count-gated
    safety net (episode backfill, absolute-number folding, phantom pruning).
    """
    from ..match import service as match_service

    with cursor() as cx:
        ids = [
            r["anilist_id"]
            for r in cx.execute(
                "SELECT anilist_id FROM titles "
                "WHERE status IS NULL OR status IN (%s) "
                "   OR cover_url IS NULL OR cover_url='' "
                "   OR romaji IS NULL OR romaji='' "
                "ORDER BY COALESCE(local_updated_at,'') ASC LIMIT ?"
                % ",".join("?" * len(_LIVE_STATUSES)),
                (*_LIVE_STATUSES, int(max_titles)),
            )
        ]
    if not ids:
        return {"checked": 0, "refreshed": 0}

    refreshed = 0
    for media in match_service.anilist_media_by_ids(ids):
        try:
            upsert_title(media)
            refreshed += 1
        except Exception as e:
            log.debug("refresh upsert_title(%s) skipped: %s", media.get("id"), e)
    log.info("refresh_stale_titles: %d/%d title(s) refreshed", refreshed, len(ids))
    return {"checked": len(ids), "refreshed": refreshed}


def get_titles() -> list[Title]:
    """All titles with derived local episode count + average comprehension."""
    with cursor() as cx:
        rows = cx.execute(
            """
            SELECT t.*,
                   COUNT(e.id)                         AS episode_count_local,
                   AVG(e.comprehension_pct)            AS avg_comprehension
            FROM titles t
            LEFT JOIN episodes e ON e.anilist_id = t.anilist_id
            GROUP BY t.anilist_id
            ORDER BY COALESCE(t.english, t.romaji, '')
            """
        ).fetchall()
    return [_row_to_title(r) for r in rows]


def get_title(anilist_id: int) -> Optional[Title]:
    with cursor() as cx:
        row = cx.execute(
            """
            SELECT t.*,
                   COUNT(e.id)              AS episode_count_local,
                   AVG(e.comprehension_pct) AS avg_comprehension
            FROM titles t
            LEFT JOIN episodes e ON e.anilist_id = t.anilist_id
            WHERE t.anilist_id = ?
            GROUP BY t.anilist_id
            """,
            (anilist_id,),
        ).fetchone()
    return _row_to_title(row) if row else None


def _row_to_title(r) -> Title:
    avg = r["avg_comprehension"]
    return Title(
        anilist_id=r["anilist_id"],
        mal_id=r["mal_id"],
        romaji=r["romaji"],
        english=r["english"],
        native=r["native"],
        format=r["format"],
        total_episodes=r["total_episodes"],
        season=r["season"],
        year=r["year"],
        cover_url=r["cover_url"],
        banner_url=r["banner_url"],
        description=r["description"],
        status=r["status"],
        mal_status=r["mal_status"],
        mal_score=r["mal_score"],
        mal_progress=r["mal_progress"],
        episode_count_local=r["episode_count_local"] or 0,
        avg_comprehension=round(avg, 1) if avg is not None else None,
    )


# ---------------------------------------------------------------------------
# episodes
# ---------------------------------------------------------------------------
def ensure_episodes(anilist_id: int) -> int:
    """Backfill placeholder episode rows 1..total_episodes for a title so the
    full episode list shows (idempotent, sanity-capped). Returns # created."""
    with cursor() as cx:
        t = cx.execute(
            "SELECT total_episodes FROM titles WHERE anilist_id=?", (anilist_id,)
        ).fetchone()
        total = (t["total_episodes"] if t else None) or 0
        if total <= 0 or total > 500:  # unknown/ongoing or implausible -> skip
            return 0
        existing = {
            r["ep_number"]
            for r in cx.execute(
                "SELECT ep_number FROM episodes WHERE anilist_id=?", (anilist_id,)
            )
        }
        missing = [ep for ep in range(1, total + 1) if ep not in existing]
        if missing:
            cx.executemany(
                "INSERT INTO episodes(anilist_id, ep_number, title) VALUES(?,?,?)",
                [(anilist_id, ep, f"Episode {ep}") for ep in missing],
            )
    return len(missing)


def list_episodes(anilist_id: int) -> list[Episode]:
    ensure_episodes(anilist_id)
    with cursor() as cx:
        rows = cx.execute(
            """
            SELECT e.*,
                   EXISTS(SELECT 1 FROM subtitles s WHERE s.episode_id = e.id
                          AND COALESCE(s.lang,'ja') <> 'en') AS has_subtitle,
                   EXISTS(SELECT 1 FROM subtitles s WHERE s.episode_id = e.id
                          AND s.lang = 'en') AS has_english,
                   (SELECT d.state FROM downloads d WHERE d.linked_episode_id = e.id
                    ORDER BY d.id DESC LIMIT 1) AS download_state,
                   (SELECT d.id FROM downloads d WHERE d.linked_episode_id = e.id
                    ORDER BY d.id DESC LIMIT 1) AS download_id
            FROM episodes e
            WHERE e.anilist_id = ?
            ORDER BY e.ep_number
            """,
            (anilist_id,),
        ).fetchall()
    return [_row_to_episode(r) for r in rows]


def _row_to_episode(r) -> Episode:
    return Episode(
        id=r["id"],
        anilist_id=r["anilist_id"],
        ep_number=r["ep_number"],
        title=r["title"],
        video_path=r["video_path"],
        codec=r["codec"],
        container=r["container"],
        duration_ms=r["duration_ms"],
        watched=bool(r["watched"]),
        has_subtitle=bool(r["has_subtitle"]),
        has_english=bool(r["has_english"]) if "has_english" in r.keys() else False,
        download_state=(r["download_state"] if "download_state" in r.keys() else None),
        download_id=(r["download_id"] if "download_id" in r.keys() else None),
        comprehension_pct=r["comprehension_pct"],
        comprehension_rating=r["comprehension_rating"],
        comprehension_source=r["comprehension_source"],
        new_word_count=r["new_word_count"],
    )


# columns on `episodes` that upsert_episode is allowed to set
_EP_FIELDS = {
    "absolute_number", "title", "video_path", "codec", "container",
    "duration_ms", "watched", "watch_progress_ms", "comprehension_pct",
    "comprehension_rating", "comprehension_source", "new_word_count",
}


def upsert_episode(anilist_id: int, ep_number: int, **fields) -> int:
    """Insert or update an episode (keyed by anilist_id+ep_number). Returns id.

    Only known `episodes` columns are honoured; unknown kwargs are ignored.
    On update, only the provided fields are overwritten (others preserved).
    """
    ep_number = int(ep_number)
    cols = {k: v for k, v in fields.items() if k in _EP_FIELDS}

    with cursor() as cx:
        existing = cx.execute(
            "SELECT id FROM episodes WHERE anilist_id=? AND ep_number=?",
            (anilist_id, ep_number),
        ).fetchone()

        if existing:
            ep_id = existing["id"]
            if cols:
                assignments = ", ".join(f"{k}=?" for k in cols)
                cx.execute(
                    f"UPDATE episodes SET {assignments}, updated_at=datetime('now') WHERE id=?",
                    (*cols.values(), ep_id),
                )
            return ep_id

        all_cols = ["anilist_id", "ep_number", *cols.keys()]
        placeholders = ", ".join("?" for _ in all_cols)
        cx.execute(
            f"INSERT INTO episodes ({', '.join(all_cols)}) VALUES ({placeholders})",
            (anilist_id, ep_number, *cols.values()),
        )
        return cx.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]


def prune_phantom_episodes(anilist_id: Optional[int] = None) -> dict:
    """Delete episode rows whose number exceeds the title's known episode count.

    Absolute (cross-season) numbering can create episode rows past a season's
    real length. The ingest path now folds those numbers (see
    match.normalize_episode_number); this cleans up rows created before the fix.

    Only acts on titles with a plausible `total_episodes` (1..500), and never
    deletes an episode that has a local video file (don't discard real media —
    only the phantom, subtitle-only rows). Cascades to subtitles/lines/lemmas;
    the FTS mirror is cleared explicitly (it has no FK). Returns a summary.
    """
    with cursor() as cx:
        where = (
            "FROM episodes e JOIN titles t ON t.anilist_id = e.anilist_id "
            "WHERE t.total_episodes IS NOT NULL "
            "AND t.total_episodes BETWEEN 1 AND 500 "
            "AND e.ep_number > t.total_episodes "
            "AND (e.video_path IS NULL OR e.video_path = '')"
        )
        params: tuple = ()
        if anilist_id is not None:
            where += " AND e.anilist_id = ?"
            params = (anilist_id,)

        rows = cx.execute(
            f"SELECT e.id AS id, e.anilist_id AS anilist_id {where}", params
        ).fetchall()
        ep_ids = [r["id"] for r in rows]
        titles_affected = sorted({r["anilist_id"] for r in rows})

        if ep_ids:
            qmarks = ",".join("?" * len(ep_ids))
            # FTS has no FK cascade — clear it first by episode id.
            cx.execute(
                f"DELETE FROM subtitle_fts WHERE episode_id IN ({qmarks})", ep_ids
            )
            # episodes delete cascades to subtitles -> subtitle_lines -> line_lemmas
            cx.execute(f"DELETE FROM episodes WHERE id IN ({qmarks})", ep_ids)

    log.info(
        "prune_phantom_episodes: removed %d row(s) across %d title(s)",
        len(ep_ids), len(titles_affected),
    )
    return {"removed": len(ep_ids), "titles": titles_affected}


def _fold_candidates(cx, anilist_id: Optional[int]) -> list[int]:
    q = "SELECT DISTINCT anilist_id FROM episodes"
    params: tuple = ()
    if anilist_id is not None:
        q += " WHERE anilist_id = ?"
        params = (int(anilist_id),)
    return [r["anilist_id"] for r in cx.execute(q, params)]


def _fold_rank(row) -> tuple:
    """Sort key for choosing which row of a duplicate pair survives.

    Never discard real media or watch progress; past that, keep the better
    subtitle (objective alignment score first, then the fuller transcript).
    """
    return (
        1 if (row["video_path"] or "") else 0,
        1 if (row["watched"] or row["watch_progress_ms"]) else 0,
        row["align_score"] if row["align_score"] is not None else -1.0,
        row["line_count"] or 0,
    )


def fold_absolute_episodes(anilist_id: Optional[int] = None, *,
                           apply: bool = False) -> dict:
    """Repair episode rows that were created under ABSOLUTE (cross-season)
    numbering, folding them onto their true season-relative numbers.

    Before the ingest path learned to fold (match.infer_episode_offset), a
    sequel could end up holding the same episode twice — once as `1` and once
    as `13` — plus later episodes stranded at absolute numbers with no relative
    twin at all. This renumbers the stranded rows and merges the duplicate
    pairs, keeping whichever row carries media / watch progress / the better
    subtitle.

    Dry-run by default: pass apply=True to write. Returns the plan either way.
    """
    plan: list[dict] = []
    offsets_learned: dict[int, int] = {}
    with cursor() as cx:
        for aid in _fold_candidates(cx, anilist_id):
            rows = cx.execute(
                """
                SELECT e.id, e.ep_number, e.video_path, e.watched,
                       e.watch_progress_ms,
                       (SELECT MAX(s.align_score) FROM subtitles s
                         WHERE s.episode_id = e.id) AS align_score,
                       (SELECT COUNT(*) FROM subtitle_lines sl
                         JOIN subtitles s2 ON s2.id = sl.subtitle_id
                        WHERE s2.episode_id = e.id) AS line_count
                  FROM episodes e WHERE e.anilist_id = ?
                """,
                (aid,),
            ).fetchall()
            if not rows:
                continue

            from ..match import service as match_service

            ceiling = match_service.episode_ceiling(aid)
            offset = match_service.infer_episode_offset(
                [r["ep_number"] for r in rows], ceiling
            )
            if not offset:
                continue

            # Remember it: after this repair the absolute rows are gone, so the
            # set-based inference can never re-derive the offset — and a later
            # single file (one downloaded release, a late subtitle sweep) still
            # needs it to fold instead of being dropped as unplaceable.
            offsets_learned[aid] = offset

            by_number = {r["ep_number"]: r for r in rows}
            for row in sorted(rows, key=lambda r: r["ep_number"]):
                if row["ep_number"] <= ceiling:
                    continue
                target = row["ep_number"] - offset
                twin = by_number.get(target)
                if twin is None:
                    plan.append({
                        "anilist_id": aid, "action": "renumber",
                        "keep_id": row["id"], "from": row["ep_number"],
                        "to": target, "drop_id": None,
                    })
                    continue
                keep, drop = (row, twin) if _fold_rank(row) > _fold_rank(twin) \
                    else (twin, row)
                plan.append({
                    "anilist_id": aid, "action": "merge",
                    "keep_id": keep["id"], "from": row["ep_number"],
                    "to": target, "drop_id": drop["id"],
                })

        if apply and plan:
            # losers first — UNIQUE(anilist_id, ep_number) would reject a
            # renumber onto a row that is still present.
            drop_ids = [p["drop_id"] for p in plan if p["drop_id"]]
            if drop_ids:
                qmarks = ",".join("?" * len(drop_ids))
                # FTS has no FK cascade — clear it explicitly, as prune does.
                cx.execute(
                    f"DELETE FROM subtitle_fts WHERE episode_id IN ({qmarks})",
                    drop_ids,
                )
                cx.execute(
                    f"DELETE FROM episodes WHERE id IN ({qmarks})", drop_ids
                )
            for p in sorted(plan, key=lambda x: x["to"]):
                cx.execute(
                    "UPDATE episodes SET ep_number=?, title=?, "
                    "updated_at=datetime('now') WHERE id=?",
                    (p["to"], f"Episode {p['to']}", p["keep_id"]),
                )
            for aid, off in offsets_learned.items():
                cx.execute(
                    "UPDATE titles SET episode_offset=?, updated_at=datetime('now') "
                    "WHERE anilist_id=? AND COALESCE(episode_offset,0) <> ?",
                    (off, aid, off),
                )

    titles_affected = sorted({p["anilist_id"] for p in plan})
    log.info(
        "fold_absolute_episodes: %d row(s) across %d title(s)%s",
        len(plan), len(titles_affected), "" if apply else " (dry run)",
    )
    return {
        "applied": bool(apply),
        "changes": len(plan),
        "merged": sum(1 for p in plan if p["action"] == "merge"),
        "renumbered": sum(1 for p in plan if p["action"] == "renumber"),
        "titles": titles_affected,
        "plan": plan,
    }


def delete_title(anilist_id: int, delete_files: bool = True) -> dict:
    """Remove a title and EVERYTHING attached to it — episodes, subtitles,
    corpus (lines + lemmas + FTS), downloads rows, clips, subtitle/video files
    on disk, and the match-cache entries that would silently re-link new files
    to it.

    Ad-hoc raw-SQL deletions can leave FTS orphans because subtitle_fts has no
    FK — this is the proper cascade. File deletion is realpath-guarded to the managed roots
    (Library/ + data/). Returns a summary.
    """
    import os

    summary: dict = {"anilist_id": anilist_id}
    lib_root = os.path.realpath(str(settings.library_dir))
    data_root = os.path.realpath(str(settings.mimi_lab_db.parent))

    def _managed(p: str) -> bool:
        real = os.path.realpath(p)
        return any(real.startswith(root + os.sep) for root in (lib_root, data_root))

    with cursor() as cx:
        trow = cx.execute(
            "SELECT romaji, english FROM titles WHERE anilist_id=?", (anilist_id,)
        ).fetchone()
        if not trow:
            return {"ok": False, "reason": "title not found"}
        summary["title"] = trow["english"] or trow["romaji"]

        ep_rows = cx.execute(
            "SELECT id, video_path FROM episodes WHERE anilist_id=?", (anilist_id,)
        ).fetchall()
        ep_ids = [r["id"] for r in ep_rows]
        video_paths = [r["video_path"] for r in ep_rows if r["video_path"]]

        sub_paths: list[str] = []
        line_ids: list[int] = []
        if ep_ids:
            marks = ",".join("?" * len(ep_ids))
            sub_paths = [r["path"] for r in cx.execute(
                f"SELECT path FROM subtitles WHERE episode_id IN ({marks}) AND path IS NOT NULL",
                ep_ids)]
            line_ids = [r["id"] for r in cx.execute(
                f"SELECT id FROM subtitle_lines WHERE episode_id IN ({marks})", ep_ids)]
            # FTS mirror has no FK — clear explicitly BEFORE the cascade.
            cx.execute(f"DELETE FROM subtitle_fts WHERE episode_id IN ({marks})", ep_ids)
            # cascades: subtitles -> subtitle_lines -> line_lemmas
            cx.execute(f"DELETE FROM episodes WHERE id IN ({marks})", ep_ids)
        cx.execute("DELETE FROM downloads WHERE anilist_id=?", (anilist_id,))
        cx.execute("DELETE FROM titles WHERE anilist_id=?", (anilist_id,))
        # match cache: a cached title->id mapping would re-link future files
        cx.execute(
            "DELETE FROM kv WHERE key LIKE 'match:title:%' AND value=?",
            (str(anilist_id),),
        )
    summary.update(episodes=len(ep_ids), lines=len(line_ids), subtitle_files=len(sub_paths))

    removed_files = 0
    if delete_files:
        for p in sub_paths + video_paths:
            try:
                if p and os.path.isfile(p) and _managed(p):
                    os.unlink(p)
                    removed_files += 1
            except Exception as e:
                log.debug("delete_title: could not remove %s: %s", p, e)
        # clips for the deleted lines
        for lid in line_ids:
            for ext in ("jpg", "m4a"):
                f = settings.clips_dir / f"line_{lid}.{ext}"
                try:
                    if f.exists():
                        f.unlink()
                        removed_files += 1
                except Exception:
                    pass
        # the show's Library directory, if it exists and is now empty-ish
        try:
            import shutil as _shutil
            from ..media.service import safe_title
            show_dir = Path(settings.library_dir) / safe_title(summary["title"] or "")
            if show_dir.is_dir() and _managed(str(show_dir)):
                _shutil.rmtree(show_dir)
                removed_files += 1
        except Exception as e:
            log.debug("delete_title: show dir cleanup skipped: %s", e)
    summary["files_removed"] = removed_files

    # downstream caches: the leverage ranking + any comprehension aggregates
    # counted this title's corpus
    try:
        from ..db import kv_set
        kv_set("leverage.dirty", "1")
    except Exception:
        pass
    try:
        from ..events import service as events
        events.record(
            "system", f"Removed from library: {summary['title']}",
            "info",
            detail=f"{len(ep_ids)} episodes, {len(line_ids)} corpus lines, "
                   f"{removed_files} files deleted.",
            meta={"anilist_id": anilist_id},
        )
    except Exception:
        pass
    log.info("delete_title(%s): %s", anilist_id, summary)
    summary["ok"] = True
    return summary


def set_watched(episode_id: int, watched: bool, source: str = "manual") -> bool:
    with cursor() as cx:
        prev = cx.execute(
            "SELECT watched, anilist_id FROM episodes WHERE id=?", (episode_id,)
        ).fetchone()
        if not prev:
            return False
        # watched_at keeps the FIRST watch date (the viewing timeline lives in
        # watch_history now — re-marking used to rewrite history by moving it)
        cur = cx.execute(
            "UPDATE episodes SET watched=?, "
            "watched_at=CASE WHEN ? THEN COALESCE(watched_at, datetime('now')) ELSE watched_at END, "
            "updated_at=datetime('now') WHERE id=?",
            (1 if watched else 0, 1 if watched else 0, episode_id),
        )
        ok = cur.rowcount > 0
        row = prev
        # log the viewing on a 0 -> 1 transition; a prior history row (or the
        # legacy watched flag) makes it a rewatch
        if ok and watched and not prev["watched"]:
            had_prior = cx.execute(
                "SELECT 1 FROM watch_history WHERE episode_id=? LIMIT 1", (episode_id,)
            ).fetchone() is not None
            cx.execute(
                "INSERT INTO watch_history(episode_id, source, is_rewatch) VALUES(?,?,?)",
                (episode_id, source, 1 if had_prior else 0),
            )
    # Two-way MAL sync: push the new watched-count for this title (async job,
    # deduped). This was the missing half of "two-way sync" — watching an
    # episode never reached MAL before.
    if ok and row and row["anilist_id"]:
        try:
            from ..jobs.service import enqueue
            enqueue("mal_sync", {"push": int(row["anilist_id"])}, delay_seconds=5)
        except Exception as e:
            log.debug("could not enqueue MAL push for episode %s: %s", episode_id, e)
    return ok


def set_watched_up_to(anilist_id: int, ep_number: int) -> dict:
    """Bulk-mark episodes 1..ep_number watched (single MAL push at the end)."""
    with cursor() as cx:
        # log each 0 -> 1 transition as a viewing BEFORE flipping the flags
        # (same predicate as the UPDATE below) — this path used to bypass
        # watch_history entirely, so bulk-marked episodes never counted as
        # views. A prior history row makes it a rewatch, mirroring set_watched.
        cx.execute(
            "INSERT INTO watch_history(episode_id, source, is_rewatch) "
            "SELECT e.id, 'manual', "
            "EXISTS(SELECT 1 FROM watch_history wh WHERE wh.episode_id = e.id) "
            "FROM episodes e "
            "WHERE e.anilist_id=? AND e.ep_number<=? AND COALESCE(e.watched,0)=0",
            (anilist_id, int(ep_number)),
        )
        cur = cx.execute(
            "UPDATE episodes SET watched=1, watched_at=COALESCE(watched_at, datetime('now')), "
            "updated_at=datetime('now') "
            "WHERE anilist_id=? AND ep_number<=? AND COALESCE(watched,0)=0",
            (anilist_id, int(ep_number)),
        )
        n = cur.rowcount or 0
    if n:
        try:
            from ..jobs.service import enqueue
            enqueue("mal_sync", {"push": int(anilist_id)}, delay_seconds=5)
        except Exception as e:
            log.debug("could not enqueue MAL push for title %s: %s", anilist_id, e)
    return {"anilist_id": anilist_id, "marked": n}


def continue_watching(limit: int = 12) -> list[dict]:
    """The library rail: resumable in-progress episodes first, then the next
    unwatched LOCAL episode of shows you've recently watched."""
    out: list[dict] = []
    seen_titles: set[int] = set()
    with connect() as cx:
        # resumable: >30s in, <90% through, video on disk
        for r in cx.execute(
            "SELECT e.id AS episode_id, e.anilist_id, e.ep_number, e.watch_progress_ms, "
            "       e.duration_ms, e.comprehension_pct, t.romaji, t.english, t.cover_url "
            "FROM episodes e JOIN titles t ON t.anilist_id=e.anilist_id "
            "WHERE COALESCE(e.watched,0)=0 AND COALESCE(e.watch_progress_ms,0) > 30000 "
            "AND e.video_path IS NOT NULL AND e.video_path != '' "
            "ORDER BY e.updated_at DESC LIMIT ?", (limit,)
        ):
            if r["duration_ms"] and r["watch_progress_ms"] / r["duration_ms"] >= 0.9:
                continue
            seen_titles.add(r["anilist_id"])
            out.append({
                "kind": "resume",
                "episode_id": r["episode_id"], "anilist_id": r["anilist_id"],
                "ep_number": r["ep_number"],
                "title": r["english"] or r["romaji"],
                "cover_url": r["cover_url"],
                "progress_ms": r["watch_progress_ms"], "duration_ms": r["duration_ms"],
                "comprehension_pct": r["comprehension_pct"],
            })
        # next-up: shows watched recently -> lowest unwatched ep WITH video
        for r in cx.execute(
            "WITH recent AS ("
            "  SELECT anilist_id, MAX(COALESCE(watched_at, updated_at)) AS last_w "
            "  FROM episodes WHERE watched=1 GROUP BY anilist_id ORDER BY last_w DESC LIMIT 10) "
            "SELECT e.id AS episode_id, e.anilist_id, e.ep_number, e.comprehension_pct, "
            "       e.duration_ms, t.romaji, t.english, t.cover_url "
            "FROM recent rc "
            "JOIN episodes e ON e.anilist_id = rc.anilist_id AND COALESCE(e.watched,0)=0 "
            "     AND e.video_path IS NOT NULL AND e.video_path != '' "
            "JOIN titles t ON t.anilist_id = e.anilist_id "
            "WHERE e.ep_number = (SELECT MIN(e2.ep_number) FROM episodes e2 "
            "                     WHERE e2.anilist_id = e.anilist_id AND COALESCE(e2.watched,0)=0 "
            "                     AND e2.video_path IS NOT NULL AND e2.video_path != '') "
            "ORDER BY rc.last_w DESC LIMIT ?", (limit,)
        ):
            if r["anilist_id"] in seen_titles:
                continue
            out.append({
                "kind": "next",
                "episode_id": r["episode_id"], "anilist_id": r["anilist_id"],
                "ep_number": r["ep_number"],
                "title": r["english"] or r["romaji"],
                "cover_url": r["cover_url"],
                "progress_ms": None, "duration_ms": r["duration_ms"],
                "comprehension_pct": r["comprehension_pct"],
            })
    return out[:limit]


# ---------------------------------------------------------------------------
# library scan
# ---------------------------------------------------------------------------
def scan_library() -> dict:
    """Walk settings.library_dir for video files, parse + resolve each.

    Confident match -> upsert title + episode (with video_path).
    Otherwise       -> enqueue into match_queue for manual confirmation.
    Returns {found, matched, queued, already, errors}.
    """
    # lazy import: match.confirm -> catalog.upsert_title would otherwise cycle
    from ..match import service as match_service

    root = Path(settings.library_dir)
    found = matched = queued = already = errors = 0

    if not root.exists():
        return {"found": 0, "matched": 0, "queued": 0, "already": 0, "errors": 0}

    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in VIDEO_EXTS:
            continue
        found += 1
        filename = path.name

        # skip files we already have linked by exact video_path
        with cursor() as cx:
            seen = cx.execute(
                "SELECT 1 FROM episodes WHERE video_path=? LIMIT 1", (str(path),)
            ).fetchone()
        if seen:
            already += 1
            continue

        try:
            result = match_service.match_file(filename)
        except Exception as e:
            errors += 1
            log.warning("scan: match failed for %s: %s", filename, e)
            continue

        ep_number = result.ep_number
        if result.confident and result.best and ep_number is not None:
            anilist_id = result.best.anilist_id
            match_service.upsert_title_from_candidate(result.best)
            upsert_episode(
                anilist_id,
                ep_number,
                video_path=str(path),
                container=path.suffix.lstrip("."),
            )
            matched += 1
        else:
            match_service.enqueue_unmatched(
                filename, result.parsed, result.candidates
            )
            queued += 1

    return {
        "found": found,
        "matched": matched,
        "queued": queued,
        "already": already,
        "errors": errors,
    }
