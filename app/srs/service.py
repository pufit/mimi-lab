"""SRS service — cards, stack, queue, review, reconcile, stats (§4, §7).

Every mutation is one `with cursor()` transaction that ends in
`renumber_stack`, so the stack invariants of §4.2 hold after every call:
exactly the `new` cards have a `queue_pos`, positions are dense 1..N,
`study_now` is 0 outside `new`, `step` is NULL outside learning/relearning, and
`last_review_at`/`introduced_at` are NULL while a card is `new`.

Error contract used by `router.py`:
  SrsNotFound  -> 404      SrsConflict -> 409 (`payload` becomes the body)
  SrsInvalid   -> 422      NotImplementedError -> 501

Calls into WP-B (`clips`, `generate`, `census`) are guarded: their hour-0 stubs
raise `NotImplementedError`, and nothing here may fail because a clip could not
be cut or a run could not be swept.
"""
from __future__ import annotations

import json
import logging
import os
import pathlib
import random
import shutil
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Optional

from ..config import ROOT, settings
from ..db import connect, cursor, kv_get, kv_set
from .constants import MAX_CARDS_PER_LEMMA
from .franchise import franchise_key
from ..models import (
    SrsBulkPrevious,
    SrsBulkRequest,
    SrsBulkResult,
    SrsCandidate,
    SrsCandidateMoment,
    SrsCandidates,
    SrsCard,
    SrsCardDetail,
    SrsCardList,
    SrsClip,
    SrsClipCounts,
    SrsContextLine,
    SrsCreateCard,
    SrsGenerateRequest,
    SrsGeneration,
    SrsGenerationRun,
    SrsImportReport,
    SrsIntervalPreview,
    SrsLineRef,
    SrsMoment,
    SrsMove,
    SrsPatchCard,
    SrsQueue,
    SrsQueueCounts,
    SrsResort,
    SrsReview,
    SrsReviewConflict,
    SrsReviewRequest,
    SrsReviewResult,
    SrsSettings,
    SrsSettingsPatch,
    SrsSiblingCard,
    SrsStats,
    SrsStatsDay,
    SrsStatsForecastDay,
    SrsStatsInterval,
    SrsSummary,
    SrsSwapMoment,
    SrsToken,
    SrsUndoResult,
    zero_states,
)
from . import scheduler
from .constants import (
    DEMOTE_SWAP_MOMENT,
    FAIL_MIN_GAP_HOURS,
    KNOWN_INTERVAL_DAYS,
    LEARN_AHEAD_MIN,
    MAX_DEMOTIONS,
    MAX_REVIEWS_PER_DAY,
    RELINK_TOLERANCE_MS,
    STACK_MIN,
    STALE_CLIP_MIN,
)
from .settings import get_settings, update_settings  # noqa: F401  (§11: service.get_settings)
from .snapshot import CardSpec, norm_text, snapshot_line

log = logging.getLogger("mimi_lab.srs")

# A word is SRS-known when the card is `known` or a mature review card (§2.6).
# Constant SQL fragment — no settings read on hot paths, no materialised flag.
KNOWN_SQL = f"(sc.state='known' OR (sc.state='review' AND sc.scheduled_days >= {KNOWN_INTERVAL_DAYS}))"

ACTIVE_STATES = ("new", "learning", "review", "relearning")
ALL_STATES = ("new", "learning", "review", "relearning", "known", "suspended", "rejected")

# Columns copied into `srs_reviews.before_json` (§2.4) — undo restores exactly these.
BEFORE_COLUMNS = (
    "state", "step", "stability", "difficulty", "due_at", "last_review_at", "last_fail_day",
    "scheduled_days", "reps", "lapses", "fail_count", "demoted_count", "demoted_at",
    "queue_pos", "study_now", "introduced_at", "line_id", "clip_version", "alt_moment_ids_json",
)

# The moment snapshot (§2.1) — backed up when a review swaps the moment.
SNAPSHOT_COLUMNS = (
    "line_id", "episode_id", "anilist_id", "show_title", "ep_number", "start_ms", "end_ms",
    "text", "norm_text", "text_furigana", "translation", "translation_source", "target_surface",
    "tokens_json", "context_json", "extend_json", "alt_moment_ids_json",
    "clip_status", "clip_start_ms", "clip_end_ms", "clip_bytes", "clip_error", "clip_version",
    "clip_requested_at",
)


class SrsNotFound(LookupError):
    """404 — unknown card / line / moment / lemma."""


class SrsConflict(RuntimeError):
    """409 — state conflict. `payload` is returned as the response body when set
    (e.g. `SrsReviewConflict`, `{detail, card_id}` on duplicate create)."""

    def __init__(self, detail: str, payload: Optional[dict] = None) -> None:
        super().__init__(detail)
        self.detail = detail
        self.payload = payload


class SrsInvalid(ValueError):
    """422 — action not allowed from this state, bad `target_surface`, bad path."""


# ---------------------------------------------------------------------------
# Time helpers (§2.2) — UTC 'YYYY-MM-DD HH:MM:SS' in the DB, ISO-8601 on the API
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _sql(ts: Optional[datetime]) -> Optional[str]:
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _dt(value) -> Optional[datetime]:
    return scheduler._as_dt(value)


def _iso(value) -> Optional[str]:
    ts = _dt(value)
    return None if ts is None else ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def _day(ts: Optional[datetime] = None) -> str:
    return scheduler.srs_day(ts or _now()).isoformat()


_local = threading.local()


def _enqueue(job_type: str, payload: dict, *, priority: int, delay_seconds: int = 0) -> None:
    """Enqueue a job — deferred to after the commit when a `_txn()` is open.

    SQLite allows one writer at a time: enqueuing from a second connection while
    an SRS transaction holds the write lock raises `database is locked`.
    """
    pending = getattr(_local, "deferred", None)
    if pending is not None:
        pending.append((job_type, payload, priority, delay_seconds))
        return
    from ..jobs.service import enqueue

    enqueue(job_type, payload, delay_seconds=delay_seconds, priority=priority)


@contextmanager
def _txn():
    """`cursor()` plus deferred job enqueues: everything `_enqueue`d inside the
    transaction is written after it commits (and dropped if it raises)."""
    outer = getattr(_local, "deferred", None) is None
    if outer:
        _local.deferred = []
    try:
        with cursor() as cx:
            yield cx
    except BaseException:
        if outer:
            _local.deferred = None
        raise
    if outer:
        jobs, _local.deferred = _local.deferred, None
        from ..jobs.service import enqueue

        for job_type, payload, priority, delay in jobs:
            enqueue(job_type, payload, delay_seconds=delay, priority=priority)


def _publish(what: str, **data) -> None:
    try:
        from ..events import bus

        bus.publish("srs", {"what": what, **data})
    except Exception as e:                                  # pragma: no cover
        log.debug("srs bus publish failed: %s", e)


def _event(title: str, kind: str = "info", detail: Optional[str] = None) -> None:
    try:
        from ..events import service as events

        events.record("srs", title, kind, detail=detail)
    except Exception as e:                                  # pragma: no cover
        log.debug("srs event failed: %s", e)


def _recompute_comprehension() -> None:
    """A word crossed the known threshold — one delayed, deduped recompute (§2.6.4)."""
    try:
        _enqueue("comprehension_recompute_all", {}, priority=10, delay_seconds=600)
    except Exception as e:                                  # pragma: no cover
        log.warning("could not enqueue comprehension recompute: %s", e)


# ---------------------------------------------------------------------------
# Known-state helpers (§2.6) — consumed by learn/service.py and the census
# ---------------------------------------------------------------------------

def known_forms() -> set[str]:
    """Lemmas that count as KNOWN for comprehension (KNOWN_SQL)."""
    cx = connect()
    try:
        return {r["lemma"] for r in cx.execute(f"SELECT lemma FROM srs_cards sc WHERE {KNOWN_SQL}")}
    finally:
        cx.close()


def active_forms() -> set[str]:
    """Lemmas being studied but not yet known (transcript's LEARNING colour)."""
    cx = connect()
    try:
        return {
            r["lemma"]
            for r in cx.execute(
                "SELECT lemma FROM srs_cards sc WHERE sc.state IN ('learning','review','relearning') "
                f"AND NOT {KNOWN_SQL}"
            )
        }
    finally:
        cx.close()


def existing_lemmas() -> set[str]:
    """Every lemma with an `srs_cards` row, in any state (generation dedupe)."""
    cx = connect()
    try:
        return {r["lemma"] for r in cx.execute("SELECT lemma FROM srs_cards")}
    finally:
        cx.close()


def current_migaku_status(cx, lemma: str) -> str:
    """`known_words.status` for the lemma, or `'ABSENT'` (§2.5)."""
    row = cx.execute(
        "SELECT status FROM known_words WHERE dict_form=? "
        "ORDER BY CASE status WHEN 'KNOWN' THEN 0 WHEN 'IGNORED' THEN 1 "
        "WHEN 'LEARNING' THEN 2 ELSE 3 END LIMIT 1",
        (lemma,),
    ).fetchone()
    return (row["status"] or "ABSENT").upper() if row else "ABSENT"


# ---------------------------------------------------------------------------
# Stack primitives (§4.1)
# ---------------------------------------------------------------------------

def count_new() -> int:
    """Cards in `state='new'` (stack size, top-up predicate)."""
    cx = connect()
    try:
        return int(cx.execute("SELECT COUNT(*) n FROM srs_cards WHERE state='new'").fetchone()["n"])
    finally:
        cx.close()


def stack_bottom(cx) -> int:
    """`MAX(queue_pos) + 1` over `state='new'`."""
    row = cx.execute(
        "SELECT COALESCE(MAX(queue_pos), 0) + 1 AS pos FROM srs_cards WHERE state='new'"
    ).fetchone()
    return int(row["pos"])


def renumber_stack(cx) -> None:
    """Make `queue_pos` dense 1..N over `state='new'` (every mutation ends here).

    Done in Python on purpose: the `WITH o AS (… ROW_NUMBER() …) UPDATE …
    (SELECT rn FROM o …)` form of §4.1 re-evaluates the CTE per row in SQLite,
    so rows already renumbered change the ordering of the ones still to come and
    the result has duplicates (observed: 1,2,3,5,5). The stack is ≤ a few
    hundred rows, so a loop is both correct and sub-millisecond.
    """
    ids = [
        int(r["id"])
        for r in cx.execute(
            "SELECT id FROM srs_cards WHERE state='new' "
            "ORDER BY (queue_pos IS NULL), queue_pos, id"
        )
    ]
    for pos, card_id in enumerate(ids, start=1):
        cx.execute(
            "UPDATE srs_cards SET queue_pos=? WHERE id=? AND (queue_pos IS NOT ?)",
            (pos, card_id, pos),
        )
    cx.execute(
        "UPDATE srs_cards SET queue_pos=NULL, study_now=0 "
        "WHERE state<>'new' AND (queue_pos IS NOT NULL OR study_now<>0)"
    )


# `_renumber_stack` is the name §4.1 uses; keep both.
_renumber_stack = renumber_stack


def _leave_stack(cx, card_id: int) -> None:
    """Clear `queue_pos`/`study_now` when a card leaves `state='new'`."""
    cx.execute("UPDATE srs_cards SET queue_pos=NULL, study_now=0 WHERE id=?", (card_id,))


def _touch(cx, card_id: int) -> None:
    cx.execute("UPDATE srs_cards SET updated_at=datetime('now') WHERE id=?", (card_id,))


def _get_card(cx, card_id: int) -> dict:
    row = cx.execute("SELECT * FROM srs_cards WHERE id=?", (card_id,)).fetchone()
    if not row:
        raise SrsNotFound(f"card {card_id} not found")
    return dict(row)


# ---------------------------------------------------------------------------
# Serialisation (§7.1)
# ---------------------------------------------------------------------------

def _json_list(raw) -> list:
    if not raw:
        return []
    try:
        val = json.loads(raw)
    except Exception:
        return []
    return val if isinstance(val, list) else []


def _clip_dir(card_id: int):
    return settings.clips_dir / "srs" / str(card_id)


def _clip_urls(card: dict) -> tuple[Optional[str], Optional[str]]:
    """`clips.urls()` when WP-B is real; otherwise the §6.4 layout on disk."""
    try:
        from . import clips

        got = clips.urls(card)
        return got.get("video_url"), got.get("poster_url")
    except NotImplementedError:
        pass
    except Exception as e:                                  # pragma: no cover
        log.debug("clips.urls failed for card %s: %s", card.get("id"), e)
    if (card.get("clip_status") or "") != "ready":
        return None, None
    cid = card.get("id")
    d = _clip_dir(int(cid))
    v = int(card.get("clip_version") or 1)
    video = f"/clips/srs/{cid}/clip.mp4?v={v}" if (d / "clip.mp4").exists() else None
    poster = f"/clips/srs/{cid}/poster.jpg" if (d / "poster.jpg").exists() else None
    return video, poster


def _source_available(cx, episode_id: Optional[int], cache: Optional[dict] = None) -> bool:
    """The episode's video file exists on disk right now (never derived from
    `line_id` — a re-ingest nulls that while the video is still there, §2.7)."""
    if not episode_id:
        return False
    if cache is not None and episode_id in cache:
        return cache[episode_id]
    row = cx.execute("SELECT video_path FROM episodes WHERE id=?", (episode_id,)).fetchone()
    ok = bool(row and row["video_path"] and os.path.exists(row["video_path"]))
    if cache is not None:
        cache[episode_id] = ok
    return ok


def _card_model(
    cx,
    row,
    *,
    preview: Optional[dict] = None,
    source_cache: Optional[dict] = None,
) -> SrsCard:
    r = dict(row)
    tokens_doc = {}
    if r.get("tokens_json"):
        try:
            tokens_doc = json.loads(r["tokens_json"]) or {}
        except Exception:
            tokens_doc = {}
    tokens = [
        SrsToken(
            surface=t.get("s") or "",
            reading=t.get("r"),
            is_target=bool(t.get("t")),
            status=(t.get("k") or "UNKNOWN"),
        )
        for t in (tokens_doc.get("tokens") or [])
        if isinstance(t, dict)
    ]
    context = [
        SrsContextLine(
            line_id=c.get("line_id"),
            idx=int(c.get("idx") or 0),
            start_ms=int(c.get("start_ms") or 0),
            end_ms=int(c.get("end_ms") or 0),
            text=c.get("text") or "",
            text_furigana=c.get("text_furigana"),
            translation=c.get("translation"),
            is_target=bool(c.get("is_target")),
        )
        for c in _json_list(r.get("context_json"))
        if isinstance(c, dict)
    ]
    extend = [
        SrsLineRef(
            line_id=x.get("line_id"),
            idx=x.get("idx"),
            start_ms=int(x.get("start_ms") or 0),
            end_ms=int(x.get("end_ms") or 0),
            text=x.get("text") or "",
            # rows written before evidence lines existed carry no role: they are
            # the §5.4 continuation cue (never shown as a front line)
            role=("evidence" if x.get("role") == "evidence" else "continuation"),
        )
        for x in _json_list(r.get("extend_json"))
        if isinstance(x, dict)
    ]
    video_url, poster_url = _clip_urls(r)
    is_known = (r["state"] == "known") or (
        r["state"] == "review" and int(r["scheduled_days"] or 0) >= KNOWN_INTERVAL_DAYS
    )
    return SrsCard(
        id=r["id"],
        lemma=r["lemma"],
        reading=r["reading"],
        pos=r["pos"],
        gloss=r["gloss"],
        meaning_short=r["meaning_short"],
        meaning_full=r["meaning_full"],
        why_clear=r["why_clear"],
        usage_note=r["usage_note"],
        tags=[str(t) for t in _json_list(r.get("tags_json"))],
        freq_rank=r["freq_rank"],
        source=r["source"] if r["source"] in ("auto", "manual", "curated-initial", "confirm") else "auto",
        score=r["score"],
        clarity=r["clarity"],
        usefulness=r["usefulness"],
        priority=r["priority"],
        line_id=r["line_id"],
        episode_id=r["episode_id"],
        anilist_id=r["anilist_id"],
        show_title=r["show_title"],
        ep_number=r["ep_number"],
        start_ms=int(r["start_ms"] or 0),
        end_ms=int(r["end_ms"] or 0),
        text=r["text"] or "",
        text_furigana=r["text_furigana"],
        translation=r["translation"],
        translation_source=r["translation_source"] if r["translation_source"] in ("human", "mt", "user") else None,
        target_surface=r["target_surface"] or "",
        tokens=tokens,
        tokens_source=(tokens_doc.get("src") if tokens_doc.get("src") in ("migaku", "local") else None),
        context=context,
        extend=extend,
        alt_moment_ids=[int(x) for x in _json_list(r.get("alt_moment_ids_json")) if isinstance(x, int)],
        clip=SrsClip(
            status=r["clip_status"] or "pending",
            video_url=video_url,
            poster_url=poster_url,
            start_ms=r["clip_start_ms"],
            end_ms=r["clip_end_ms"],
            bytes=r["clip_bytes"],
            error=r["clip_error"],
            version=int(r["clip_version"] or 1),
        ),
        state=r["state"],
        queue_pos=r["queue_pos"],
        study_now=bool(r["study_now"]),
        buried_until=_iso(r["buried_until"]),
        stability=r["stability"],
        difficulty=r["difficulty"],
        step=r["step"],
        due_at=_iso(r["due_at"]),
        last_review_at=_iso(r["last_review_at"]),
        scheduled_days=int(r["scheduled_days"] or 0),
        reps=int(r["reps"] or 0),
        lapses=int(r["lapses"] or 0),
        fail_count=int(r["fail_count"] or 0),
        demoted_count=int(r["demoted_count"] or 0),
        demoted_at=_iso(r["demoted_at"]),
        introduced_at=_iso(r["introduced_at"]),
        known_source=r["known_source"] if r["known_source"] in ("srs", "user", "migaku") else None,
        known_at=_iso(r["known_at"]),
        suspend_reason=r["suspend_reason"],
        notes=r["notes"],
        is_known=is_known,
        source_available=_source_available(cx, r["episode_id"], source_cache),
        line_available=r["line_id"] is not None,
        created_at=_iso(r["created_at"]) or "",
        updated_at=_iso(r["updated_at"]) or "",
        preview=SrsIntervalPreview(**preview) if preview else None,
    )


def _card_out(cx, card_id: int) -> SrsCard:
    row = cx.execute("SELECT * FROM srs_cards WHERE id=?", (card_id,)).fetchone()
    if not row:
        raise SrsNotFound(f"card {card_id} not found")
    return _card_model(cx, row)


# ---------------------------------------------------------------------------
# Card creation (§5.7)
# ---------------------------------------------------------------------------

_INSERT_COLUMNS = (
    "lemma", "reading", "pos", "gloss", "meaning_short", "meaning_full", "why_clear",
    "usage_note", "tags_json", "freq_rank", "source", "score", "clarity", "usefulness",
    "priority", "line_id", "episode_id", "anilist_id", "show_title", "ep_number", "start_ms",
    "end_ms", "text", "norm_text", "text_furigana", "translation", "translation_source",
    "target_surface", "tokens_json", "context_json", "extend_json", "alt_moment_ids_json",
    "clip_status", "clip_requested_at", "clip_version", "state", "queue_pos", "study_now",
    "migaku_status_seen",
)


def _spec_values(cx, spec: CardSpec, *, queue_pos: Optional[int], study_now: bool, now: datetime) -> list:
    return [
        spec.lemma, spec.reading, spec.pos, spec.gloss, spec.meaning_short, spec.meaning_full,
        spec.why_clear, spec.usage_note,
        json.dumps(spec.tags or [], ensure_ascii=False),
        spec.freq_rank, spec.source, spec.score, spec.clarity, spec.usefulness, spec.priority,
        spec.line_id, spec.episode_id, spec.anilist_id, spec.show_title, spec.ep_number,
        int(spec.start_ms or 0), int(spec.end_ms or 0), spec.text or "",
        spec.norm_text or norm_text(spec.text or ""),
        spec.text_furigana, spec.translation, spec.translation_source, spec.target_surface or "",
        spec.tokens_json, spec.context_json,
        json.dumps(spec.extend or [], ensure_ascii=False),
        json.dumps(spec.alt_moment_ids or [], ensure_ascii=False),
        "pending" if spec.text else "no_source",
        _sql(now), 1, "new", queue_pos, 1 if study_now else 0,
        current_migaku_status(cx, spec.lemma),
    ]


def live_cards_of(cx, lemma: str) -> list[dict]:
    """The word's cards that count towards `MAX_CARDS_PER_LEMMA`: every real
    (non-`confirm`) card that is not `rejected`, oldest first."""
    return [dict(r) for r in cx.execute(
        "SELECT id, show_title, anilist_id, episode_id, norm_text, state, source FROM srs_cards "
        "WHERE lemma=? AND source<>'confirm' AND state<>'rejected' ORDER BY id",
        (lemma,),
    )]


def _same_moment_card(cx, lemma: str, episode_id, norm: str):
    return cx.execute(
        "SELECT id FROM srs_cards WHERE lemma=? AND episode_id IS ? AND norm_text=?",
        (lemma, episode_id, norm),
    ).fetchone()


def create_card(
    spec: CardSpec,
    *,
    position: Literal["top", "bottom"] | int = "bottom",
    study_now: bool = False,
    allow_same_franchise: bool = False,
) -> int:
    """INSERT the card; sets `migaku_status_seen`, `clip_requested_at`, enqueues
    `srs_clip {card_id, v:1}` at priority 30 and publishes the SSE nudge.
    Returns the card id (§5.7).

    Multi-card words (2026-09-03): a word may hold up to `MAX_CARDS_PER_LEMMA`
    independent sibling cards, each from a different anime (`franchise_key` of
    `show_title`). Idempotent per moment — `(lemma, episode_id, norm_text)` is
    unique, so the same line is never carded twice (the existing id is
    returned). A word that already has its `confirm`-known card keeps it (that
    id is returned). Raises `SrsConflict` when the word is full or the anime is
    already used; `allow_same_franchise=True` lifts the anime rule (manual cards
    are the learner's own choice).
    """
    now = _now()
    created = False
    norm = spec.norm_text or norm_text(spec.text or "")
    with _txn() as cx:
        same = _same_moment_card(cx, spec.lemma, spec.episode_id, norm)
        confirm = cx.execute(
            "SELECT id FROM srs_cards WHERE lemma=? AND source='confirm'", (spec.lemma,)
        ).fetchone()
        if same:
            card_id = int(same["id"])
        elif confirm:
            card_id = int(confirm["id"])
        else:
            live = live_cards_of(cx, spec.lemma)
            if len(live) >= MAX_CARDS_PER_LEMMA:
                raise SrsConflict(
                    f"this word already has {MAX_CARDS_PER_LEMMA} cards",
                    {"detail": f"this word already has {MAX_CARDS_PER_LEMMA} cards",
                     "card_id": int(live[0]["id"])},
                )
            if live and not allow_same_franchise:
                fr = franchise_key(spec.show_title)
                for r in live:
                    if fr and franchise_key(r["show_title"]) == fr:
                        raise SrsConflict(
                            "this word already has a card from this anime",
                            {"detail": "this word already has a card from this anime",
                             "card_id": int(r["id"])},
                        )
            if position == "top":
                queue_pos = 0
            elif position == "bottom":
                queue_pos = stack_bottom(cx)
            else:
                queue_pos = int(position)
            cols = ", ".join(_INSERT_COLUMNS)
            marks = ", ".join("?" * len(_INSERT_COLUMNS))
            cur = cx.execute(
                f"INSERT INTO srs_cards ({cols}) VALUES ({marks}) "
                f"ON CONFLICT(lemma, episode_id, norm_text) DO NOTHING",
                _spec_values(cx, spec, queue_pos=queue_pos, study_now=study_now, now=now),
            )
            if cur.rowcount == 0:
                # a concurrent writer inserted the same moment between the SELECT
                # above (autocommit read — no protection) and this INSERT;
                # lastrowid would be 0 and poison srs_words / the clip job.
                card_id = int(_same_moment_card(cx, spec.lemma, spec.episode_id, norm)["id"])
                created = False
            else:
                card_id = int(cur.lastrowid)
                created = True
            # `srs_words.card_id` stays the PRIMARY (first) card of the word.
            cx.execute(
                "INSERT INTO srs_words(lemma, reading, gloss, freq_rank, judge_status, card_id, updated_at) "
                "VALUES(?,?,?,?, 'accepted', ?, datetime('now')) "
                "ON CONFLICT(lemma) DO UPDATE SET judge_status='accepted', "
                "card_id=COALESCE(srs_words.card_id, excluded.card_id), "
                "updated_at=datetime('now')",
                (spec.lemma, spec.reading, spec.gloss, spec.freq_rank, card_id),
            )
            renumber_stack(cx)
    if created:
        if spec.text and spec.line_id:
            _enqueue("srs_clip", {"card_id": card_id, "v": 1}, priority=30)
        _publish("card", card_id=card_id)
    return card_id


# ---------------------------------------------------------------------------
# Moments, relink and swapping (§2.7, §4.2)
# ---------------------------------------------------------------------------

def relink_episode(cx, episode_id: int) -> int:
    """Re-attach NULL `line_id`s on `srs_cards`/`srs_moments` by
    `(episode_id, norm_text, start_ms±RELINK_TOLERANCE_MS)` after a re-ingest
    (§2.7). Returns the number of rows fixed."""
    if not episode_id:
        return 0
    cards = cx.execute(
        "SELECT id, norm_text, start_ms FROM srs_cards WHERE episode_id=? AND line_id IS NULL",
        (episode_id,),
    ).fetchall()
    moments = cx.execute(
        "SELECT id, norm_text, start_ms FROM srs_moments WHERE episode_id=? AND line_id IS NULL",
        (episode_id,),
    ).fetchall()
    if not cards and not moments:
        return 0
    lines = cx.execute(
        "SELECT id, idx, start_ms, end_ms, text, text_furigana FROM subtitle_lines WHERE episode_id=?",
        (episode_id,),
    ).fetchall()
    if not lines:
        return 0
    by_norm: dict[str, list] = {}
    for ln in lines:
        by_norm.setdefault(norm_text(ln["text"] or ""), []).append(ln)

    def _best(nt: str, start_ms: int):
        rows = by_norm.get(nt or "")
        if not rows:
            return None
        return min(rows, key=lambda r: abs(int(r["start_ms"] or 0) - int(start_ms or 0)))

    fixed = 0
    for c in cards:
        ln = _best(c["norm_text"], c["start_ms"])
        if not ln:
            continue
        cx.execute(
            "UPDATE srs_cards SET line_id=?, start_ms=?, end_ms=?, "
            "text_furigana=COALESCE(?, text_furigana), updated_at=datetime('now') WHERE id=?",
            (ln["id"], ln["start_ms"], ln["end_ms"], ln["text_furigana"], c["id"]),
        )
        fixed += 1
    for m in moments:
        ln = _best(m["norm_text"], m["start_ms"])
        if not ln:
            continue
        cx.execute(
            "UPDATE srs_moments SET line_id=?, start_ms=?, end_ms=?, idx=? WHERE id=?",
            (ln["id"], ln["start_ms"], ln["end_ms"], ln["idx"], m["id"]),
        )
        fixed += 1
    if fixed:
        log.info("relink episode %s: %d rows re-attached", episode_id, fixed)
    return fixed


def _resolve_line(cx, episode_id: Optional[int], text: str, start_ms: int) -> Optional[int]:
    """Find a subtitle line by content (§5.11.2 / §2.7): same episode, same
    `norm_text`, nearest `start_ms`."""
    if not episode_id:
        return None
    nt = norm_text(text or "")
    if not nt:
        return None
    best, best_delta = None, None
    for ln in cx.execute(
        "SELECT id, start_ms, text FROM subtitle_lines WHERE episode_id=?", (episode_id,)
    ):
        if norm_text(ln["text"] or "") != nt:
            continue
        delta = abs(int(ln["start_ms"] or 0) - int(start_ms or 0))
        if best_delta is None or delta < best_delta:
            best, best_delta = int(ln["id"]), delta
    if best is not None and best_delta is not None and best_delta > 5 * RELINK_TOLERANCE_MS:
        log.debug("resolved line %s with a %d ms offset", best, best_delta)
    return best


def _moment_evidence(moment) -> list[int]:
    """`srs_moments.evidence_line_ids_json` as a list of ints (§6.2). Rows judged
    before evidence lines existed have NULL → `[]`."""
    try:
        raw = moment["evidence_line_ids_json"]
    except (KeyError, IndexError, TypeError):
        return []
    return [int(x) for x in _json_list(raw) if isinstance(x, int)]


def _primary_moment_id(cx, card: dict) -> Optional[int]:
    row = cx.execute(
        "SELECT id FROM srs_moments WHERE lemma=? AND episode_id=? AND norm_text=?",
        (card["lemma"], card["episode_id"], card["norm_text"]),
    ).fetchone()
    return int(row["id"]) if row else None


def _apply_snapshot(cx, card_id: int, snap: dict, *, bump_clip: bool, now: datetime) -> None:
    cx.execute(
        "UPDATE srs_cards SET line_id=?, episode_id=?, anilist_id=?, show_title=?, ep_number=?, "
        "start_ms=?, end_ms=?, text=?, norm_text=?, text_furigana=?, translation=?, "
        "translation_source=?, target_surface=?, tokens_json=?, context_json=?, extend_json=?, "
        + ("clip_version=clip_version+1, clip_status='pending', clip_requested_at=?, "
           "clip_error=NULL, clip_bytes=NULL, " if bump_clip else "")
        + "updated_at=datetime('now') WHERE id=?",
        (
            snap["line_id"], snap["episode_id"], snap["anilist_id"], snap["show_title"],
            snap["ep_number"], snap["start_ms"], snap["end_ms"], snap["text"], snap["norm_text"],
            snap["text_furigana"], snap["translation"], snap["translation_source"],
            snap["target_surface"], snap["tokens_json"], snap["context_json"],
            json.dumps(snap.get("extend") or [], ensure_ascii=False),
            *(( _sql(now),) if bump_clip else ()),
            card_id,
        ),
    )


def _keep_prev_clip(card_id: int) -> None:
    """Move `<clips_dir>/srs/<id>/` aside as `<…>.prev/` (demotion swap, §3.7.3)."""
    src = _clip_dir(card_id)
    dst = src.with_name(src.name + ".prev")
    try:
        if src.exists():
            if dst.exists():
                shutil.rmtree(dst, ignore_errors=True)
            src.rename(dst)
    except Exception as e:                                  # pragma: no cover
        log.warning("could not keep previous clip of card %s: %s", card_id, e)


def _restore_prev_clip(card_id: int) -> bool:
    try:
        from . import clips

        return bool(clips.restore_prev(card_id))
    except NotImplementedError:
        pass
    except Exception as e:                                  # pragma: no cover
        log.debug("clips.restore_prev failed: %s", e)
    src = _clip_dir(card_id)
    prev = src.with_name(src.name + ".prev")
    try:
        if prev.exists():
            if src.exists():
                shutil.rmtree(src, ignore_errors=True)
            prev.rename(src)
            return True
    except Exception as e:                                  # pragma: no cover
        log.warning("could not restore previous clip of card %s: %s", card_id, e)
    return False


def _remove_clip(card_id: int) -> None:
    try:
        from . import clips

        clips.remove(card_id)
        return
    except NotImplementedError:
        pass
    except Exception as e:                                  # pragma: no cover
        log.debug("clips.remove failed: %s", e)
    for d in (_clip_dir(card_id), _clip_dir(card_id).with_name(f"{card_id}.prev")):
        shutil.rmtree(d, ignore_errors=True)


def swap_moment(cx, card_id: int, moment_id: int, *, keep_prev: bool = False) -> None:
    """Re-snapshot the card onto another moment: bumps `clip_version`, sets
    `clip_status='pending'`, enqueues `srs_clip {card_id, v}`; the previous
    moment id goes to the front of `alt_moment_ids_json`. `keep_prev=True` is the
    demotion swap (§3.7) — the old clip stays until the new one is ready."""
    now = _now()
    card = _get_card(cx, card_id)
    m = cx.execute("SELECT * FROM srs_moments WHERE id=?", (moment_id,)).fetchone()
    if not m:
        raise SrsNotFound(f"moment {moment_id} not found")
    if m["lemma"] != card["lemma"]:
        raise SrsConflict("moment belongs to another word")
    # a moment another card of this word already shows is not a swap target —
    # two cards of one word on one line are one card (multi-card words,
    # 2026-09-03; the UNIQUE(lemma, episode_id, norm_text) index agrees)
    other = cx.execute(
        "SELECT id, state FROM srs_cards WHERE lemma=? AND episode_id=? AND norm_text=? "
        "AND id<>? AND source<>'confirm' ORDER BY id LIMIT 1",
        (card["lemma"], m["episode_id"], m["norm_text"] or "", card_id),
    ).fetchone()
    if other:
        raise SrsConflict(
            f"this moment is already card #{int(other['id'])} of this word",
            {"detail": f"this moment is already card #{int(other['id'])} of this word",
             "card_id": int(other["id"]), "state": other["state"]},
        )
    line_id = m["line_id"]
    if line_id is None:
        relink_episode(cx, m["episode_id"])
        m = cx.execute("SELECT * FROM srs_moments WHERE id=?", (moment_id,)).fetchone()
        line_id = m["line_id"]
    if line_id is None:
        raise SrsConflict("moment's subtitle line is gone")

    # the moment remembers which neighbours its meaning depends on (§6.2): the
    # swapped-in clip must cover them and the front must show them.
    snap = snapshot_line(line_id, card["lemma"], m["target_surface"], reading=card["reading"],
                         evidence_line_ids=_moment_evidence(m))
    prev_id = _primary_moment_id(cx, card)
    alts = [int(x) for x in _json_list(card.get("alt_moment_ids_json")) if isinstance(x, int)]
    alts = [a for a in alts if a != moment_id]
    if prev_id and prev_id != moment_id:
        alts = [prev_id] + [a for a in alts if a != prev_id]

    if keep_prev:
        _keep_prev_clip(card_id)
    _apply_snapshot(cx, card_id, snap, bump_clip=True, now=now)
    cx.execute(
        "UPDATE srs_cards SET alt_moment_ids_json=? WHERE id=?",
        (json.dumps(alts, ensure_ascii=False), card_id),
    )
    version = int(cx.execute("SELECT clip_version FROM srs_cards WHERE id=?", (card_id,)).fetchone()["clip_version"])
    _enqueue("srs_clip", {"card_id": card_id, "v": version}, priority=30)
    _publish("card", card_id=card_id)


def _accepted_alternate(cx, card: dict) -> Optional[int]:
    """The best accepted alternate moment with a video on disk (§3.7.3)."""
    # never the moment another live card of the same word already shows
    # (sibling cards, 2026-09-03)
    rows = cx.execute(
        "SELECT m.* FROM srs_moments m WHERE m.lemma=? AND m.accepted=1 "
        "AND NOT (m.episode_id=? AND m.norm_text=?) "
        "AND NOT EXISTS (SELECT 1 FROM srs_cards c WHERE c.lemma=m.lemma AND c.id<>? "
        "                AND c.state<>'rejected' AND c.episode_id=m.episode_id "
        "                AND c.norm_text=m.norm_text) "
        "ORDER BY COALESCE(m.clarity,0) DESC, COALESCE(m.line_score,0) DESC",
        (card["lemma"], card["episode_id"], card["norm_text"], int(card["id"])),
    ).fetchall()
    for m in rows:
        if not _source_available(cx, m["episode_id"]):
            continue
        if m["line_id"] is None:
            relink_episode(cx, m["episode_id"])
            fresh = cx.execute("SELECT line_id FROM srs_moments WHERE id=?", (m["id"],)).fetchone()
            if not fresh or fresh["line_id"] is None:
                continue
        return int(m["id"])
    return None


# ---------------------------------------------------------------------------
# Queue (§3.6)
# ---------------------------------------------------------------------------

def _counts_today(cx, day: str) -> tuple[int, int, int]:
    """(introduced, review-state ratings, total ratings) today, undone excluded."""
    row = cx.execute(
        "SELECT COUNT(*) AS total, "
        "SUM(CASE WHEN state_before='new' THEN 1 ELSE 0 END) AS intro, "
        "SUM(CASE WHEN state_before='review' THEN 1 ELSE 0 END) AS reviews "
        "FROM srs_reviews WHERE review_day=? AND undone=0",
        (day,),
    ).fetchone()
    return int(row["intro"] or 0), int(row["reviews"] or 0), int(row["total"] or 0)


_NOT_BURIED = "(buried_until IS NULL OR buried_until <= ?)"


def queue(limit: int = 20, extra_new: int = 0) -> SrsQueue:
    """`GET /srs/queue` — §3.6 composition with `preview` filled."""
    now = _now()
    now_sql = _sql(now)
    day = _day(now)
    cfg = get_settings()
    cx = connect()
    try:
        introduced, reviewed_review, reviewed_today = _counts_today(cx, day)
        learning = cx.execute(
            "SELECT * FROM srs_cards WHERE state IN ('learning','relearning') AND due_at <= ? "
            f"AND {_NOT_BURIED} ORDER BY due_at ASC, id ASC LIMIT ?",
            (now_sql, now_sql, max(limit, 1) * 4),
        ).fetchall()
        review_cap = max(0, MAX_REVIEWS_PER_DAY - reviewed_review)
        reviews = cx.execute(
            "SELECT * FROM srs_cards WHERE state='review' AND due_at <= ? "
            f"AND {_NOT_BURIED} ORDER BY due_at ASC, id ASC LIMIT ?",
            (now_sql, now_sql, min(review_cap, max(limit, 1) * 4)),
        ).fetchall() if review_cap else []

        allowance = max(0, int(cfg.new_per_day) - introduced) + max(0, int(extra_new))
        study_now_rows = cx.execute(
            "SELECT * FROM srs_cards WHERE state='new' AND study_now=1 AND text <> '' "
            f"AND {_NOT_BURIED} ORDER BY queue_pos ASC, id ASC LIMIT ?",
            (now_sql, max(limit, 1)),
        ).fetchall()
        seen = {r["id"] for r in study_now_rows}
        rest = cx.execute(
            "SELECT * FROM srs_cards WHERE state='new' AND study_now=0 AND text <> '' "
            f"AND {_NOT_BURIED} "
            "ORDER BY (clip_status <> 'ready'), queue_pos ASC, id ASC LIMIT ?",
            (now_sql, max(allowance, 0)),
        ).fetchall() if allowance else []
        news = list(study_now_rows) + [r for r in rest if r["id"] not in seen]

        # merge: learning first, then R R R N
        merged = list(learning)
        i = j = 0
        while i < len(reviews) or j < len(news):
            for _ in range(3):
                if i < len(reviews):
                    merged.append(reviews[i])
                    i += 1
            if j < len(news):
                merged.append(news[j])
                j += 1
        merged = merged[: max(1, limit)]

        soon_cut = _sql(now + timedelta(minutes=LEARN_AHEAD_MIN))
        soon = cx.execute(
            "SELECT * FROM srs_cards WHERE state IN ('learning','relearning') "
            f"AND due_at > ? AND due_at <= ? AND {_NOT_BURIED} ORDER BY due_at ASC LIMIT 20",
            (now_sql, soon_cut, now_sql),
        ).fetchall()

        cache: dict = {}
        cards = [
            _card_model(cx, r, preview=scheduler.preview(dict(r), now), source_cache=cache)
            for r in merged
        ]
        soon_cards = [
            _card_model(cx, r, preview=scheduler.preview(dict(r), now), source_cache=cache)
            for r in soon
        ]
        counts = SrsQueueCounts(
            learning_due=int(cx.execute(
                "SELECT COUNT(*) n FROM srs_cards WHERE state IN ('learning','relearning') "
                f"AND due_at <= ? AND {_NOT_BURIED}", (now_sql, now_sql)).fetchone()["n"]),
            review_due=int(cx.execute(
                "SELECT COUNT(*) n FROM srs_cards WHERE state='review' AND due_at <= ? "
                f"AND {_NOT_BURIED}", (now_sql, now_sql)).fetchone()["n"]),
            new_left_today=max(0, int(cfg.new_per_day) - introduced),
            new_stack_total=int(cx.execute(
                "SELECT COUNT(*) n FROM srs_cards WHERE state='new'").fetchone()["n"]),
            reviewed_today=reviewed_today,
            clips_pending=int(cx.execute(
                "SELECT COUNT(*) n FROM srs_cards WHERE state='new' AND clip_status='pending'"
            ).fetchone()["n"]),
        )
    finally:
        cx.close()
    return SrsQueue(
        cards=cards, learning_soon=soon_cards,
        server_time=_iso(_sql(now)) or "", day=day, counts=counts,
    )


# ---------------------------------------------------------------------------
# Review (§3.7, §3.10)
# ---------------------------------------------------------------------------

def _is_known_row(state: str, scheduled_days: int) -> bool:
    return state == "known" or (state == "review" and int(scheduled_days or 0) >= KNOWN_INTERVAL_DAYS)


def _before_json(card: dict, snapshot_backup: Optional[dict] = None) -> str:
    doc = {k: card.get(k) for k in BEFORE_COLUMNS}
    if snapshot_backup:
        doc["snapshot_backup"] = snapshot_backup
    return json.dumps(doc, ensure_ascii=False)


def _counted_fail(card: dict, rating: int, now: datetime, today: str) -> bool:
    """§3.7 — an `Again` the learner slept on, counted at most once per SRS day."""
    if rating != 1:
        return False
    if card["state"] not in ("learning", "review", "relearning"):
        return False
    last = _dt(card.get("last_review_at"))
    if last is None:
        return False
    if scheduler.srs_day(last).isoformat() >= today:
        return False
    if (now - last) < timedelta(hours=FAIL_MIN_GAP_HOURS):
        return False
    return (card.get("last_fail_day") or "") != today


def _demote(cx, card: dict, now: datetime, trigger: str) -> dict:
    """§3.7 — back to the stack: bottom, scheduler wiped, fresh moment.

    Returns `{"state", "suspended", "swapped"}`; the caller writes the review row.
    """
    card_id = int(card["id"])
    demoted_count = int(card["demoted_count"] or 0) + 1
    parked = demoted_count >= MAX_DEMOTIONS
    if parked:
        cx.execute(
            "UPDATE srs_cards SET state='suspended', suspend_reason='auto_demotions', "
            "state_before_suspend='new', queue_pos=NULL, study_now=0, stability=NULL, "
            "difficulty=NULL, step=NULL, due_at=NULL, scheduled_days=0, fail_count=0, "
            "last_fail_day=NULL, last_review_at=NULL, introduced_at=NULL, demoted_count=?, "
            "demoted_at=?, updated_at=datetime('now') WHERE id=?",
            (demoted_count, _sql(now), card_id),
        )
    else:
        cx.execute(
            "UPDATE srs_cards SET state='new', queue_pos=?, study_now=0, stability=NULL, "
            "difficulty=NULL, step=NULL, due_at=NULL, scheduled_days=0, fail_count=0, "
            "last_fail_day=NULL, last_review_at=NULL, introduced_at=NULL, demoted_count=?, "
            "demoted_at=?, updated_at=datetime('now') WHERE id=?",
            (stack_bottom(cx), demoted_count, _sql(now), card_id),
        )
    swapped = False
    if DEMOTE_SWAP_MOMENT and not parked:
        alt = _accepted_alternate(cx, card)
        if alt:
            try:
                swap_moment(cx, card_id, alt, keep_prev=True)
                swapped = True
            except (SrsConflict, SrsNotFound, LookupError, ValueError) as e:
                log.info("demotion moment swap skipped for card %s: %s", card_id, e)
    renumber_stack(cx)
    log.info("card %s demoted (%s), count=%d parked=%s", card_id, trigger, demoted_count, parked)
    return {"state": "suspended" if parked else "new", "suspended": parked, "swapped": swapped}


def _stored_review_result(cx, row) -> SrsReviewResult:
    card = _card_out(cx, int(row["card_id"]))
    return SrsReviewResult(
        card=card,
        review_id=int(row["id"]),
        duplicate=True,
        demoted=bool(row["demoted"]),
        suspended=card.state == "suspended",
        graduated=row["state_before"] in ("new", "learning") and row["state_after"] == "review",
        became_known=False,
        known_crossed=None,
        next_due_at=card.due_at,
        message=None,
    )


def review(req: SrsReviewRequest) -> SrsReviewResult:
    """`POST /srs/review` — one transaction: schedule, demotion rule, stack exit,
    history. Repeated `client_id` returns the stored result with
    `duplicate=True`; a card in an inactive state raises `SrsConflict` carrying
    an `SrsReviewConflict` body (§3.10)."""
    now = _now()
    today = _day(now)
    cfg = get_settings()
    crossed: Optional[str] = None
    topup = False
    with _txn() as cx:
        if req.client_id:
            dup = cx.execute(
                "SELECT * FROM srs_reviews WHERE client_id=?", (req.client_id,)
            ).fetchone()
            if dup:
                return _stored_review_result(cx, dup)

        card = _get_card(cx, req.card_id)
        if card["state"] not in ACTIVE_STATES:
            raise SrsConflict(
                f"card is {card['state']}",
                SrsReviewConflict(
                    detail=f"card is {card['state']}", card=_card_model(cx, card)
                ).model_dump(),
            )

        state_before = card["state"]
        was_known = _is_known_row(state_before, card["scheduled_days"])
        sched = scheduler.schedule(card, int(req.rating), now, rng=random.Random())

        counted_fail = _counted_fail(card, int(req.rating), now, today)
        fail_count = int(card["fail_count"] or 0)
        last_fail_day = card["last_fail_day"]
        if counted_fail:
            fail_count += 1
            last_fail_day = today
        if int(req.rating) != 1 and int(sched["scheduled_days"]) >= KNOWN_INTERVAL_DAYS:
            fail_count = 0

        threshold = int(cfg.demote_after_fails)
        demote = bool(threshold > 0 and counted_fail and fail_count >= threshold)

        snapshot_backup = None
        if demote:
            snapshot_backup = {k: card.get(k) for k in SNAPSHOT_COLUMNS}
        before_json = _before_json(card, snapshot_backup)

        introduced_at = card["introduced_at"] or (_sql(now) if state_before == "new" else None)
        cx.execute(
            "UPDATE srs_cards SET state=?, step=?, stability=?, difficulty=?, due_at=?, "
            "scheduled_days=?, last_review_at=?, reps=?, lapses=?, fail_count=?, last_fail_day=?, "
            "introduced_at=?, updated_at=datetime('now') WHERE id=?",
            (
                sched["state"], sched["step"], sched["stability"], sched["difficulty"],
                _sql(sched["due_at"]), int(sched["scheduled_days"]), _sql(now),
                int(sched["reps"]), int(sched["lapses"]), fail_count, last_fail_day,
                introduced_at, req.card_id,
            ),
        )
        _leave_stack(cx, req.card_id)

        demotion = None
        if demote:
            card_for_demote = _get_card(cx, req.card_id)
            card_for_demote["demoted_count"] = card["demoted_count"]
            demotion = _demote(cx, card_for_demote, now, "review")
        renumber_stack(cx)

        after = _get_card(cx, req.card_id)
        is_known = _is_known_row(after["state"], after["scheduled_days"])
        if is_known and not was_known:
            crossed = "up"
        elif was_known and not is_known:
            crossed = "down"

        cur = cx.execute(
            "INSERT INTO srs_reviews(card_id, client_id, reviewed_at, review_day, rating, "
            "state_before, state_after, scheduled_days, elapsed_ms, counted_fail, demoted, "
            "undone, before_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,0,?)",
            (
                req.card_id, req.client_id or None, _sql(now), today, int(req.rating),
                state_before, after["state"], int(after["scheduled_days"] or 0),
                req.elapsed_ms, 1 if counted_fail else 0, 1 if demote else 0, before_json,
            ),
        )
        review_id = int(cur.lastrowid)

        if state_before == "new":
            n_new = int(cx.execute(
                "SELECT COUNT(*) n FROM srs_cards WHERE state='new'").fetchone()["n"])
            topup = n_new < STACK_MIN

        graduated = state_before in ("new", "learning") and after["state"] == "review"
        message = None
        if demotion:
            if demotion["suspended"]:
                message = f"{after['lemma']} → parked after {MAX_DEMOTIONS} returns"
            else:
                message = (
                    f"{after['lemma']} → back to the stack "
                    f"(missed on {fail_count} different days)"
                )
        result = SrsReviewResult(
            card=_card_model(cx, after),
            review_id=review_id,
            duplicate=False,
            demoted=bool(demotion),
            suspended=bool(demotion and demotion["suspended"]),
            graduated=graduated,
            became_known=crossed == "up",
            known_crossed=crossed,
            next_due_at=_iso(after["due_at"]),
            message=message,
        )
    _publish("card", card_id=int(req.card_id))
    if crossed:
        _recompute_comprehension()
    if topup:
        try:
            _enqueue("srs_generate", {"trigger": "auto"}, priority=60, delay_seconds=60)
        except Exception as e:                              # pragma: no cover
            log.warning("could not enqueue top-up generation: %s", e)
    return result


def undo_review(review_id: Optional[int] = None) -> Any:
    """`POST /srs/review/undo` — restore `before_json` (§3.11). `None` = the
    newest non-undone review of today. Returns `SrsUndoResult`."""
    now = _now()
    today = _day(now)
    crossed = False
    with _txn() as cx:
        if review_id is None:
            row = cx.execute(
                "SELECT * FROM srs_reviews WHERE review_day=? AND undone=0 ORDER BY id DESC LIMIT 1",
                (today,),
            ).fetchone()
        else:
            row = cx.execute(
                "SELECT * FROM srs_reviews WHERE id=? AND review_day=? AND undone=0",
                (review_id, today),
            ).fetchone()
        if not row:
            raise SrsNotFound("nothing to undo today")
        later = cx.execute(
            "SELECT COUNT(*) n FROM srs_reviews WHERE card_id=? AND id>? AND undone=0",
            (row["card_id"], row["id"]),
        ).fetchone()["n"]
        if later:
            raise SrsConflict("this card has a newer review")

        card = _get_card(cx, int(row["card_id"]))
        was_known = _is_known_row(card["state"], card["scheduled_days"])
        try:
            before = json.loads(row["before_json"]) or {}
        except Exception:
            before = {}
        sets, vals = [], []
        for col in BEFORE_COLUMNS:
            if col in before:
                sets.append(f"{col}=?")
                vals.append(before[col])
        snap = before.get("snapshot_backup")
        if isinstance(snap, dict):
            for col in SNAPSHOT_COLUMNS:
                if col in snap:
                    sets.append(f"{col}=?")
                    vals.append(snap[col])
        if sets:
            cx.execute(
                f"UPDATE srs_cards SET {', '.join(sets)}, updated_at=datetime('now') WHERE id=?",
                (*vals, card["id"]),
            )
        if isinstance(snap, dict) and not _restore_prev_clip(int(card["id"])):
            # The review swapped the moment and the kept `<dir>.prev` is gone —
            # the new clip already committed over it (`clips._job_clip` drops
            # `.prev` when it lands). Re-cut the restored moment rather than
            # leaving the card pointing at a clip of the wrong line.
            cx.execute(
                "UPDATE srs_cards SET clip_status='pending', clip_version=clip_version+1, "
                "clip_requested_at=?, clip_error=NULL WHERE id=? AND text <> ''",
                (_sql(now), card["id"]),
            )
            fresh = cx.execute(
                "SELECT clip_version, text FROM srs_cards WHERE id=?", (card["id"],)
            ).fetchone()
            if fresh and fresh["text"]:
                _enqueue("srs_clip", {"card_id": int(card["id"]), "v": int(fresh["clip_version"])},
                         priority=30)
        cx.execute("UPDATE srs_reviews SET undone=1 WHERE id=?", (row["id"],))
        renumber_stack(cx)
        after = _get_card(cx, int(card["id"]))
        crossed = _is_known_row(after["state"], after["scheduled_days"]) != was_known
        out = SrsUndoResult(card=_card_model(cx, after), undone_review_id=int(row["id"]))
    if crossed:
        _recompute_comprehension()
    _publish("card", card_id=int(out.card.id))
    return out


# ---------------------------------------------------------------------------
# Card listing & detail (§7.2)
# ---------------------------------------------------------------------------

_STATE_FILTERS = {
    "all": None,
    "new": ("state='new'", ()),
    "studying": ("state IN ('learning','review','relearning')", ()),
    "learning": ("state='learning'", ()),
    "review": ("state='review'", ()),
    "relearning": ("state='relearning'", ()),
    "known": ("state='known'", ()),
    "suspended": ("state='suspended'", ()),
    "rejected": ("state='rejected'", ()),
    "parked": ("state IN ('suspended','rejected')", ()),
}

_SORTS = {
    "queue": "queue_pos IS NULL, queue_pos {o}, id {o}",
    "due": "due_at IS NULL, due_at {o}, id {o}",
    "created": "created_at {o}, id {o}",
    "lemma": "lemma {o}",
    "score": "score IS NULL, score {o2}, id {o}",
    "rank": "freq_rank IS NULL, freq_rank {o}, id {o}",
}


def list_cards(
    state: str = "all",
    q: str = "",
    sort: str = "queue",
    order: str = "asc",
    limit: int = 50,
    offset: int = 0,
) -> SrsCardList:
    """`GET /srs/cards` — `limit=0` means no limit (the Stack tab loads the whole
    stack); `state` accepts the grouped values `studying`/`parked`."""
    where, params = [], []
    flt = _STATE_FILTERS.get(state or "all", "missing")
    if flt == "missing":
        raise SrsInvalid(f"unknown state filter {state!r}")
    if flt:
        where.append(flt[0])
        params.extend(flt[1])
    if q:
        like = f"%{q.strip()}%"
        where.append(
            "(lemma LIKE ? OR reading LIKE ? OR gloss LIKE ? OR meaning_short LIKE ? "
            "OR text LIKE ? OR show_title LIKE ?)"
        )
        params.extend([like] * 6)
    sql_where = (" WHERE " + " AND ".join(where)) if where else ""
    o = "DESC" if (order or "asc").lower() == "desc" else "ASC"
    o2 = "ASC" if o == "DESC" else "DESC"          # score/rank read best descending
    order_by = _SORTS.get(sort or "queue", _SORTS["queue"]).format(o=o, o2=o2)
    cx = connect()
    try:
        total = int(cx.execute(f"SELECT COUNT(*) n FROM srs_cards{sql_where}", params).fetchone()["n"])
        sql = f"SELECT * FROM srs_cards{sql_where} ORDER BY {order_by}"
        args = list(params)
        if limit and limit > 0:
            sql += " LIMIT ? OFFSET ?"
            args += [int(limit), int(offset)]
        elif offset:
            sql += " LIMIT -1 OFFSET ?"
            args += [int(offset)]
        cache: dict = {}
        items = [_card_model(cx, r, source_cache=cache) for r in cx.execute(sql, args)]
    finally:
        cx.close()
    return SrsCardList(items=items, total=total)


def _moment_model(cx, m, card: dict, cache: dict) -> SrsMoment:
    ep = cx.execute(
        "SELECT e.ep_number, t.romaji, t.english FROM episodes e "
        "LEFT JOIN titles t ON t.anilist_id=e.anilist_id WHERE e.id=?",
        (m["episode_id"],),
    ).fetchone()
    is_primary = (
        m["episode_id"] == card["episode_id"] and (m["norm_text"] or "") == (card["norm_text"] or "")
    )
    # the moment's context as a swap would carry it (2026-09-03): same-cue half
    # + the evidence lines recorded for it, plus the unpadded span for previews
    extend_refs: list[SrsLineRef] = []
    win_start: Optional[int] = None
    win_end: Optional[int] = None
    line_idx: Optional[int] = None
    if m["line_id"]:
        from .snapshot import build_extend, dialogue_neighbours, strip_bidi

        lr = cx.execute(
            "SELECT id, episode_id, idx, start_ms, end_ms, text FROM subtitle_lines WHERE id=?",
            (m["line_id"],),
        ).fetchone()
        if lr:
            line_idx = lr["idx"]
            line = {"line_id": lr["id"], "episode_id": lr["episode_id"], "idx": lr["idx"],
                    "start_ms": int(lr["start_ms"] or 0), "end_ms": int(lr["end_ms"] or 0),
                    "text": strip_bidi(lr["text"] or "")}
            try:
                before, after = dialogue_neighbours(cx, line, 2)
                entries, _tr = build_extend(
                    cx, line, _moment_evidence(m),
                    next_line=after[0] if after else None,
                    allowed_ids={n["line_id"] for n in (*before, *after) if n.get("line_id")},
                )
            except Exception as e:                       # never fail the detail over context
                log.debug("moment %s context unavailable: %s", m["id"], e)
                entries = []
            extend_refs = [
                SrsLineRef(line_id=x.get("line_id"), idx=x.get("idx"),
                           start_ms=int(x.get("start_ms") or 0), end_ms=int(x.get("end_ms") or 0),
                           text=x.get("text") or "", role=x.get("role") or "continuation")
                for x in entries if isinstance(x, dict)
            ]
            win_start = min([line["start_ms"]] + [r.start_ms for r in extend_refs])
            win_end = max([line["end_ms"]] + [r.end_ms for r in extend_refs])
    other = cx.execute(
        "SELECT id, state FROM srs_cards WHERE lemma=? AND episode_id=? AND norm_text=? "
        "AND id<>? AND source<>'confirm' ORDER BY id LIMIT 1",
        (m["lemma"], m["episode_id"], m["norm_text"] or "", int(card["id"])),
    ).fetchone()
    return SrsMoment(
        used_by_card_id=int(other["id"]) if other else None,
        used_by_state=other["state"] if other else None,
        idx=line_idx,
        extend=extend_refs,
        window_start_ms=win_start,
        window_end_ms=win_end,
        id=int(m["id"]),
        lemma=m["lemma"],
        line_id=m["line_id"],
        episode_id=int(m["episode_id"]),
        show_title=(ep["romaji"] or ep["english"]) if ep else None,
        ep_number=ep["ep_number"] if ep else None,
        text=m["text"] or "",
        text_furigana=None,
        translation=m["translation"],
        translation_source=m["translation_source"] if m["translation_source"] in ("human", "mt", "user") else None,
        translation_shared=bool(m["translation_shared"]),
        start_ms=int(m["start_ms"] or 0),
        end_ms=int(m["end_ms"] or 0),
        has_video=_source_available(cx, m["episode_id"], cache),
        target_surface=m["target_surface"],
        other_unknowns=m["other_unknowns"],
        line_score=m["line_score"],
        clarity=m["clarity"],
        translation_renders_word=None if m["translation_renders_word"] is None else bool(m["translation_renders_word"]),
        clean_utterance=None if m["clean_utterance"] is None else bool(m["clean_utterance"]),
        accepted=None if m["accepted"] is None else bool(m["accepted"]),
        verdict=m["verdict"] if m["verdict"] in ("accept", "reject", "user", "user_rejected") else None,
        note=m["note"],
        judged_at=_iso(m["judged_at"]),
        is_primary=is_primary,
    )


def card_detail(card_id: int) -> SrsCardDetail:
    """`GET /srs/cards/{id}` — card + every moment of the lemma + last 100
    reviews; runs `relink_episode` first."""
    with _txn() as cx:
        card = _get_card(cx, card_id)
        if card["episode_id"] and card["line_id"] is None:
            relink_episode(cx, int(card["episode_id"]))
            card = _get_card(cx, card_id)
        cache: dict = {}
        moments = [
            _moment_model(cx, m, card, cache)
            for m in cx.execute(
                "SELECT * FROM srs_moments WHERE lemma=? "
                "ORDER BY COALESCE(clarity,0) DESC, COALESCE(line_score,0) DESC, id ASC",
                (card["lemma"],),
            )
        ]
        reviews = [
            SrsReview(
                id=int(r["id"]),
                reviewed_at=_iso(r["reviewed_at"]) or "",
                review_day=r["review_day"],
                rating=int(r["rating"]),
                state_before=r["state_before"],
                state_after=r["state_after"],
                scheduled_days=int(r["scheduled_days"] or 0),
                elapsed_ms=r["elapsed_ms"],
                counted_fail=bool(r["counted_fail"]),
                demoted=bool(r["demoted"]),
                undone=bool(r["undone"]),
            )
            for r in cx.execute(
                "SELECT * FROM srs_reviews WHERE card_id=? ORDER BY id DESC LIMIT 100", (card_id,)
            )
        ]
        # the word's other cards (multi-card words, 2026-09-03), live first
        siblings = [
            SrsSiblingCard(
                id=int(r["id"]), state=r["state"], show_title=r["show_title"],
                ep_number=r["ep_number"], text=r["text"] or "", clarity=r["clarity"],
                queue_pos=r["queue_pos"], due_at=_iso(r["due_at"]),
                clip_status=r["clip_status"] or "pending",
            )
            for r in cx.execute(
                "SELECT id, state, show_title, ep_number, text, clarity, queue_pos, due_at, "
                "clip_status FROM srs_cards WHERE lemma=? AND id<>? AND source<>'confirm' "
                "ORDER BY (state='rejected'), id",
                (card["lemma"], card_id),
            )
        ]
        out = SrsCardDetail(card=_card_model(cx, card, source_cache=cache), moments=moments,
                            reviews=reviews, siblings=siblings)
    return out


# ---------------------------------------------------------------------------
# Manual creation, editing, deletion (§5.12, §4.2)
# ---------------------------------------------------------------------------

def create_card_manual(req: SrsCreateCard) -> dict:
    """`POST /srs/cards` (§5.12). With `line_id`: `{"card": SrsCard, "warning":
    str | None}` (201). Without: `{"queued": True, "lemma": str}` (202) after
    enqueuing `srs_find_moments`."""
    lemma = (req.lemma or "").strip()
    if not lemma:
        raise SrsInvalid("lemma is required")
    with _txn() as cx:
        rows = cx.execute(
            "SELECT id, source, state FROM srs_cards WHERE lemma=? ORDER BY id", (lemma,)
        ).fetchall()
        live = [r for r in rows if r["source"] != "confirm" and r["state"] != "rejected"]
        # multi-card words (2026-09-03): up to MAX_CARDS_PER_LEMMA sibling cards
        if len(live) >= MAX_CARDS_PER_LEMMA:
            raise SrsConflict(
                f"this word already has {MAX_CARDS_PER_LEMMA} cards",
                {"detail": f"this word already has {MAX_CARDS_PER_LEMMA} cards",
                 "card_id": int(live[0]["id"])},
            )
        confirm_id = next((int(r["id"]) for r in rows if r["source"] == "confirm"), None)

    if req.line_id is None:
        _enqueue("srs_find_moments", {"lemma": lemma}, priority=45)
        return {"queued": True, "lemma": lemma}

    try:
        snap = snapshot_line(int(req.line_id), lemma)
    except LookupError as e:
        raise SrsNotFound(str(e))
    except ValueError:
        raise SrsInvalid("invalid_surface: the word does not occur in that line")

    with _txn() as cx:
        dup = _same_moment_card(cx, lemma, snap["episode_id"], snap["norm_text"])
        if dup:
            raise SrsConflict(
                "this line is already a card of this word",
                {"detail": "this line is already a card of this word", "card_id": int(dup["id"])},
            )
        if confirm_id is not None:
            cx.execute("DELETE FROM srs_cards WHERE id=?", (confirm_id,))
        moment_id = _upsert_moment(
            cx, lemma, snap, verdict="user", accepted=1, clarity=None, now=_now()
        )
        gloss = _gloss_for(lemma)
        freq = cx.execute("SELECT rank FROM lemma_freq WHERE lemma=?", (lemma,)).fetchone()
        migaku = current_migaku_status(cx, lemma)
    spec = CardSpec(
        lemma=lemma,
        gloss=gloss,
        freq_rank=int(freq["rank"]) if freq else None,
        source="manual",
        line_id=snap["line_id"],
        episode_id=snap["episode_id"],
        anilist_id=snap["anilist_id"],
        show_title=snap["show_title"],
        ep_number=snap["ep_number"],
        start_ms=snap["start_ms"],
        end_ms=snap["end_ms"],
        text=snap["text"],
        norm_text=snap["norm_text"],
        text_furigana=snap["text_furigana"],
        translation=snap["translation"],
        translation_source=snap["translation_source"],
        target_surface=snap["target_surface"],
        tokens_json=snap["tokens_json"],
        context_json=snap["context_json"],
        extend=snap["extend"],
        alt_moment_ids=[],
    )
    # a manual card is the learner's own choice: the anime rule does not apply
    card_id = create_card(spec, position="top", study_now=bool(req.study_next),
                          allow_same_franchise=True)
    with _txn() as cx:
        cx.execute(
            "UPDATE srs_moments SET line_id=COALESCE(line_id, ?) WHERE id=?",
            (snap["line_id"], moment_id),
        )
        card = _card_out(cx, card_id)
    warning = "known_in_migaku" if migaku == "KNOWN" else None
    return {"card": card.model_dump(), "warning": warning}


def _gloss_for(lemma: str) -> Optional[str]:
    try:
        from ..learn.service import _glosses_for

        return _glosses_for([lemma]).get(lemma)
    except Exception as e:                                  # pragma: no cover
        log.debug("gloss lookup failed for %s: %s", lemma, e)
        return None


def _upsert_moment(
    cx, lemma: str, snap: dict, *, verdict: str, accepted: Optional[int],
    clarity: Optional[float], now: datetime,
    evidence_line_ids: Optional[list] = None,
    note: Optional[str] = None, judge_model: Optional[str] = None,
) -> int:
    """Insert/refresh the `srs_moments` row for this snapshot.

    `evidence_line_ids` (the neighbours the meaning depends on, §6.2) is stored
    so a later swap onto this moment rebuilds the same `extend`; `None` leaves
    whatever is already recorded (a user re-snapshot must not wipe a judged
    moment's evidence). `clarity`, `note` and `judge_model` likewise refresh the
    row only when given (2026-09-03 — a re-judge used to leave a stale score).
    """
    ev_json = (json.dumps([int(i) for i in evidence_line_ids], ensure_ascii=False)
               if evidence_line_ids is not None else None)
    cx.execute(
        "INSERT INTO srs_moments(lemma, line_id, episode_id, idx, start_ms, end_ms, norm_text, "
        "text, translation, translation_source, target_surface, verdict, accepted, clarity, "
        "judged_at, evidence_line_ids_json, note, judge_model) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(lemma, episode_id, norm_text) DO UPDATE SET "
        "line_id=COALESCE(excluded.line_id, srs_moments.line_id), "
        "verdict=excluded.verdict, accepted=excluded.accepted, judged_at=excluded.judged_at, "
        "clarity=COALESCE(excluded.clarity, srs_moments.clarity), "
        "note=COALESCE(excluded.note, srs_moments.note), "
        "judge_model=COALESCE(excluded.judge_model, srs_moments.judge_model), "
        "evidence_line_ids_json=COALESCE(excluded.evidence_line_ids_json, "
        "srs_moments.evidence_line_ids_json)",
        (
            lemma, snap.get("line_id"), snap["episode_id"], snap.get("idx"),
            snap["start_ms"], snap["end_ms"], snap["norm_text"], snap["text"],
            snap.get("translation"), snap.get("translation_source"), snap.get("target_surface"),
            verdict, accepted, clarity, _sql(now), ev_json, note, judge_model,
        ),
    )
    row = cx.execute(
        "SELECT id FROM srs_moments WHERE lemma=? AND episode_id=? AND norm_text=?",
        (lemma, snap["episode_id"], snap["norm_text"]),
    ).fetchone()
    return int(row["id"])


def patch_card(card_id: int, patch: SrsPatchCard) -> SrsCard:
    """`PATCH /srs/cards/{id}` — editing `translation` sets
    `translation_source='user'`; `target_surface` must be a substring of `text`."""
    data = patch.model_dump(exclude_none=True)
    if not data:
        with _txn() as cx:
            return _card_out(cx, card_id)
    with _txn() as cx:
        card = _get_card(cx, card_id)
        sets, vals = [], []
        for field in ("reading", "gloss", "meaning_short", "meaning_full", "usage_note", "notes"):
            if field in data:
                sets.append(f"{field}=?")
                vals.append(data[field])
        if "target_surface" in data:
            from .snapshot import strip_bidi

            surface = strip_bidi(data["target_surface"] or "")
            if surface and surface not in (card["text"] or ""):
                raise SrsInvalid("target_surface is not part of the line")
            sets.append("target_surface=?")
            vals.append(surface)
        if "translation" in data:
            sets.append("translation=?")
            vals.append(data["translation"])
            sets.append("translation_source='user'")
        if sets:
            cx.execute(
                f"UPDATE srs_cards SET {', '.join(sets)}, updated_at=datetime('now') WHERE id=?",
                (*vals, card_id),
            )
        out = _card_out(cx, card_id)
    _publish("card", card_id=card_id)
    return out


def delete_card(card_id: int) -> None:
    """`DELETE /srs/cards/{id}` — hard delete: row + reviews + clip dir; the
    primary moment becomes `user_rejected`; `srs_words` resets to `unjudged`."""
    with _txn() as cx:
        card = _get_card(cx, card_id)
        cx.execute(
            "UPDATE srs_moments SET verdict='user_rejected', accepted=0 "
            "WHERE lemma=? AND episode_id=? AND norm_text=?",
            (card["lemma"], card["episode_id"], card["norm_text"]),
        )
        # the word resets only when this was its last live card; otherwise a
        # surviving sibling becomes the word's primary (multi-card words, 2026-09-03)
        siblings = [c for c in live_cards_of(cx, card["lemma"]) if int(c["id"]) != card_id]
        if siblings:
            cx.execute(
                "UPDATE srs_words SET card_id=CASE WHEN card_id=? THEN ? ELSE card_id END, "
                "updated_at=datetime('now') WHERE lemma=?",
                (card_id, int(siblings[0]["id"]), card["lemma"]),
            )
        else:
            cx.execute(
                "UPDATE srs_words SET judge_status='unjudged', judge_reason=NULL, judge_note=NULL, "
                "card_id=NULL, updated_at=datetime('now') WHERE lemma=?",
                (card["lemma"],),
            )
        cx.execute("DELETE FROM srs_cards WHERE id=?", (card_id,))
        renumber_stack(cx)
    _remove_clip(card_id)
    _publish("card", card_id=card_id)


# ---------------------------------------------------------------------------
# Actions (§4.2, §3.8)
# ---------------------------------------------------------------------------

CONFIRM_FORBIDDEN = (
    "unknown", "forget", "demote", "study_next", "bottom", "bury", "unbury", "suspend", "resume",
)


def _reset_scheduler(cx, card_id: int, *, position: str, now: datetime) -> None:
    """The §3.7-step-1 reset (forget / unknown / restore): scheduler wiped, the
    card is a fresh `new` card again."""
    pos = 0 if position == "top" else stack_bottom(cx)
    cx.execute(
        "UPDATE srs_cards SET state='new', queue_pos=?, study_now=0, stability=NULL, "
        "difficulty=NULL, step=NULL, due_at=NULL, scheduled_days=0, fail_count=0, "
        "last_fail_day=NULL, last_review_at=NULL, introduced_at=NULL, known_source=NULL, "
        "known_at=NULL, suspend_reason=NULL, state_before_suspend=NULL, "
        "updated_at=datetime('now') WHERE id=?",
        (pos, card_id),
    )


def _resume(cx, card: dict, now: datetime) -> None:
    """The one resume/restore table of §3.8."""
    card_id = int(card["id"])
    state = card["state"]
    has_memory = card["stability"] is not None
    due = card["due_at"]
    now_sql = _sql(now)
    if state == "known":
        if has_memory:
            cx.execute(
                "UPDATE srs_cards SET state='review', step=NULL, due_at=?, known_source=NULL, "
                "known_at=NULL, queue_pos=NULL, study_now=0, updated_at=datetime('now') WHERE id=?",
                (max(due or now_sql, now_sql), card_id),
            )
        else:
            _reset_scheduler(cx, card_id, position="top", now=now)
    elif state == "suspended":
        prev = card["state_before_suspend"]
        if prev in ("learning", "relearning") and has_memory:
            cx.execute(
                "UPDATE srs_cards SET state=?, due_at=?, suspend_reason=NULL, "
                "state_before_suspend=NULL, queue_pos=NULL, study_now=0, "
                "updated_at=datetime('now') WHERE id=?",
                (prev, now_sql, card_id),
            )
        elif prev == "review" and has_memory:
            cx.execute(
                "UPDATE srs_cards SET state='review', step=NULL, due_at=?, suspend_reason=NULL, "
                "state_before_suspend=NULL, queue_pos=NULL, study_now=0, "
                "updated_at=datetime('now') WHERE id=?",
                (max(due or now_sql, now_sql), card_id),
            )
        else:
            _reset_scheduler(cx, card_id, position="bottom", now=now)
    elif state == "rejected":
        _reset_scheduler(cx, card_id, position="bottom", now=now)
    else:
        raise SrsInvalid(f"cannot resume a {state} card")
    cx.execute(
        "UPDATE srs_cards SET migaku_status_seen=? WHERE id=?",
        (current_migaku_status(cx, card["lemma"]), card_id),
    )


def _do_action(cx, card_id: int, action: str, now: datetime) -> dict:
    """Apply one §4.2 action inside an open transaction. Returns side-effect
    flags for the caller (`known_changed`, `clip`, `remove_clip`)."""
    card = _get_card(cx, card_id)
    flags: dict = {"known_before": _is_known_row(card["state"], card["scheduled_days"])}
    if card["source"] == "confirm" and action in CONFIRM_FORBIDDEN:
        raise SrsInvalid(f"{action} is not available on a confirm-known card")

    if action == "study_next":
        if card["state"] != "new":
            raise SrsInvalid("study_next applies to stack cards only")
        cx.execute("UPDATE srs_cards SET queue_pos=0, study_now=1, updated_at=datetime('now') WHERE id=?", (card_id,))
    elif action == "bottom":
        if card["state"] != "new":
            raise SrsInvalid("bottom applies to stack cards only")
        cx.execute(
            "UPDATE srs_cards SET queue_pos=?, study_now=0, updated_at=datetime('now') WHERE id=?",
            (stack_bottom(cx), card_id),
        )
    elif action == "bury":
        if card["state"] not in ACTIVE_STATES:
            raise SrsInvalid("only an active card can be buried")
        tomorrow = scheduler.day_start(scheduler.srs_day(now) + timedelta(days=1))
        cx.execute(
            "UPDATE srs_cards SET buried_until=?, updated_at=datetime('now') WHERE id=?",
            (_sql(tomorrow), card_id),
        )
    elif action == "unbury":
        cx.execute("UPDATE srs_cards SET buried_until=NULL, updated_at=datetime('now') WHERE id=?", (card_id,))
    elif action == "suspend":
        if card["state"] not in ACTIVE_STATES:
            raise SrsInvalid(f"cannot suspend a {card['state']} card")
        cx.execute(
            "UPDATE srs_cards SET state='suspended', suspend_reason='user', state_before_suspend=?, "
            "updated_at=datetime('now') WHERE id=?",
            (card["state"], card_id),
        )
        _leave_stack(cx, card_id)
    elif action == "resume":
        _resume(cx, card, now)
    elif action == "reject":
        cx.execute(
            "UPDATE srs_cards SET state='rejected', updated_at=datetime('now') WHERE id=?", (card_id,)
        )
        _leave_stack(cx, card_id)
        # the WORD is rejected only when no sibling card of it stays live
        # (multi-card words, 2026-09-03); rejecting one moment keeps the others
        siblings = [c for c in live_cards_of(cx, card["lemma"]) if int(c["id"]) != card_id]
        if not siblings:
            cx.execute(
                "INSERT INTO srs_words(lemma, judge_status, judge_reason, user_flag, updated_at) "
                "VALUES(?, 'rejected', 'user_skip', 'skip', datetime('now')) "
                "ON CONFLICT(lemma) DO UPDATE SET judge_status='rejected', judge_reason='user_skip', "
                "user_flag='skip', updated_at=datetime('now')",
                (card["lemma"],),
            )
        flags["remove_clip"] = True
    elif action == "restore":
        if card["state"] != "rejected":
            raise SrsInvalid("restore applies to rejected cards only")
        _resume(cx, card, now)
        cx.execute(
            "UPDATE srs_words SET user_flag=NULL, judge_status='accepted', judge_reason=NULL, "
            "updated_at=datetime('now') WHERE lemma=?",
            (card["lemma"],),
        )
        if card["text"]:
            cx.execute(
                "UPDATE srs_cards SET clip_status='pending', clip_version=clip_version+1, "
                "clip_requested_at=? WHERE id=?",
                (_sql(now), card_id),
            )
            flags["clip"] = int(cx.execute(
                "SELECT clip_version FROM srs_cards WHERE id=?", (card_id,)).fetchone()["clip_version"])
    elif action == "known":
        migaku_now = current_migaku_status(cx, card["lemma"])
        cx.execute(
            "UPDATE srs_cards SET state='known', known_source='user', known_at=?, due_at=NULL, "
            "step=NULL, suspend_reason=NULL, state_before_suspend=NULL, migaku_status_seen=?, "
            "updated_at=datetime('now') WHERE id=?",
            (_sql(now), migaku_now, card_id),
        )
        _leave_stack(cx, card_id)
        # knowing the word is knowing the word: its active sibling cards follow
        # (multi-card words, 2026-09-03)
        for sib in cx.execute(
            "SELECT id FROM srs_cards WHERE lemma=? AND id<>? AND state IN "
            "('new','learning','review','relearning')", (card["lemma"], card_id)
        ).fetchall():
            cx.execute(
                "UPDATE srs_cards SET state='known', known_source='sibling', known_at=?, "
                "due_at=NULL, step=NULL, suspend_reason=NULL, state_before_suspend=NULL, "
                "migaku_status_seen=?, updated_at=datetime('now') WHERE id=?",
                (_sql(now), migaku_now, int(sib["id"])),
            )
            _leave_stack(cx, int(sib["id"]))
    elif action == "unknown":
        _reset_scheduler(cx, card_id, position="top", now=now)
        cx.execute(
            "UPDATE srs_cards SET migaku_status_seen=? WHERE id=?",
            (current_migaku_status(cx, card["lemma"]), card_id),
        )
    elif action == "forget":
        cx.execute(
            "UPDATE srs_cards SET demoted_count=demoted_count WHERE id=?", (card_id,)
        )
        _reset_scheduler(cx, card_id, position="top", now=now)
        cx.execute(
            "UPDATE srs_cards SET migaku_status_seen=? WHERE id=?",
            (current_migaku_status(cx, card["lemma"]), card_id),
        )
    elif action == "demote":
        if card["state"] not in ACTIVE_STATES:
            raise SrsInvalid(f"cannot demote a {card['state']} card")
        _demote(cx, card, now, "manual")
    else:
        raise SrsInvalid(f"unknown action {action!r}")

    renumber_stack(cx)
    after = _get_card(cx, card_id)
    flags["known_after"] = _is_known_row(after["state"], after["scheduled_days"])
    return flags


def card_action(card_id: int, action: str) -> SrsCard:
    """`POST /srs/cards/{id}/action` — the §4.2 verbs."""
    now = _now()
    with _txn() as cx:
        flags = _do_action(cx, card_id, action, now)
        out = _card_out(cx, card_id)
    if flags.get("remove_clip"):
        _remove_clip(card_id)
    if flags.get("clip"):
        _enqueue("srs_clip", {"card_id": card_id, "v": int(flags["clip"])}, priority=30)
    if flags["known_before"] != flags["known_after"]:
        _recompute_comprehension()
    _publish("stack", card_id=card_id)
    return out


def bulk_action(req: SrsBulkRequest) -> SrsBulkResult:
    """`POST /srs/cards/bulk` — one transaction; `previous` enables the exact
    inverse the client's 6 s Undo issues."""
    now = _now()
    previous: list[SrsBulkPrevious] = []
    updated = 0
    known_changed = False
    clips_to_cut: list[tuple[int, int]] = []
    clips_to_drop: list[int] = []
    with _txn() as cx:
        # snapshot every row *before* the first mutation — `_do_action` renumbers
        # the stack, so reading inside the loop would record post-renumber
        # positions for every card after the first and the client's Undo would
        # restore the wrong order.
        snapshot: dict[int, Any] = {}
        for card_id in req.card_ids:
            row = cx.execute(
                "SELECT id, state, queue_pos FROM srs_cards WHERE id=?", (card_id,)
            ).fetchone()
            if row:
                snapshot[int(card_id)] = row
        for card_id in req.card_ids:
            row = snapshot.get(int(card_id))
            if not row:
                continue
            previous.append(
                SrsBulkPrevious(card_id=int(row["id"]), state=row["state"], queue_pos=row["queue_pos"])
            )
            try:
                flags = _do_action(cx, int(card_id), req.action, now)
            except SrsInvalid as e:
                log.info("bulk %s skipped card %s: %s", req.action, card_id, e)
                previous.pop()
                continue
            updated += 1
            known_changed = known_changed or flags["known_before"] != flags["known_after"]
            if flags.get("clip"):
                clips_to_cut.append((int(card_id), int(flags["clip"])))
            if flags.get("remove_clip"):
                clips_to_drop.append(int(card_id))
    for cid in clips_to_drop:
        _remove_clip(cid)
    for cid, v in clips_to_cut:
        _enqueue("srs_clip", {"card_id": cid, "v": v}, priority=30)
    if known_changed:
        _recompute_comprehension()
    _publish("stack")
    return SrsBulkResult(updated=updated, previous=previous)


def set_moment(card_id: int, req: SrsSwapMoment) -> SrsCard:
    """`POST /srs/cards/{id}/moment` — swap the primary moment (exactly one of
    `moment_id` / `line_id`)."""
    if (req.moment_id is None) == (req.line_id is None):
        raise SrsInvalid("give exactly one of moment_id / line_id")
    now = _now()
    with _txn() as cx:
        card = _get_card(cx, card_id)
        if card["source"] == "confirm":
            raise SrsInvalid("a confirm-known card has no moment")
        moment_id = req.moment_id
        if moment_id is None:
            line = cx.execute(
                "SELECT id, episode_id FROM subtitle_lines WHERE id=?", (req.line_id,)
            ).fetchone()
            if not line:
                raise SrsNotFound(f"line {req.line_id} not found")
            try:
                snap = snapshot_line(int(req.line_id), card["lemma"])
            except ValueError:
                raise SrsConflict("the word does not occur in that line")
            moment_id = _upsert_moment(
                cx, card["lemma"], snap, verdict="user", accepted=1, clarity=None, now=now
            )
        swap_moment(cx, card_id, int(moment_id))
        out = _card_out(cx, card_id)
    return out


def regenerate_clip(card_id: int) -> SrsCard:
    """`POST /srs/cards/{id}/clip` — bump `clip_version`, re-enqueue `srs_clip`;
    409 when the episode video is not on disk."""
    now = _now()
    with _txn() as cx:
        card = _get_card(cx, card_id)
        if not _source_available(cx, card["episode_id"]):
            raise SrsConflict("the episode video is not on disk")
        cx.execute(
            "UPDATE srs_cards SET clip_status='pending', clip_version=clip_version+1, "
            "clip_requested_at=?, clip_error=NULL, updated_at=datetime('now') WHERE id=?",
            (_sql(now), card_id),
        )
        version = int(cx.execute(
            "SELECT clip_version FROM srs_cards WHERE id=?", (card_id,)).fetchone()["clip_version"])
        out = _card_out(cx, card_id)
    _enqueue("srs_clip", {"card_id": card_id, "v": version}, priority=30)
    return out


def find_moments(card_id: int) -> dict:
    """`POST /srs/cards/{id}/find-moments` — 202; enqueues
    `srs_find_moments {lemma, card_id}` at priority 45."""
    with _txn() as cx:
        card = _get_card(cx, card_id)
    _enqueue("srs_find_moments", {"lemma": card["lemma"], "card_id": card_id}, priority=45)
    return {"queued": True, "lemma": card["lemma"]}


# ---------------------------------------------------------------------------
# Stack operations (§4.2)
# ---------------------------------------------------------------------------

def stack_move(req: SrsMove) -> SrsCard:
    """`POST /srs/stack/move` — the only reorder primitive (§4.2)."""
    with _txn() as cx:
        card = _get_card(cx, req.card_id)
        if card["state"] != "new":
            raise SrsInvalid("only stack cards can be moved")
        ids = [
            int(r["id"])
            for r in cx.execute(
                "SELECT id FROM srs_cards WHERE state='new' ORDER BY queue_pos, id"
            )
        ]
        ids = [i for i in ids if i != req.card_id]
        pos = max(0, min(int(req.position), len(ids)))
        ids.insert(pos, int(req.card_id))
        for n, cid in enumerate(ids, start=1):
            cx.execute("UPDATE srs_cards SET queue_pos=? WHERE id=?", (n, cid))
        _touch(cx, req.card_id)
        renumber_stack(cx)
        out = _card_out(cx, req.card_id)
    _publish("stack", card_id=req.card_id)
    return out


def _leverage_index() -> dict[str, int]:
    try:
        from ..learn.service import word_leverage

        data = word_leverage(top=200) or {}
        return {
            (w.get("lemma") or ""): i
            for i, w in enumerate(data.get("words") or [])
            if isinstance(w, dict)
        }
    except Exception as e:
        log.info("leverage unavailable for resort: %s", e)
        return {}


def stack_resort(req: SrsResort) -> dict:
    """`POST /srs/stack/resort` — `{"updated": n}`."""
    keep_top = max(0, int(req.keep_top or 0))
    lev = _leverage_index() if req.by == "leverage" else {}
    rng = random.Random()
    with _txn() as cx:
        rows = [dict(r) for r in cx.execute(
            "SELECT * FROM srs_cards WHERE state='new' ORDER BY queue_pos, id")]
        head, tail = rows[:keep_top], rows[keep_top:]

        def key(r: dict):
            demoted = 1 if int(r["demoted_count"] or 0) > 0 else 0
            study = 0 if int(r["study_now"] or 0) else 1
            if req.by == "score":
                primary = (-(r["score"] if r["score"] is not None else -1e9),)
            elif req.by == "frequency":
                primary = (r["freq_rank"] if r["freq_rank"] is not None else 10**9,)
            elif req.by == "leverage":
                primary = (lev.get(r["lemma"], 10**9), -(r["score"] or 0.0))
            elif req.by == "show":
                primary = (r["anilist_id"] or 10**9, -(r["score"] or 0.0))
            else:                                    # random
                primary = (rng.random(),)
            return (study, demoted, *primary, int(r["id"]))

        tail.sort(key=key)
        ordered = head + tail
        for n, r in enumerate(ordered, start=1):
            cx.execute("UPDATE srs_cards SET queue_pos=? WHERE id=?", (n, r["id"]))
        renumber_stack(cx)
    _publish("stack")
    return {"updated": len(rows)}


# ---------------------------------------------------------------------------
# Generation & candidates (§5.8, §5.9)
# ---------------------------------------------------------------------------

def _run_is_live(cx) -> bool:
    try:
        from . import generate

        return bool(generate.run_is_live(cx))
    except NotImplementedError:
        pass
    except Exception as e:                                  # pragma: no cover
        log.debug("generate.run_is_live failed: %s", e)
    row = cx.execute(
        "SELECT id FROM srs_generation_runs WHERE state IN ('planning','judging') ORDER BY id DESC"
    ).fetchall()
    if not row:
        return False
    for r in row:
        live = cx.execute(
            "SELECT 1 FROM jobs WHERE type='srs_judge_batch' AND state IN ('queued','running') "
            "AND payload_json LIKE ?",
            (f'%"run_id": {int(r["id"])}%',),
        ).fetchone()
        if live:
            return True
    return False


def _sweep_stale_runs(cx) -> int:
    try:
        from . import generate

        return int(generate.sweep_stale_runs(cx) or 0)
    except NotImplementedError:
        return 0
    except Exception as e:                                  # pragma: no cover
        log.warning("stale-run sweep failed: %s", e)
        return 0


def start_generation(req: SrsGenerateRequest) -> dict:
    """`POST /srs/generate` — runs the stale sweep, then enqueues `srs_generate`
    (or `srs_find_moments` for a single `lemma`). `{"queued": True, "run_id":
    int | None}`; 409 only when a run is open WITH a live batch job."""
    lemma = (req.lemma or "").strip()
    if lemma:
        _enqueue("srs_find_moments", {"lemma": lemma}, priority=45)
        return {"queued": True, "run_id": None}
    with _txn() as cx:
        _sweep_stale_runs(cx)
        if _run_is_live(cx):
            raise SrsConflict("a generation run is already in flight")
    payload: dict = {"trigger": "manual"}
    if req.want:
        payload["want"] = int(req.want)
    _enqueue("srs_generate", payload, priority=60)
    return {"queued": True, "run_id": None}


def generation(limit: int = 10) -> SrsGeneration:
    """`GET /srs/generation` — the Generation panel."""
    from .. import llm

    cx = connect()
    try:
        runs = [
            SrsGenerationRun(
                id=int(r["id"]), trigger=r["trigger"], state=r["state"], want=r["want"],
                words_scored=int(r["words_scored"] or 0), words_planned=int(r["words_planned"] or 0),
                batches_planned=int(r["batches_planned"] or 0), batches_done=int(r["batches_done"] or 0),
                moments_judged=int(r["moments_judged"] or 0), accepted=int(r["accepted"] or 0),
                rejected=int(r["rejected"] or 0), cards_created=int(r["cards_created"] or 0),
                llm_calls=int(r["llm_calls"] or 0), llm_in_tokens=int(r["llm_in_tokens"] or 0),
                llm_out_tokens=int(r["llm_out_tokens"] or 0), error=r["error"],
                started_at=_iso(r["started_at"]) or "", finished_at=_iso(r["finished_at"]),
            )
            for r in cx.execute(
                "SELECT * FROM srs_generation_runs ORDER BY id DESC LIMIT ?", (int(limit),)
            )
        ]
        ready = int(cx.execute(
            "SELECT COUNT(*) n FROM srs_words WHERE judge_status='unjudged' AND user_flag IS NULL "
            "AND canonical_of IS NULL").fetchone()["n"])
        probably_known = int(cx.execute(
            "SELECT COUNT(*) n FROM srs_words WHERE judge_status='probably_known'").fetchone()["n"])
        running = _run_is_live(cx)
    finally:
        cx.close()
    return SrsGeneration(
        runs=runs,
        running=running,
        llm_available=llm.available(),
        judge_model_id=settings.translation_model,
        candidates_ready=ready,
        probably_known=probably_known,
        census_at=_iso(kv_get("srs.candidates.at")),
    )


_CANDIDATE_SORTS = {"score": "score DESC", "rank": "freq_rank IS NULL, freq_rank ASC", "occ": "occ DESC"}


def candidates(
    status: str = "unjudged",
    q: str = "",
    sort: str = "score",
    limit: int = 50,
    offset: int = 0,
    refresh: bool = False,
) -> SrsCandidates:
    """`GET /srs/candidates` — Up next; `refresh=True` enqueues `srs_census`."""
    if refresh:
        _enqueue("srs_census", {}, priority=70)
    where, params = ["canonical_of IS NULL"], []
    if status and status != "all":
        where.append("judge_status=?")
        params.append(status)
    if q:
        like = f"%{q.strip()}%"
        where.append("(lemma LIKE ? OR reading LIKE ? OR gloss LIKE ?)")
        params.extend([like] * 3)
    order = _CANDIDATE_SORTS.get(sort or "score", _CANDIDATE_SORTS["score"])
    sql_where = " WHERE " + " AND ".join(where)
    cx = connect()
    try:
        total = int(cx.execute(f"SELECT COUNT(*) n FROM srs_words{sql_where}", params).fetchone()["n"])
        rows = cx.execute(
            f"SELECT * FROM srs_words{sql_where} ORDER BY {order} LIMIT ? OFFSET ?",
            (*params, int(limit), int(offset)),
        ).fetchall()
        items = [_candidate_model(cx, r) for r in rows]
    finally:
        cx.close()
    return SrsCandidates(items=items, total=total, computed_at=_iso(kv_get("srs.candidates.at")))


def _candidate_model(cx, r) -> SrsCandidate:
    best = None
    ids = _json_list(r["best_moment_ids_json"])
    if ids:
        m = cx.execute(
            "SELECT m.id, m.line_id, m.text, m.translation, m.episode_id FROM srs_moments m WHERE m.id=?",
            (ids[0],),
        ).fetchone()
        if m:
            ep = cx.execute(
                "SELECT e.ep_number, t.romaji, t.english FROM episodes e "
                "LEFT JOIN titles t ON t.anilist_id=e.anilist_id WHERE e.id=?",
                (m["episode_id"],),
            ).fetchone()
            best = SrsCandidateMoment(
                moment_id=int(m["id"]), line_id=m["line_id"], text=m["text"] or "",
                translation=m["translation"],
                show_title=(ep["romaji"] or ep["english"]) if ep else None,
                ep_number=ep["ep_number"] if ep else None,
            )
    return SrsCandidate(
        lemma=r["lemma"], reading=r["reading"], gloss=r["gloss"], freq_rank=r["freq_rank"],
        pos1=r["pos1"], score=float(r["score"] or 0.0), occ=int(r["occ"] or 0), eps=int(r["eps"] or 0),
        iplus1_lines=int(r["iplus1_lines"] or 0), moment_lines=int(r["moment_lines"] or 0),
        leverage_crossings=int(r["leverage_crossings"] or 0),
        next_watch_hits=int(r["next_watch_hits"] or 0), migaku_status=r["migaku_status"],
        in_unwatched=bool(r["in_unwatched"]), judge_status=r["judge_status"] or "unjudged",
        judge_reason=r["judge_reason"], judge_note=r["judge_note"], judged_at=_iso(r["judged_at"]),
        user_flag=r["user_flag"], card_id=r["card_id"], best_moment=best,
    )


def _candidate_out(lemma: str) -> SrsCandidate:
    cx = connect()
    try:
        row = cx.execute("SELECT * FROM srs_words WHERE lemma=?", (lemma,)).fetchone()
        if not row:
            raise SrsNotFound(f"word {lemma!r} not found")
        return _candidate_model(cx, row)
    finally:
        cx.close()


def word_skip(lemma: str) -> SrsCandidate:
    """`POST /srs/words/{lemma}/skip` — user blocklist (§4.2)."""
    with _txn() as cx:
        cx.execute(
            "INSERT INTO srs_words(lemma, judge_status, judge_reason, user_flag, updated_at) "
            "VALUES(?, 'rejected', 'user_skip', 'skip', datetime('now')) "
            "ON CONFLICT(lemma) DO UPDATE SET judge_status='rejected', judge_reason='user_skip', "
            "user_flag='skip', updated_at=datetime('now')",
            (lemma,),
        )
    return _candidate_out(lemma)


def word_unskip(lemma: str) -> SrsCandidate:
    """`POST /srs/words/{lemma}/unskip`."""
    with _txn() as cx:
        row = cx.execute("SELECT lemma FROM srs_words WHERE lemma=?", (lemma,)).fetchone()
        if not row:
            raise SrsNotFound(f"word {lemma!r} not found")
        cx.execute(
            "UPDATE srs_words SET judge_status='unjudged', judge_reason=NULL, user_flag='unskip', "
            "updated_at=datetime('now') WHERE lemma=?",
            (lemma,),
        )
    return _candidate_out(lemma)


def word_confirm_known(lemma: str) -> SrsCandidate:
    """`POST /srs/words/{lemma}/confirm-known` — creates the `source='confirm'`
    known card (no moment)."""
    now = _now()
    with _txn() as cx:
        existing = cx.execute("SELECT id FROM srs_cards WHERE lemma=?", (lemma,)).fetchone()
        if not existing:
            cur = cx.execute(
                "INSERT INTO srs_cards(lemma, source, state, known_source, known_at, text, "
                "norm_text, clip_status, migaku_status_seen, gloss) "
                "VALUES(?, 'confirm', 'known', 'user', ?, '', '', 'no_source', ?, ?)",
                (lemma, _sql(now), current_migaku_status(cx, lemma), _gloss_for(lemma)),
            )
            card_id = int(cur.lastrowid)
        else:
            card_id = int(existing["id"])
        cx.execute(
            "INSERT INTO srs_words(lemma, judge_status, user_flag, card_id, updated_at) "
            "VALUES(?, 'probably_known', 'confirm_known', ?, datetime('now')) "
            "ON CONFLICT(lemma) DO UPDATE SET judge_status='probably_known', "
            "user_flag='confirm_known', card_id=excluded.card_id, updated_at=datetime('now')",
            (lemma, card_id),
        )
        renumber_stack(cx)
    _recompute_comprehension()
    _publish("card", card_id=card_id)
    return _candidate_out(lemma)


def word_judge(lemma: str) -> dict:
    """`POST /srs/words/{lemma}/judge` — 202; enqueues `srs_find_moments`."""
    _enqueue("srs_find_moments", {"lemma": lemma}, priority=45)
    return {"queued": True, "lemma": lemma}


# ---------------------------------------------------------------------------
# Import (§5.11)
# ---------------------------------------------------------------------------

def import_deck(path: Optional[str] = None) -> SrsImportReport:
    """`POST /srs/import` — loads the JSON (default
    `data/srs-curation/cards.json`, must resolve under `ROOT/data`) and calls
    `importer.import_cards` (§5.11)."""
    from .importer import DEFAULT_PATH, import_cards

    raw = (path or DEFAULT_PATH).strip()
    p = pathlib.Path(raw).resolve() if os.path.isabs(raw) else (ROOT / raw).resolve()
    data_root = (ROOT / "data").resolve()
    if not str(p).startswith(str(data_root) + os.sep):
        raise SrsInvalid("path must resolve under data/")
    if not p.exists():
        raise SrsNotFound(f"{p} not found")
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        raise SrsInvalid(f"could not read {p.name}: {e}")
    if not isinstance(doc, dict) or not isinstance(doc.get("cards"), list):
        raise SrsInvalid("expected {cards: [...]}")
    return import_cards(doc)


# ---------------------------------------------------------------------------
# Summary & stats (§7.2)
# ---------------------------------------------------------------------------

def _states_map(cx) -> dict[str, int]:
    states = zero_states()
    for r in cx.execute("SELECT state, COUNT(*) n FROM srs_cards GROUP BY state"):
        if r["state"] in states:
            states[r["state"]] = int(r["n"])
    return states


def _streak_days(cx, today: str) -> int:
    days = [
        r["review_day"]
        for r in cx.execute(
            "SELECT DISTINCT review_day FROM srs_reviews WHERE undone=0 ORDER BY review_day DESC LIMIT 400"
        )
    ]
    if not days:
        return 0
    today_d = datetime.strptime(today, "%Y-%m-%d").date()
    have = set(days)
    cursor_day = today_d
    if today not in have:
        cursor_day = today_d - timedelta(days=1)
        if cursor_day.isoformat() not in have:
            return 0
    streak = 0
    while cursor_day.isoformat() in have:
        streak += 1
        cursor_day -= timedelta(days=1)
    return streak


def summary() -> SrsSummary:
    """`GET /srs/summary` — deck header + nav badge; `states` zero-filled."""
    now = _now()
    now_sql = _sql(now)
    day = _day(now)
    cfg = get_settings()
    cx = connect()
    try:
        introduced, reviewed_review, reviewed_today = _counts_today(cx, day)
        due_learning = int(cx.execute(
            "SELECT COUNT(*) n FROM srs_cards WHERE state IN ('learning','relearning') "
            f"AND due_at <= ? AND {_NOT_BURIED}", (now_sql, now_sql)).fetchone()["n"])
        due_review = int(cx.execute(
            "SELECT COUNT(*) n FROM srs_cards WHERE state='review' AND due_at <= ? "
            f"AND {_NOT_BURIED}", (now_sql, now_sql)).fetchone()["n"])
        new_stack_total = int(cx.execute(
            "SELECT COUNT(*) n FROM srs_cards WHERE state='new'").fetchone()["n"])
        servable_new = int(cx.execute(
            f"SELECT COUNT(*) n FROM srs_cards WHERE state='new' AND text <> '' AND {_NOT_BURIED}",
            (now_sql,)).fetchone()["n"])
        again_today = int(cx.execute(
            "SELECT COUNT(*) n FROM srs_reviews WHERE review_day=? AND undone=0 AND rating=1",
            (day,)).fetchone()["n"])
        time_today = int(cx.execute(
            "SELECT COALESCE(SUM(elapsed_ms),0) ms FROM srs_reviews WHERE review_day=? AND undone=0",
            (day,)).fetchone()["ms"] or 0)
        next_due = cx.execute(
            "SELECT MIN(due_at) d FROM srs_cards WHERE state IN ('learning','review','relearning') "
            "AND due_at IS NOT NULL").fetchone()["d"]
        known_total = int(cx.execute(
            f"SELECT COUNT(*) n FROM srs_cards sc WHERE {KNOWN_SQL}").fetchone()["n"])
        week_delta = int(cx.execute(
            f"SELECT COUNT(*) n FROM srs_cards sc WHERE {KNOWN_SQL} AND "
            "COALESCE(sc.known_at, sc.last_review_at) >= datetime('now','-7 days')").fetchone()["n"])
        demoted_in_stack = int(cx.execute(
            "SELECT COUNT(*) n FROM srs_cards WHERE state='new' AND demoted_count > 0").fetchone()["n"])
        clip_rows = {
            r["clip_status"]: int(r["n"])
            for r in cx.execute("SELECT clip_status, COUNT(*) n FROM srs_cards GROUP BY clip_status")
        }
        states = _states_map(cx)
        streak = _streak_days(cx, day)
    finally:
        cx.close()
    try:
        clip_bytes = int(kv_get("srs.clips.bytes") or 0)
    except (TypeError, ValueError):
        clip_bytes = 0
    return SrsSummary(
        day=day,
        server_time=_iso(now_sql) or "",
        due_learning=due_learning,
        due_review=due_review,
        new_today_done=introduced,
        new_today_limit=int(cfg.new_per_day),
        new_available=min(max(0, int(cfg.new_per_day) - introduced), servable_new),
        new_stack_total=new_stack_total,
        reviewed_today=reviewed_today,
        again_today=again_today,
        time_today_ms=time_today,
        streak_days=streak,
        next_due_at=_iso(next_due),
        review_cap_hit=reviewed_review >= MAX_REVIEWS_PER_DAY,
        states=states,
        known_total=known_total,
        known_week_delta=week_delta,
        demoted_in_stack=demoted_in_stack,
        generation=generation(limit=5),
        clips=SrsClipCounts(
            ready=clip_rows.get("ready", 0), pending=clip_rows.get("pending", 0),
            failed=clip_rows.get("failed", 0), no_source=clip_rows.get("no_source", 0),
            missing=clip_rows.get("missing", 0), bytes=clip_bytes,
        ),
        settings=cfg,
    )


_INTERVAL_BUCKETS = (
    ("1d", 1, 1), ("2-3d", 2, 3), ("4-7d", 4, 7), ("8-14d", 8, 14),
    ("15-30d", 15, 30), ("31-90d", 31, 90), ("91d+", 91, 10**6),
)


def stats(days: int = 90) -> SrsStats:
    """`GET /srs/stats` — per-day history, forecast, retention, intervals."""
    now = _now()
    today = scheduler.srs_day(now)
    first_day = (today - timedelta(days=max(1, int(days)) - 1)).isoformat()
    cx = connect()
    try:
        by_day = {
            r["review_day"]: r
            for r in cx.execute(
                "SELECT review_day, COUNT(*) reviews, "
                "SUM(rating=1) again, SUM(rating=2) hard, SUM(rating=3) good, SUM(rating=4) easy, "
                "SUM(state_before='new') new_cards, COALESCE(SUM(elapsed_ms),0) time_ms, "
                "SUM(demoted) demotions "
                "FROM srs_reviews WHERE undone=0 AND review_day >= ? GROUP BY review_day",
                (first_day,),
            )
        }
        series = []
        d = today - timedelta(days=max(1, int(days)) - 1)
        while d <= today:
            key = d.isoformat()
            r = by_day.get(key)
            series.append(SrsStatsDay(
                day=key,
                reviews=int(r["reviews"]) if r else 0,
                again=int(r["again"] or 0) if r else 0,
                hard=int(r["hard"] or 0) if r else 0,
                good=int(r["good"] or 0) if r else 0,
                easy=int(r["easy"] or 0) if r else 0,
                new_cards=int(r["new_cards"] or 0) if r else 0,
                time_ms=int(r["time_ms"] or 0) if r else 0,
                demotions=int(r["demotions"] or 0) if r else 0,
            ))
            d += timedelta(days=1)

        forecast_counts: dict[str, int] = {}
        for r in cx.execute(
            "SELECT due_at FROM srs_cards WHERE state='review' AND due_at IS NOT NULL"
        ):
            due = _dt(r["due_at"])
            if due is None:
                continue
            key = max(scheduler.srs_day(due), today).isoformat()
            if key <= (today + timedelta(days=30)).isoformat():
                forecast_counts[key] = forecast_counts.get(key, 0) + 1
        forecast = []
        for n in range(30):
            key = (today + timedelta(days=n)).isoformat()
            forecast.append(SrsStatsForecastDay(day=key, due=forecast_counts.get(key, 0)))

        def _retention(window: int) -> Optional[float]:
            since = (today - timedelta(days=window)).isoformat()
            row = cx.execute(
                "SELECT COUNT(*) n, SUM(rating > 1) ok FROM srs_reviews "
                "WHERE undone=0 AND state_before='review' AND review_day >= ?",
                (since,),
            ).fetchone()
            n = int(row["n"] or 0)
            return round(float(row["ok"] or 0) / n, 4) if n else None

        avg_row = cx.execute(
            "SELECT AVG(elapsed_ms) a FROM srs_reviews WHERE undone=0 AND elapsed_ms IS NOT NULL "
            "AND review_day >= ?",
            (first_day,),
        ).fetchone()
        intervals = []
        for label, lo, hi in _INTERVAL_BUCKETS:
            n = int(cx.execute(
                "SELECT COUNT(*) n FROM srs_cards WHERE state='review' AND scheduled_days BETWEEN ? AND ?",
                (lo, hi),
            ).fetchone()["n"])
            intervals.append(SrsStatsInterval(bucket=label, count=n))
        states = _states_map(cx)
        known_total = int(cx.execute(
            f"SELECT COUNT(*) n FROM srs_cards sc WHERE {KNOWN_SQL}").fetchone()["n"])
        out = SrsStats(
            days=series,
            forecast=forecast,
            retention_7d=_retention(7),
            retention_30d=_retention(30),
            states=states,
            known_total=known_total,
            avg_time_per_card_ms=round(float(avg_row["a"]), 1) if avg_row and avg_row["a"] else None,
            streak_days=_streak_days(cx, today.isoformat()),
            intervals=intervals,
        )
    finally:
        cx.close()
    return out


def settings_get() -> SrsSettings:
    """`GET /srs/settings`."""
    return get_settings()


def settings_put(patch: SrsSettingsPatch) -> SrsSettings:
    """`PUT /srs/settings` — partial; ranges validated by the model (422)."""
    return update_settings(patch)


# ---------------------------------------------------------------------------
# Jobs & periodics (§9.1, §9.2)
# ---------------------------------------------------------------------------

def _requeue_stale_clips(cx) -> int:
    """Cards stuck `pending` for > STALE_CLIP_MIN with no live `srs_clip` job."""
    try:
        from . import clips

        return int(clips.requeue_stale_pending(cx) or 0)
    except NotImplementedError:
        pass
    except Exception as e:                                  # pragma: no cover
        log.warning("clips.requeue_stale_pending failed: %s", e)
        return 0
    rows = cx.execute(
        "SELECT id, clip_version FROM srs_cards WHERE clip_status='pending' AND text <> '' "
        "AND (clip_requested_at IS NULL OR clip_requested_at <= datetime('now', ?)) LIMIT 200",
        (f"-{STALE_CLIP_MIN} minutes",),
    ).fetchall()
    n = 0
    for r in rows:
        payload = json.dumps({"card_id": int(r["id"]), "v": int(r["clip_version"] or 1)}, sort_keys=True)
        live = cx.execute(
            "SELECT 1 FROM jobs WHERE type='srs_clip' AND payload_json=? AND state IN ('queued','running')",
            (payload,),
        ).fetchone()
        if live:
            continue
        cx.execute(
            "UPDATE srs_cards SET clip_requested_at=datetime('now') WHERE id=?", (int(r["id"]),)
        )
        _enqueue("srs_clip", {"card_id": int(r["id"]), "v": int(r["clip_version"] or 1)}, priority=30)
        n += 1
    return n


def periodic_tick() -> None:
    """Every 6 h: enqueue `srs_generate {"trigger":"auto"}`, `srs_reconcile` once
    per SRS day, `srs_clip_audit` once per 7 days; inline stale-run sweep, stale
    `pending` clip re-enqueue and `relink_episode` for episodes with NULL
    `line_id` rows (§9.2)."""
    now = _now()
    today = _day(now)
    try:
        _enqueue("srs_generate", {"trigger": "auto"}, priority=60)
    except Exception as e:                                  # pragma: no cover
        log.warning("srs tick: could not enqueue generation: %s", e)
    if (kv_get("srs.reconcile.last") or "")[:10] != today:
        try:
            _enqueue("srs_reconcile", {}, priority=30)
        except Exception as e:                              # pragma: no cover
            log.warning("srs tick: could not enqueue reconcile: %s", e)
    last_audit = _dt(kv_get("srs.audit.last"))
    if last_audit is None or (now - last_audit) >= timedelta(days=7):
        try:
            _enqueue("srs_clip_audit", {}, priority=90)
            kv_set("srs.audit.last", _sql(now) or "")
        except Exception as e:                              # pragma: no cover
            log.warning("srs tick: could not enqueue clip audit: %s", e)
    try:
        with _txn() as cx:
            _sweep_stale_runs(cx)
            _requeue_stale_clips(cx)
            eps = [
                int(r["episode_id"])
                for r in cx.execute(
                    "SELECT DISTINCT episode_id FROM srs_cards WHERE line_id IS NULL "
                    "AND episode_id IS NOT NULL "
                    "UNION SELECT DISTINCT episode_id FROM srs_moments WHERE line_id IS NULL"
                )
            ]
            for episode_id in eps[:200]:
                relink_episode(cx, episode_id)
    except Exception as e:                                  # pragma: no cover
        log.warning("srs tick housekeeping failed: %s", e)


def _job_reconcile(payload: dict) -> None:
    """`srs_reconcile {}` — edge-triggered Migaku reconcile (§5.10). Raises on a
    DB error so the queue retries."""
    now = _now()
    retired = parked = 0
    with _txn() as cx:
        rows = cx.execute(
            "SELECT id, lemma, state, migaku_status_seen, scheduled_days FROM srs_cards "
            "WHERE state IN ('new','learning','review','relearning')"
        ).fetchall()
        for r in rows:
            kw = current_migaku_status(cx, r["lemma"])
            if kw == (r["migaku_status_seen"] or None):
                continue
            if kw == "KNOWN":
                cx.execute(
                    "UPDATE srs_cards SET state='known', known_source='migaku', known_at=?, "
                    "due_at=NULL, step=NULL, migaku_status_seen=?, updated_at=datetime('now') WHERE id=?",
                    (_sql(now), kw, r["id"]),
                )
                _leave_stack(cx, int(r["id"]))
                retired += 1
            elif kw == "IGNORED":
                cx.execute(
                    "UPDATE srs_cards SET state='suspended', suspend_reason='migaku_ignored', "
                    "state_before_suspend=?, migaku_status_seen=?, updated_at=datetime('now') WHERE id=?",
                    (r["state"], kw, r["id"]),
                )
                _leave_stack(cx, int(r["id"]))
                parked += 1
            else:
                cx.execute(
                    "UPDATE srs_cards SET migaku_status_seen=? WHERE id=?", (kw, r["id"])
                )
        cx.execute(
            "UPDATE srs_words SET migaku_status = (SELECT kw.status FROM known_words kw "
            "WHERE kw.dict_form = srs_words.lemma LIMIT 1)"
        )
        renumber_stack(cx)
    kv_set("srs.reconcile.last", _sql(now) or "")
    if retired or parked:
        _event(
            f"Migaku sync: {retired} cards retired as known, {parked} parked", "info"
        )
        _publish("reconcile")
        _recompute_comprehension()
        try:
            _enqueue("srs_generate", {"trigger": "known_sync"}, priority=60)
        except Exception as e:                              # pragma: no cover
            log.warning("could not enqueue generation after reconcile: %s", e)


def register_jobs() -> None:
    """Register this module's job handlers (§9.1). `srs_reconcile` is real, so it
    is registered; the generation/clip types belong to WP-B and stay unregistered
    (and therefore parked, not failed) until their handlers exist."""
    try:
        from ..jobs.service import register

        register("srs_reconcile", _job_reconcile)
    except Exception as e:                                  # pragma: no cover
        log.warning("could not register srs handlers: %s", e)


# Importing this module (via main.MODULES) must register every SRS handler —
# only the real ones. The generation/clip stubs register nothing yet (§9.1).
from . import census, clips, generate  # noqa: E402,F401

for _m in (census, generate, clips):
    _m.register_jobs()
register_jobs()
