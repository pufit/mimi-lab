#!/usr/bin/env python3
"""Apply the curated `evidence_line_ids` to cards that already exist.

The user (2026-09-03): "If you're explaining context with the messages that are NOT
in the sentence, we should extend the period." `tools/srs_evidence_lines.py`
annotates `data/srs-curation/cards.json` with the neighbouring lines each card's
`why_clear` leans on; the importer picks them up for NEW cards. This tool does
the same for the deck that is already imported and clipped:

  for every card matched by (lemma, line_id) to a curation entry
    → rebuild `extend_json` from `evidence_line_ids` (+ the continuation rule)
    → recompute the clip window (`clip_start_ms/clip_end_ms`, §6.2)
    → bump `clip_version`, set `clip_status='pending'`
    → enqueue `srs_clip {card_id, v}` at priority 30 so the clip is re-cut

Idempotent: a card whose stored `extend_json` already equals the rebuilt one is
skipped (no version bump, no job). Safe to run against the LIVE database with
the server up — every write is one short transaction and the connections use a
30 s `busy_timeout`.

Usage:
  .venv/bin/python tools/srs_apply_evidence.py --dry-run
  .venv/bin/python tools/srs_apply_evidence.py [--limit N] [--lemma 語] [--db PATH]
                                               [--cards data/srs-curation/cards.json]
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DEFAULT_CARDS = ROOT / "data" / "srs-curation" / "cards.json"
BUSY_TIMEOUT_MS = 30_000

CARD_COLS = (
    "id, lemma, line_id, episode_id, start_ms, end_ms, norm_text, text, context_json, "
    "extend_json, clip_status, clip_version"
)


def _open(path: str) -> sqlite3.Connection:
    cx = sqlite3.connect(path, timeout=BUSY_TIMEOUT_MS / 1000.0, check_same_thread=False)
    cx.row_factory = sqlite3.Row
    cx.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    cx.execute("PRAGMA foreign_keys=ON")
    return cx


def _norm(entries) -> list[dict]:
    """Comparable form of one `extend_json` list (order + the fields that matter)."""
    out = []
    for x in entries or []:
        if not isinstance(x, dict):
            continue
        out.append({
            "line_id": x.get("line_id"),
            "start_ms": int(x.get("start_ms") or 0),
            "end_ms": int(x.get("end_ms") or 0),
            "text": x.get("text") or "",
            "role": x.get("role") or "continuation",
        })
    return out


def _json_list(raw) -> list:
    if not raw:
        return []
    try:
        val = json.loads(raw)
    except Exception:
        return []
    return val if isinstance(val, list) else []


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cards", default=str(DEFAULT_CARDS), help="curation file")
    ap.add_argument("--db", default="", help="database (default: settings.mimi_lab_db)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--lemma", default="", help="only this lemma (debugging)")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    if args.db:
        os.environ["MIMI_LAB_DB"] = str(Path(args.db).resolve())

    from app.config import settings                      # noqa: E402  (env first)
    from app.srs import clips                            # noqa: E402
    from app.srs.snapshot import build_extend, dialogue_neighbours  # noqa: E402

    doc = json.loads(Path(args.cards).read_text())
    entries: dict[tuple[str, int], dict] = {}
    annotated = 0
    for c in doc.get("cards") or []:
        if not isinstance(c, dict) or "evidence_line_ids" not in c:
            continue
        annotated += 1
        lemma = (c.get("lemma") or "").strip()
        try:
            lid = int(c.get("line_id"))
        except (TypeError, ValueError):
            continue
        if lemma:
            entries[(lemma, lid)] = c

    db_path = str(settings.mimi_lab_db)
    print(f"db     : {db_path}")
    print(f"cards  : {args.cards} — {annotated} annotated entries")

    cx = _open(db_path)
    try:
        rows = [dict(r) for r in cx.execute(
            f"SELECT {CARD_COLS} FROM srs_cards WHERE line_id IS NOT NULL"
            + (" AND lemma=?" if args.lemma else "") + " ORDER BY id",
            (args.lemma,) if args.lemma else ())]
        eps = {int(r["id"]): r["duration_ms"] for r in cx.execute(
            "SELECT id, duration_ms FROM episodes")}

        matched = changed = unchanged = no_entry = gone = 0
        plans: list[tuple[dict, list[dict], int, int]] = []
        for card in rows:
            entry = entries.get((card["lemma"], int(card["line_id"])))
            if entry is None:
                no_entry += 1
                continue
            matched += 1
            line_row = cx.execute(
                "SELECT id, episode_id, idx, start_ms, end_ms, text, text_furigana, translation "
                "FROM subtitle_lines WHERE id=?", (card["line_id"],)).fetchone()
            if line_row is None:
                gone += 1
                continue
            line = {"line_id": int(line_row["id"]), "episode_id": int(line_row["episode_id"]),
                    "idx": line_row["idx"], "start_ms": int(line_row["start_ms"] or 0),
                    "end_ms": int(line_row["end_ms"] or 0), "text": line_row["text"] or ""}
            before, after = dialogue_neighbours(cx, line, 2)
            extend, truncated = build_extend(
                cx, line, entry.get("evidence_line_ids") or [],
                next_line=after[0] if after else None,
                allowed_ids={n["line_id"] for n in (*before, *after) if n.get("line_id")},
            )
            prev, nxt = clips._neighbours(cx, card, extend)
            plan = clips.plan_window(line, extend, prev, nxt, eps.get(int(card["episode_id"] or 0)))
            # compare against what would be STORED (the window may drop one more
            # evidence line than `build_extend` did), so a second run is a no-op
            if _norm(plan.extend) == _norm(_json_list(card.get("extend_json"))):
                unchanged += 1
                continue
            changed += 1
            plans.append((card, plan.extend, plan.start_ms, plan.end_ms))
            if args.verbose or args.dry_run:
                shown = " ".join(f"{x.get('role', '?')[:4]}:{x.get('line_id')}"
                                 for x in plan.extend)
                print(f"  card {card['id']:>5} {card['lemma']:<8} "
                      f"{(plan.end_ms - plan.start_ms) / 1000:5.1f}s  [{shown}]"
                      + ("  TRUNCATED" if plan.truncated or truncated else ""))
            if args.limit and changed >= args.limit:
                break
    finally:
        cx.close()

    print(f"cards  : {len(rows)} with a line · matched {matched} · to change {changed} · "
          f"already current {unchanged} · no curation entry {no_entry} · line gone {gone}")
    if args.dry_run:
        print("dry run — nothing written")
        return 0
    if not plans:
        print("nothing to do")
        return 0

    from app.jobs.service import enqueue                 # noqa: E402

    wx = _open(db_path)
    # `srs_moments.evidence_line_ids_json` arrives with the app's additive
    # migration (app/db.py `_MIGRATIONS`), i.e. at the next server boot. Running
    # this tool against a DB that has not booted the new code yet is fine — the
    # cards are updated, only the moment memo is skipped.
    has_evidence_col = any(
        r["name"] == "evidence_line_ids_json"
        for r in wx.execute("PRAGMA table_info(srs_moments)"))
    if not has_evidence_col:
        print("note: srs_moments.evidence_line_ids_json is missing (server not restarted "
              "on the new code) — cards are updated, moment memos are skipped")
    now = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    written = queued = failed = 0
    try:
        for card, extend, start_ms, end_ms in plans:
            try:
                with wx:                                  # one short transaction per card
                    n = wx.execute(
                        "UPDATE srs_cards SET extend_json=?, clip_start_ms=?, clip_end_ms=?, "
                        "clip_version=clip_version+1, clip_status='pending', "
                        "clip_requested_at=?, clip_error=NULL, updated_at=datetime('now') "
                        "WHERE id=? AND clip_version=?",
                        (json.dumps(extend, ensure_ascii=False), start_ms, end_ms, now,
                         card["id"], card["clip_version"])).rowcount
                    if n == 1 and has_evidence_col:
                        wx.execute(
                            "UPDATE srs_moments SET evidence_line_ids_json=? "
                            "WHERE lemma=? AND episode_id=? AND norm_text=?",
                            (json.dumps([x["line_id"] for x in extend
                                         if x.get("role") == "evidence" and x.get("line_id")]),
                             card["lemma"], card["episode_id"], card["norm_text"]))
            except sqlite3.OperationalError as e:         # locked past the 30 s timeout
                print(f"  ! card {card['id']}: {e}")
                failed += 1
                continue
            if n != 1:                                    # clip_version moved under us
                print(f"  ~ card {card['id']}: clip_version changed — skipped")
                continue
            written += 1
            version = int(card["clip_version"]) + 1
            try:
                enqueue("srs_clip", {"card_id": int(card["id"]), "v": version}, priority=30)
                queued += 1
            except Exception as e:
                print(f"  ! enqueue for card {card['id']} failed: {e}")
                failed += 1
    finally:
        wx.close()

    print(f"updated {written} cards · queued {queued} srs_clip jobs · {failed} failures")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
