"""Initial deck import — `data/srs-curation/cards.json` (§5.11, amendments §B).

The curated deck (1,026 cards) is imported, not generated in-app. Idempotent by
`lemma`, one transaction per card, the whole deck placed on top of whatever is
already in the stack. Clip jobs are enqueued at priority 30; when the running
server does not yet register `srs_clip` they are parked, not failed, and
`unpark_jobs()` requeues them at the next boot.

`tools/srs_import_cards.py` calls `import_cards` directly and
`POST /api/srs/import` does the same in-process.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

from ..db import cursor
from ..models import SrsImportError, SrsImportReport
from .service import (
    _enqueue,
    _event,
    _publish,
    _resolve_line,
    _upsert_moment,
    current_migaku_status,
    renumber_stack,
)
from .service import _gloss_for, _now, _sql
from .snapshot import CardSpec, norm_text, snapshot_line

log = logging.getLogger("mimi_lab.srs.importer")

DEFAULT_PATH = "data/srs-curation/cards.json"

# Columns written for a curated card (the moment snapshot comes from
# `snapshot_line`, everything editorial from the curation file).
_COLUMNS = (
    "lemma", "reading", "pos", "gloss", "meaning_short", "meaning_full", "why_clear",
    "usage_note", "tags_json", "freq_rank", "source", "score", "clarity", "usefulness",
    "priority", "line_id", "episode_id", "anilist_id", "show_title", "ep_number", "start_ms",
    "end_ms", "text", "norm_text", "text_furigana", "translation", "translation_source",
    "target_surface", "tokens_json", "context_json", "extend_json", "alt_moment_ids_json",
    "clip_status", "clip_requested_at", "clip_version", "state", "queue_pos", "study_now",
    "migaku_status_seen",
)


def _int(value) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def import_cards(doc: dict) -> SrsImportReport:
    """Import `{generated_at, source, stats, cards:[…]}` (§5.11).

    Per card: skip when the lemma already has a card; resolve `line_id` (from the
    file, else by `(episode_id, norm_text(text), start_ms)`); `snapshot_line`;
    insert with `source='curated-initial'`, `state='new'`,
    `queue_pos = stack_rank`, `score = stack_score`; insert the primary and
    `alt_line_ids[]` `srs_moments` rows (`verdict='user'`, `accepted=1`); upsert
    `srs_words` (`judge_status='accepted'`, `card_id`); enqueue
    `srs_clip {card_id, v:1}`.
    """
    cards = [c for c in (doc.get("cards") or []) if isinstance(c, dict)]
    now = _now()
    created = skipped = queued = 0
    errors: list[SrsImportError] = []

    # 0. the curated deck goes on top: whatever is already in the stack is pushed
    #    down. Curated rows are inserted at `queue_pos = stack_rank`, so the shift
    #    must clear the deck's whole rank range — shifting by the number actually
    #    created would interleave the pre-existing rows with the curated deck
    #    whenever any card is skipped or fails. Retry safety comes from the
    #    `id <= max_id_before` guard alone: rows created by an earlier run of the
    #    same deck are never shifted again.
    deck_span = max(
        [len(cards)] + [r for r in (_int(c.get("stack_rank")) for c in cards) if r]
    )
    with cursor() as cx:
        row = cx.execute("SELECT COALESCE(MAX(id), 0) AS m FROM srs_cards").fetchone()
    max_id_before = int(row["m"] if row else 0)

    for entry in cards:
        lemma = (entry.get("lemma") or "").strip()
        if not lemma:
            errors.append(SrsImportError(lemma="", reason="missing lemma"))
            continue
        try:
            outcome = _import_one(entry, lemma, now)
        except Exception as e:                              # one bad card never stops the deck
            log.warning("import %s failed: %s", lemma, e)
            errors.append(SrsImportError(lemma=lemma, reason=str(e)[:200]))
            continue
        if outcome is None:
            skipped += 1
            continue
        card_id, has_clip = outcome
        created += 1
        if has_clip:
            _enqueue("srs_clip", {"card_id": card_id, "v": 1}, priority=30)
            queued += 1

    with cursor() as cx:
        if created:
            cx.execute(
                "UPDATE srs_cards SET queue_pos = queue_pos + ? "
                "WHERE state='new' AND id <= ?",
                (deck_span, max_id_before),
            )
        renumber_stack(cx)
    if created:
        _event(f"Imported {created} curated cards", "success",
               detail=f"skipped {skipped} existing · {queued} clip jobs queued")
        _publish("card")
    log.info("srs import: created=%d skipped=%d errors=%d clips=%d",
             created, skipped, len(errors), queued)
    return SrsImportReport(
        created=created, skipped_existing=skipped, clip_jobs_queued=queued, errors=errors
    )


def _import_one(entry: dict, lemma: str, now) -> Optional[tuple[int, bool]]:
    """Insert one curated card. Returns `(card_id, clip_wanted)` or `None` when
    the lemma already has a card. Raises with a human reason on failure."""
    with cursor() as cx:
        if cx.execute("SELECT 1 FROM srs_cards WHERE lemma=?", (lemma,)).fetchone():
            return None

        episode_id = _int(entry.get("episode_id"))
        text = entry.get("text") or ""
        start_ms = _int(entry.get("start_ms")) or 0
        line_id = _int(entry.get("line_id"))
        if line_id is not None:
            exists = cx.execute(
                "SELECT episode_id FROM subtitle_lines WHERE id=?", (line_id,)
            ).fetchone()
            if not exists:
                line_id = None
        if line_id is None:
            line_id = _resolve_line(cx, episode_id, text, start_ms)
        if line_id is None:
            raise ValueError("line not found (episode re-ingested or deleted)")

        surface = entry.get("target_surface") or None
        # `evidence_line_ids` (tools/srs_evidence_lines.py): the neighbouring
        # lines the curated `why_clear` leans on. They become the moment's
        # `extend` — the clip covers them and the front shows them (§6.2).
        evidence = [i for i in (_int(x) for x in (entry.get("evidence_line_ids") or []))
                    if i is not None]
        # the curation file already carries a trimmed translation, so the §B2
        # cleaning call would be ~1,000 Haiku calls for a discarded result.
        snap = snapshot_line(line_id, lemma, surface, reading=entry.get("reading"),
                             clean=not (entry.get("translation") or "").strip(),
                             evidence_line_ids=evidence)

        # the curated translation wins over the raw subtitle cue (it was cleaned
        # during curation); fall back to the snapshot's own cleaning.
        translation = (entry.get("translation") or "").strip() or snap["translation"]
        translation_source = "human" if entry.get("translation") else snap["translation_source"]

        spec = CardSpec(
            lemma=lemma,
            reading=entry.get("reading"),
            pos=entry.get("pos"),
            gloss=_gloss_for(lemma),
            meaning_short=entry.get("meaning_short"),
            meaning_full=entry.get("meaning_full"),
            why_clear=entry.get("why_clear"),
            usage_note=entry.get("usage_note"),
            tags=[str(t) for t in (entry.get("tags") or [])],
            freq_rank=_int(entry.get("jpdb_rank")),
            source="curated-initial",
            score=entry.get("stack_score"),
            clarity=entry.get("clarity"),
            usefulness=entry.get("usefulness"),
            priority=_int(entry.get("priority")),
            line_id=snap["line_id"],
            episode_id=snap["episode_id"],
            anilist_id=snap["anilist_id"],
            show_title=snap["show_title"] or entry.get("show"),
            ep_number=snap["ep_number"],
            start_ms=snap["start_ms"],
            end_ms=snap["end_ms"],
            text=snap["text"],
            norm_text=snap["norm_text"],
            text_furigana=snap["text_furigana"],
            translation=translation,
            translation_source=translation_source,
            target_surface=snap["target_surface"],
            tokens_json=snap["tokens_json"],
            context_json=snap["context_json"],
            extend=snap["extend"],
        )
        queue_pos = _int(entry.get("stack_rank")) or None

        values = [
            spec.lemma, spec.reading, spec.pos, spec.gloss, spec.meaning_short, spec.meaning_full,
            spec.why_clear, spec.usage_note, json.dumps(spec.tags, ensure_ascii=False),
            spec.freq_rank, spec.source, spec.score, spec.clarity, spec.usefulness, spec.priority,
            spec.line_id, spec.episode_id, spec.anilist_id, spec.show_title, spec.ep_number,
            spec.start_ms, spec.end_ms, spec.text, spec.norm_text or norm_text(spec.text),
            spec.text_furigana, spec.translation, spec.translation_source, spec.target_surface,
            spec.tokens_json, spec.context_json,
            json.dumps(spec.extend, ensure_ascii=False), "[]",
            "pending", _sql(now), 1, "new", queue_pos, 0,
            current_migaku_status(cx, lemma),
        ]
        cur = cx.execute(
            f"INSERT INTO srs_cards ({', '.join(_COLUMNS)}) "
            f"VALUES ({', '.join('?' * len(_COLUMNS))}) "
            f"ON CONFLICT(lemma, episode_id, norm_text) DO NOTHING",
            values,
        )
        if cur.rowcount == 0:
            # concurrent writer won the moment between the SELECT above and this
            # INSERT — lastrowid would be 0 and every downstream write (moments,
            # srs_words, the clip job) would target card 0. (lemma alone is no
            # longer unique — multi-card words, 2026-09-03.)
            return None
        card_id = int(cur.lastrowid)

        # primary moment + the curated alternates (§5.11.2)
        _upsert_moment(cx, lemma, snap, verdict="user", accepted=1,
                       clarity=entry.get("clarity"), now=now,
                       evidence_line_ids=snap.get("evidence_line_ids") or [])
        alt_ids: list[int] = []
        for alt in (entry.get("alt_line_ids") or []):
            alt_line = _int(alt)
            if alt_line is None:
                continue
            if not cx.execute("SELECT 1 FROM subtitle_lines WHERE id=?", (alt_line,)).fetchone():
                continue
            try:
                # clean=False: an alternate only ever supplies a swap-dialog
                # preview, and the §B2 trim is one Haiku call per alternate —
                # 2,059 calls (~25 s each, measured) for the curated deck. The
                # primary moment above still gets cleaned when the curation file
                # carries no translation of its own.
                alt_snap = snapshot_line(alt_line, lemma, clean=False)
            except (LookupError, ValueError):
                continue
            alt_ids.append(
                _upsert_moment(cx, lemma, alt_snap, verdict="user", accepted=1,
                               clarity=None, now=now)
            )
        if alt_ids:
            cx.execute(
                "UPDATE srs_cards SET alt_moment_ids_json=? WHERE id=?",
                (json.dumps(alt_ids, ensure_ascii=False), card_id),
            )
        cx.execute(
            "INSERT INTO srs_words(lemma, reading, gloss, freq_rank, occ, eps, "
            "leverage_crossings, judge_status, card_id, updated_at) "
            "VALUES(?,?,?,?,?,?,?, 'accepted', ?, datetime('now')) "
            "ON CONFLICT(lemma) DO UPDATE SET judge_status='accepted', "
            "card_id=COALESCE(srs_words.card_id, excluded.card_id), "
            "updated_at=datetime('now')",
            (
                lemma, spec.reading, spec.gloss, spec.freq_rank,
                _int(entry.get("occurrences")) or 0, _int(entry.get("episodes")) or 0,
                _int(entry.get("leverage_crossings")) or 0, card_id,
            ),
        )
    return card_id, bool(spec.text and spec.line_id)
