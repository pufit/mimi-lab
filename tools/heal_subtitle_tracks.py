"""One-off maintenance: find + heal structurally-broken subtitle tracks.

Companion to the display-sanity gate (subs.service.sub_display_sane). The gate
stops NEW broken tracks at every acceptance point; this sweep repairs what is
already on disk / in the DB:

  * English (lang='en') tracks that fail the gate — for example, "doubled"
    tracks (every line present twice, one copy mistimed → two English
    lines on screen at once). Heal: quarantine the file into
    data/quarantine-subs/<ts>/, delete the subtitles row, clear the (equally
    poisoned) per-line translations, and enqueue a fresh `english_subtitle_fetch`
    — with the new gates that lands on a sane source (MT when no trustworthy
    human track exists).
  * Orphan .en.srt sidecars next to videos with NO subtitles row that fail the
    gate — quarantined so the resolver/sidecar fallback can't serve them.
  * Japanese corpora whose FILE self-overlaps (bilingual JP+CN rips: every line
    twice at identical timestamps) — re-ingested; ingest now drops the
    non-Japanese twins, then comprehension is recomputed.
  * Episodes with MULTIPLE JA subtitle sets (rows ingested before the
    one-corpus-per-episode purge existed) — the current/best set is re-ingested,
    which purges the stale sets, then comprehension is recomputed and an
    `english_subtitle_fetch` re-merges/rebuilds the translations the re-ingest
    cleared. This prevents MT English from inheriting duplicate corpus lines.

Idempotent + resumable: healthy rows are skipped, so a re-run continues where a
crash left off. Dry-run by default; pass --apply to actually heal.

Run:  .venv/bin/python -m tools.heal_subtitle_tracks [--apply]
Progress is appended to data/heal_subtitle_tracks.log and printed to stdout.
"""
from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

from app.db import connect, cursor
from app.subs import service as S
from app.learn import service as L

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "data" / "heal_subtitle_tracks.log"
QUARANTINE = ROOT / "data" / "quarantine-subs" / time.strftime("%Y%m%d-%H%M%S")


def _log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _quarantine(path: Path, apply: bool) -> None:
    if not apply:
        return
    QUARANTINE.mkdir(parents=True, exist_ok=True)
    target = QUARANTINE / path.name
    i = 1
    while target.exists():
        target = QUARANTINE / f"{path.stem}.{i}{path.suffix}"
        i += 1
    shutil.move(str(path), str(target))


def _heal_english(apply: bool) -> tuple[int, int]:
    """Broken lang='en' rows -> quarantine + delete row + clear translations +
    re-enqueue english_subtitle_fetch. Returns (checked, healed)."""
    with connect() as cx:
        rows = cx.execute(
            "SELECT s.id, s.episode_id, s.source, s.path, e.video_path "
            "FROM subtitles s JOIN episodes e ON e.id = s.episode_id "
            "WHERE s.lang='en' AND s.path IS NOT NULL ORDER BY s.id"
        ).fetchall()
    checked = healed = 0
    for r in rows:
        p = Path(r["path"])
        if not p.exists():
            continue
        checked += 1
        ok, why = S.sub_display_sane(p, r["video_path"])
        if ok:
            continue
        healed += 1
        _log(f"EN ep {r['episode_id']} (sub {r['id']}, source={r['source']}): {why}"
             f" -> {'healing' if apply else 'WOULD heal'} ({p.name})")
        if not apply:
            continue
        _quarantine(p, apply)
        with cursor() as cx:
            cx.execute("DELETE FROM subtitles WHERE id=?", (r["id"],))
            # translations were merged FROM the broken track — equally poisoned
            cx.execute("UPDATE subtitle_lines SET translation=NULL WHERE episode_id=?",
                       (r["episode_id"],))
        try:
            from app.jobs.service import enqueue
            enqueue("english_subtitle_fetch", {"episode_id": r["episode_id"]})
        except Exception as e:
            _log(f"  ! could not enqueue english_subtitle_fetch: {e}")
    return checked, healed


def _heal_orphan_sidecars(apply: bool) -> tuple[int, int]:
    """Broken .en.srt sidecars with no subtitles row -> quarantine (the resolver
    would otherwise still serve them via the sidecar fallback)."""
    with connect() as cx:
        eps = cx.execute(
            "SELECT e.id, e.video_path FROM episodes e "
            "WHERE e.video_path IS NOT NULL AND e.video_path <> '' "
            "AND NOT EXISTS (SELECT 1 FROM subtitles s "
            "                WHERE s.episode_id=e.id AND s.lang='en')"
        ).fetchall()
    checked = healed = 0
    for e in eps:
        en = Path(e["video_path"]).with_suffix("").with_suffix(".en.srt")
        if not en.exists():
            continue
        checked += 1
        ok, why = S.sub_display_sane(en, e["video_path"])
        if ok:
            continue
        healed += 1
        _log(f"EN sidecar ep {e['id']} (no row): {why} -> "
             f"{'quarantining' if apply else 'WOULD quarantine'} ({en.name})")
        _quarantine(en, apply)
        if apply:
            try:
                from app.jobs.service import enqueue
                enqueue("english_subtitle_fetch", {"episode_id": e["id"]})
            except Exception as ex:
                _log(f"  ! could not enqueue english_subtitle_fetch: {ex}")
    return checked, healed


def _heal_duplicate_ja(apply: bool) -> tuple[int, int]:
    """Episodes with >1 non-EN subtitle sets holding lines (pre-purge legacy) ->
    re-ingest the current/best set; ingest's one-corpus purge drops the rest.
    Then recompute comprehension and re-run the english pass (the re-ingest
    rebuilt the lines, clearing translations)."""
    with connect() as cx:
        eps = [r["episode_id"] for r in cx.execute(
            "SELECT s.episode_id FROM subtitles s "
            "WHERE COALESCE(s.lang,'ja') <> 'en' "
            "AND EXISTS (SELECT 1 FROM subtitle_lines sl WHERE sl.subtitle_id=s.id) "
            "GROUP BY s.episode_id HAVING COUNT(*) > 1"
        ).fetchall()]
    healed = 0
    for ep_id in eps:
        row = S._current_ja_sub(ep_id)
        if not row or not row.get("path") or not Path(row["path"]).exists():
            _log(f"JA-dup ep {ep_id}: no usable current sub file — skipping")
            continue
        _log(f"JA-dup ep {ep_id}: multiple JA sets -> "
             f"{'re-ingesting' if apply else 'WOULD re-ingest'} "
             f"({Path(row['path']).name}, sub {row['id']})")
        if not apply:
            continue
        try:
            S.ingest_subtitle(
                ep_id, row["path"], source=row["source"] or "jimaku",
                jimaku_entry_id=row["jimaku_entry_id"], jimaku_file_id=row["jimaku_file_id"],
                jimaku_filename=row["jimaku_filename"], aligned=row["aligned"] or 0,
                align_tool=row["align_tool"], align_score=row["align_score"],
                align_ref=row["align_ref"],
            )
            try:
                L.comprehension_aligned(ep_id)
            except Exception:
                pass
            try:
                from app.jobs.service import enqueue
                enqueue("english_subtitle_fetch", {"episode_id": ep_id})
            except Exception as e:
                _log(f"  ! could not enqueue english_subtitle_fetch: {e}")
            healed += 1
        except Exception as e:
            _log(f"  ! re-ingest failed: {e}")
    return len(eps), healed


def _heal_japanese(apply: bool) -> tuple[int, int]:
    """JA corpora whose file self-overlaps (bilingual twins) -> re-ingest (the
    twin-drop now applies) + recompute comprehension."""
    with connect() as cx:
        rows = cx.execute(
            "SELECT s.id, s.episode_id, s.source, s.jimaku_entry_id, s.jimaku_file_id, "
            "       s.jimaku_filename, s.aligned, s.align_tool, s.align_score, "
            "       s.align_ref, s.path "
            "FROM subtitles s WHERE COALESCE(s.lang,'ja') <> 'en' "
            "AND s.path IS NOT NULL ORDER BY s.id"
        ).fetchall()
    checked = healed = 0
    for r in rows:
        p = Path(r["path"])
        if not p.exists():
            continue
        frac = S.sub_overlap_fraction(p)
        if frac is None or frac <= S._MAX_OVERLAP_FRACTION:
            continue
        checked += 1
        _log(f"JA ep {r['episode_id']} (sub {r['id']}): overlap {frac:.0%} -> "
             f"{'re-ingesting' if apply else 'WOULD re-ingest'} ({p.name})")
        if not apply:
            continue
        try:
            S.ingest_subtitle(
                r["episode_id"], p, source=r["source"] or "jimaku",
                jimaku_entry_id=r["jimaku_entry_id"], jimaku_file_id=r["jimaku_file_id"],
                jimaku_filename=r["jimaku_filename"], aligned=r["aligned"] or 0,
                align_tool=r["align_tool"], align_score=r["align_score"],
                align_ref=r["align_ref"],
            )
            try:
                L.comprehension_aligned(r["episode_id"])
            except Exception:
                pass
            healed += 1
        except Exception as e:
            _log(f"  ! re-ingest failed: {e}")
    return checked, healed


def main() -> int:
    apply = "--apply" in sys.argv
    _log(f"=== heal_subtitle_tracks start ({'APPLY' if apply else 'dry-run'}) ===")
    en_c, en_h = _heal_english(apply)
    or_c, or_h = _heal_orphan_sidecars(apply)
    dup_c, dup_h = _heal_duplicate_ja(apply)
    ja_c, ja_h = _heal_japanese(apply)
    _log(f"=== done: EN rows {en_h}/{en_c} broken, orphan sidecars {or_h}/{or_c} broken, "
         f"JA duplicate-sets {dup_h if apply else 0}/{dup_c} re-ingested, "
         f"JA overlapping {ja_h if apply else 0}/{ja_c} re-ingested"
         + ("" if apply else " (dry-run — nothing changed; use --apply)") + " ===")
    if apply and (en_h or or_h):
        _log(f"quarantined files -> {QUARANTINE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
