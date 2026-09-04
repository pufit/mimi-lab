"""Generation runs — planning, fan-out and the judge jobs (§5.8).

`srs_generate` sweeps stale runs, runs the census, plans ONE run inside a single
`BEGIN IMMEDIATE` transaction (so two planners can never overlap) and fans out
`srs_judge_batch` jobs, four words each. Every step is idempotent: the pending
`srs_moments` rows ARE the work list, so a retry never re-bills a judged word,
and a run whose jobs died is swept back to `unjudged` within `STALE_RUN_HOURS`.

Every handler raises on failure (the queue retries with backoff, §9.1); the
only silent exits are "nothing to do", a superseded/absent run and a missing
Anthropic key (which resets the run instead of burning attempts).
"""
from __future__ import annotations

import json
import logging
import math
import sqlite3
import time
from typing import Optional

from .constants import (
    BLIND_JUDGE_MODEL,
    BLIND_WORKERS,
    JUDGE_MIN_CLARITY,
    MAX_CARDS_PER_LEMMA,
    MAX_WANT_PER_RUN,
    MOMENTS_PER_WORD,
    STACK_MIN,
    STACK_TARGET,
    STALE_RUN_HOURS,
    WORDS_PER_JUDGE_CALL,
)

log = logging.getLogger("mimi_lab.srs.generate")

# words planned per run ≈ want / expected acceptance, capped (§5.8)
EXPECTED_ACCEPTANCE = 0.5
MAX_WORDS_PER_RUN = 80
# a run younger than this with no batch job yet is being planned right now
SWEEP_GRACE_S = 60
# Anthropic list prices per MTok (§5.6) for the run's cost line
_PRICES = {"claude-sonnet-4-6": (3.0, 15.0), "claude-haiku-4-5": (1.0, 5.0),
           "claude-opus-5": (5.0, 25.0)}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _srs_day() -> str:
    """Local SRS day (04:00 cutoff). Falls back to the local date when WP-A's
    scheduler is not available yet."""
    try:
        from .scheduler import srs_day
        return srs_day()
    except Exception:
        from datetime import datetime, timedelta
        now = datetime.now()
        return (now - timedelta(hours=4)).strftime("%Y-%m-%d")


def _publish(data: dict) -> None:
    try:
        from ..events import bus
        bus.publish("srs", data)
    except Exception as e:
        log.debug("bus publish failed: %s", e)


def _record(title: str, kind: str = "info", detail: Optional[str] = None) -> None:
    try:
        from ..events import service as events
        events.record("srs", title, kind=kind, detail=detail)
    except Exception as e:
        log.debug("event record failed: %s", e)


def _first_sense(gloss: Optional[str]) -> str:
    return (gloss or "").split(";")[0].strip().lower()


def _batch_job_live(cx, run_id: int) -> bool:
    r = cx.execute(
        "SELECT 1 FROM jobs WHERE type='srs_judge_batch' AND state IN ('queued','running') "
        "AND payload_json LIKE ? LIMIT 1", (f'%"run_id": {int(run_id)}%',)).fetchone()
    return r is not None


# ---------------------------------------------------------------------------
# stale-run sweep (§5.8)
# ---------------------------------------------------------------------------

def sweep_stale_runs(cx) -> int:
    """Close runs in `planning`/`judging` with no live `srs_judge_batch` job or a
    heartbeat older than `STALE_RUN_HOURS`: `state='error', error='stale'`, drop
    their unjudged `srs_moments`, reset their `pending` words to `unjudged`.
    Returns the number of runs closed (§5.8). Called by `service.periodic_tick`
    and by `POST /srs/generate`.

    A run started seconds ago with no job yet is being planned right now
    (`SWEEP_GRACE_S`) and is left alone.
    """
    rows = cx.execute(
        "SELECT id, state, started_at, heartbeat_at, "
        "  CAST((julianday('now') - julianday(COALESCE(heartbeat_at, started_at))) * 24 AS REAL) "
        "    AS age_h, "
        "  CAST((julianday('now') - julianday(started_at)) * 86400 AS REAL) AS started_s "
        "FROM srs_generation_runs WHERE state IN ('planning','judging')"
    ).fetchall()
    closed = 0
    for r in rows:
        age_h = float(r["age_h"] or 0.0)
        live = _batch_job_live(cx, r["id"])
        young = float(r["started_s"] or 0.0) < SWEEP_GRACE_S
        if live and age_h <= STALE_RUN_HOURS:
            continue
        if not live and young:
            continue
        if live and age_h > STALE_RUN_HOURS:
            log.warning("run %s heartbeat is %.1f h old — sweeping", r["id"], age_h)
        lemmas = [x["lemma"] for x in cx.execute(
            "SELECT DISTINCT lemma FROM srs_moments WHERE run_id=?", (r["id"],))]
        cx.execute("DELETE FROM srs_moments WHERE run_id=? AND judged_at IS NULL", (r["id"],))
        for chunk in (lemmas[i:i + 400] for i in range(0, len(lemmas), 400)):
            q = ",".join("?" * len(chunk))
            cx.execute(
                f"UPDATE srs_words SET judge_status='unjudged', updated_at=datetime('now') "
                f"WHERE judge_status='pending' AND lemma IN ({q})", chunk)
        cx.execute(
            "UPDATE srs_generation_runs SET state='error', error='stale', "
            "finished_at=datetime('now') WHERE id=? AND state IN ('planning','judging')",
            (r["id"],))
        closed += 1
    if closed:
        cx.commit()
        log.info("swept %d stale generation run(s)", closed)
        _publish({"what": "generation", "state": "error"})
    return closed


def run_is_live(cx) -> bool:
    """True when a run is `planning`/`judging` AND has a queued/running
    `srs_judge_batch` job — the 409 predicate of `POST /srs/generate`."""
    rows = cx.execute(
        "SELECT id FROM srs_generation_runs WHERE state IN ('planning','judging')").fetchall()
    return any(_batch_job_live(cx, r["id"]) for r in rows)


# ---------------------------------------------------------------------------
# planning (§5.8)
# ---------------------------------------------------------------------------

def plan_run(trigger: str, want: Optional[int]) -> Optional[int]:
    """Census + plan one generation run inside a single `BEGIN IMMEDIATE`
    transaction, then fan out `srs_judge_batch` jobs. Returns the run id, or
    `None` when there is nothing to do (§4.4 fast path)."""
    from ..config import settings as _settings
    from ..db import connect, kv_get
    from ..jobs.service import enqueue
    from . import census
    from .settings import get_settings

    cfg = get_settings()
    cx = connect()
    try:
        n_cards = cx.execute("SELECT COUNT(*) AS n FROM srs_cards").fetchone()["n"]
        if n_cards == 0:
            log.info("srs_generate: empty deck — import the curated cards first")
            return None
        n_new = cx.execute(
            "SELECT COUNT(*) AS n FROM srs_cards WHERE state='new'").fetchone()["n"]
        mark = census.corpus_mark(cx)
        mark_changed = mark != (kv_get("srs.corpus.mark") or "")
        if trigger == "auto" and n_new >= STACK_MIN and not mark_changed:
            log.info("srs_generate: stack has %d new cards and the corpus is unchanged", n_new)
            return None
        if run_is_live(cx):
            log.info("srs_generate: another run is live")
            return None
        if want is None:
            want = STACK_TARGET - n_new
        want = max(0, min(int(want), MAX_WANT_PER_RUN))
        if want <= 0:
            if trigger == "auto":
                return None
            want = 1
    finally:
        cx.close()

    result = census.run(cfg)
    if not result.candidates:
        log.info("srs_generate: census produced no candidates")
        return None

    words_planned = min(MAX_WORDS_PER_RUN, max(WORDS_PER_JUDGE_CALL,
                                               math.ceil(want / EXPECTED_ACCEPTANCE)))

    px = sqlite3.connect(str(_settings.mimi_lab_db), check_same_thread=False,
                         isolation_level=None)
    px.row_factory = sqlite3.Row
    px.execute("PRAGMA busy_timeout=15000")
    run_id: Optional[int] = None
    planned: list[str] = []
    try:
        px.execute("BEGIN IMMEDIATE")
        open_run = px.execute(
            "SELECT id FROM srs_generation_runs WHERE state IN ('planning','judging') LIMIT 1"
        ).fetchone()
        if open_run is not None:
            px.execute("ROLLBACK")
            log.info("srs_generate: run %s is already open", open_run["id"])
            return None
        cur = px.execute(
            "INSERT INTO srs_generation_runs (trigger, state, want, words_scored, corpus_mark, "
            "started_at, heartbeat_at) VALUES (?, 'planning', ?, ?, ?, datetime('now'), "
            "datetime('now'))",
            (trigger, want, result.words_scored, mark))
        run_id = int(cur.lastrowid)

        eligible = px.execute(
            "SELECT lemma FROM srs_words WHERE judge_status='unjudged' AND user_flag IS NULL "
            "AND canonical_of IS NULL AND card_id IS NULL"
        ).fetchall()
        eligible_set = {r["lemma"] for r in eligible}
        for cand in result.candidates:
            if len(planned) >= words_planned:
                break
            lemma = cand["lemma"]
            if lemma not in eligible_set:
                continue
            moments = census.rank_moments(lemma, cfg, cx=px, run_id=run_id, batch_no=0,
                                          limit=MOMENTS_PER_WORD, translate=False,
                                          exclude_judged=True)
            if not moments:
                continue
            planned.append(lemma)
        for i, lemma in enumerate(planned):
            batch_no = i // WORDS_PER_JUDGE_CALL
            px.execute(
                "UPDATE srs_moments SET batch_no=? WHERE run_id=? AND lemma=? "
                "AND judged_at IS NULL", (batch_no, run_id, lemma))
            px.execute(
                "UPDATE srs_words SET judge_status='pending', updated_at=datetime('now') "
                "WHERE lemma=?", (lemma,))
        batches = math.ceil(len(planned) / WORDS_PER_JUDGE_CALL) if planned else 0
        if not planned:
            px.execute(
                "UPDATE srs_generation_runs SET state='nothing_to_do', "
                "finished_at=datetime('now') WHERE id=?", (run_id,))
            px.execute("COMMIT")
            log.info("srs_generate: nothing to plan (run %s)", run_id)
            return None
        px.execute(
            "UPDATE srs_generation_runs SET state='judging', words_planned=?, batches_planned=? "
            "WHERE id=?", (len(planned), batches, run_id))
        px.execute(
            "INSERT INTO kv(key,value,updated_at) VALUES('srs.corpus.mark',?,datetime('now')) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=datetime('now')",
            (mark,))
        px.execute("COMMIT")
    except Exception:
        try:
            px.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        px.close()
        census.invalidate_cache()      # ~450 MB of lines; the run is planned

    for batch_no in range(math.ceil(len(planned) / WORDS_PER_JUDGE_CALL)):
        enqueue("srs_judge_batch", {"run_id": run_id, "batch_no": batch_no}, priority=55)
    _publish({"what": "generation", "run_id": run_id, "state": "judging"})
    log.info("run %s: planned %d words in %d batches (want %d)",
             run_id, len(planned), math.ceil(len(planned) / WORDS_PER_JUDGE_CALL), want)
    return run_id


# ---------------------------------------------------------------------------
# packets
# ---------------------------------------------------------------------------

def _hints_for(cx, word: dict, moments: list[dict]) -> list[str]:
    """Judge hints from DB-only signals (§5.5): tokenizer splits, a reading that
    matches a KNOWN word, a capitalised English word, a single-kanji fragment."""
    from ..learn import service as ls
    from .census import KANJI_RE, _CAPS_RE, _is_katakana

    lemma = word["lemma"]
    hints: list[str] = []
    has_kanji = bool(KANJI_RE.search(lemma))
    if not word.get("pos1"):
        try:
            toks = list(ls._tagger()(lemma))
            if len(toks) > 1 and any((ls._feat(t, "pos1") or "") in ls._NON_VOCAB_POS
                                     for t in toks):
                hints.append("tokenizer: UniDic splits this as "
                             + "+".join(t.surface for t in toks))
        except Exception:
            pass
    if not has_kanji:
        hira = ls._to_hira(lemma)
        r = cx.execute(
            "SELECT dict_form FROM known_words WHERE status='KNOWN' AND (reading=? OR dict_form=?) "
            "LIMIT 1", (hira, hira)).fetchone()
        if r and r["dict_form"] != lemma:
            hints.append(f"reading matches KNOWN {r['dict_form']} (may be a homophone)")
    if _is_katakana(lemma) and any(_CAPS_RE.search(m.get("translation") or "") for m in moments):
        hints.append("looks like a name in the English")
    if len(lemma) == 1 and KANJI_RE.match(lemma):
        ratio = word.get("standalone_ratio")
        pct = f"{float(ratio) * 100:.0f}%" if ratio is not None else "some"
        hints.append(f"single kanji; standalone in {pct} of its occurrences — "
                     f"accept only if used standalone here")
    if (word.get("migaku_status") or "") == "LEARNING":
        hints.append("Migaku marks this word LEARNING")
    return hints


def _context_for(cx, moment: dict) -> tuple[list[dict], list[dict]]:
    from .snapshot import dialogue_neighbours
    line = {"line_id": moment.get("line_id"), "episode_id": moment["episode_id"],
            "start_ms": moment["start_ms"], "end_ms": moment["end_ms"]}
    try:
        before, after = dialogue_neighbours(cx, line, n=2)
    except Exception as e:
        log.debug("context lookup failed: %s", e)
        return [], []
    keep = ("text", "translation", "line_id", "start_ms", "end_ms")
    return ([{k: c.get(k) for k in keep} for c in before],
            [{k: c.get(k) for k in keep} for c in after])


def _fill_missing_translations(moments_by_lemma: dict[str, list[dict]]) -> int:
    """Machine-translate the top-2 moments per word that have no English (§5.4).

    Done here rather than during planning: `translate_line` is an LLM call and
    the planning transaction holds the write lock. The moments are tagged
    `translation_source='mt'` so the judge is told the English is not
    independent evidence.
    """
    from .. import llm as _llm
    from ..db import cursor
    from ..learn import service as ls

    if not _llm.available():
        return 0
    todo: list[dict] = []
    for rows in moments_by_lemma.values():
        missing = [m for m in rows if not (m.get("translation") or "") and m.get("line_id")]
        todo.extend(sorted(missing, key=lambda m: -(m.get("line_score") or 0.0))[:2])
    if not todo:
        return 0
    done = 0
    for m in todo:
        try:
            tr = (ls.translate_line(int(m["line_id"])) or {}).get("translation")
        except Exception as e:
            log.debug("translate_line(%s) failed: %s", m.get("line_id"), e)
            continue
        if not tr:
            continue
        m["translation"] = tr
        m["translation_source"] = "mt"
        with cursor() as wx:
            wx.execute("UPDATE srs_moments SET translation=?, translation_source='mt' "
                       "WHERE id=? AND judged_at IS NULL", (tr, m["id"]))
        done += 1
    return done


def _repeat_counts(cx, texts: list[str]) -> dict[str, int]:
    """How often each moment text occurs in the downloaded corpus — the judge is
    told, because a text seen twice may be a recap or a song (§5.5)."""
    out: dict[str, int] = {}
    uniq = list(dict.fromkeys(t for t in texts if t))
    for i in range(0, len(uniq), 200):
        chunk = uniq[i:i + 200]
        q = ",".join("?" * len(chunk))
        try:
            for r in cx.execute(
                f"SELECT l.text, COUNT(*) AS n FROM subtitle_lines l JOIN episodes e "
                f"ON e.id = l.episode_id WHERE COALESCE(e.video_path,'') <> '' "
                f"AND l.text IN ({q}) GROUP BY l.text", chunk):
                out[r["text"]] = int(r["n"])
        except Exception as e:
            log.debug("repeat count failed: %s", e)
    return out


def _build_packets(cx, lemmas: list[str], moments_by_lemma: dict[str, list[dict]]):
    """`(packets, moment_rows_by_index)` for one judge call."""
    from .judge import WordPacket

    packets: list[WordPacket] = []
    rows_by_word: list[list[dict]] = []
    repeats = _repeat_counts(cx, [m["text"] for rows in moments_by_lemma.values()
                                  for m in rows])
    for lemma in lemmas:
        w = cx.execute("SELECT * FROM srs_words WHERE lemma=?", (lemma,)).fetchone()
        word = dict(w) if w else {"lemma": lemma}
        rows = moments_by_lemma[lemma]
        mdicts: list[dict] = []
        for m in rows:
            ep = cx.execute(
                "SELECT e.ep_number, COALESCE(e.watched,0) AS watched, t.romaji, t.english "
                "FROM episodes e LEFT JOIN titles t ON t.anilist_id=e.anilist_id WHERE e.id=?",
                (m["episode_id"],)).fetchone()
            before, after = _context_for(cx, m)
            others = []
            if m["line_id"]:
                others = [r["lemma"] for r in cx.execute(
                    "SELECT DISTINCT lemma FROM line_lemmas WHERE line_id=? AND lemma<>?",
                    (m["line_id"], lemma))][:20]
            mdicts.append({
                "moment_id": m["id"], "line_id": m["line_id"], "episode_id": m["episode_id"],
                "show": (ep["romaji"] or ep["english"]) if ep else None,
                "ep_number": ep["ep_number"] if ep else None,
                "watched": bool(ep["watched"]) if ep else False,
                "start_ms": m["start_ms"], "end_ms": m["end_ms"], "text": m["text"],
                "translation": m["translation"], "translation_source": m["translation_source"],
                "translation_shared": bool(m["translation_shared"]),
                "target_surface": m["target_surface"],
                "other_unknowns": m["other_unknowns"],
                "other_unknowns_list": _other_unknown_names(cx, m, lemma, others),
                "repeats": repeats.get(m["text"], 1),
                "context_before": before, "context_after": after,
            })
        packets.append(WordPacket(
            lemma=lemma, reading=word.get("reading"), gloss=word.get("gloss"),
            freq_rank=word.get("freq_rank"), occ=int(word.get("occ") or 0),
            eps=int(word.get("eps") or 0),
            hints=_hints_for(cx, word, mdicts), moments=mdicts,
        ))
        rows_by_word.append(rows)
    return packets, rows_by_word


def _other_unknown_names(cx, moment: dict, lemma: str, candidates: list[str]) -> list[str]:
    """Names of the OTHER not-known lemmas on the line (a hint only, §5.5)."""
    if not candidates:
        return []
    from ..learn import service as ls
    try:
        known, ignored = ls._known_lookup()
    except Exception:
        return []
    out = []
    for form in candidates:
        if form == lemma or not form:
            continue
        if ls._token_status(form, None, known, ignored) == "UNKNOWN" and ls._is_content(form):
            out.append(form)
    return out[: int(moment["other_unknowns"] or 0) or len(out)]


# ---------------------------------------------------------------------------
# verdicts (§5.5)
# ---------------------------------------------------------------------------

def _moment_for_index(rows: list[dict], num: int) -> Optional[dict]:
    """The judge answers with 1-based MOMENT numbers; tolerate a raw
    `srs_moments.id` as well."""
    if 1 <= num <= len(rows):
        return rows[num - 1]
    for r in rows:
        if int(r["id"]) == int(num):
            return r
    return None


def _accepted(word_ok: bool, verdict: dict, moment: dict) -> bool:
    """§5.5 acceptance rule."""
    return bool(
        word_ok
        and verdict["clean_utterance"]
        and float(verdict["clarity"] or 0.0) >= JUDGE_MIN_CLARITY
        and (verdict["translation_renders_word"] or not bool(moment["translation_shared"]))
    )


def _mt_translation(cx, moment: dict) -> Optional[str]:
    """A Haiku translation of THIS line for the card (§5.5). Never written back
    to `subtitle_lines` — the human cue stays the corpus truth."""
    from .. import llm as _llm

    if not _llm.available():
        return None
    ctx = ""
    if moment.get("line_id"):
        rows = cx.execute(
            "SELECT text FROM subtitle_lines WHERE episode_id=? AND idx BETWEEN ? AND ? "
            "ORDER BY idx", (moment["episode_id"], (moment["idx"] or 0) - 2,
                             (moment["idx"] or 0) + 2)).fetchall()
        ctx = "\n".join(r["text"] for r in rows)
    res = _llm.claude_json(
        "You translate Japanese anime subtitle lines to natural, concise English. "
        "Use the surrounding lines only as context; translate ONLY the target line. "
        'Return JSON {"t": "<english>"}.',
        f"Context:\n{ctx}\n\nTarget line:\n{moment['text']}",
        max_tokens=200, timeout=20.0,
    )
    tr = (res or {}).get("t") if isinstance(res, dict) else None
    tr = str(tr).strip() if tr else ""
    return tr or None


def _write_moment_verdict(wx, moment: dict, verdict: dict, model: str, accepted: bool) -> None:
    translation = moment["translation"]
    clean = (verdict.get("translation_clean") or "").strip()
    if clean and clean != (translation or ""):
        translation = clean
    wx.execute(
        "UPDATE srs_moments SET judged_at=datetime('now'), judge_model=?, clarity=?, "
        "translation_renders_word=?, clean_utterance=?, accepted=?, verdict=?, note=?, "
        "translation=?, evidence_line_ids_json=? WHERE id=?",
        (model, verdict["clarity"], 1 if verdict["translation_renders_word"] else 0,
         1 if verdict["clean_utterance"] else 0, 1 if accepted else 0,
         "accept" if accepted else "reject", verdict["note"], translation,
         json.dumps([int(i) for i in (verdict.get("evidence_line_ids") or [])]),
         moment["id"]))


def _reject_word(wx, lemma: str, reason: str, note: str, moment_lines: Optional[int]) -> None:
    wx.execute(
        "UPDATE srs_words SET judge_status='rejected', judge_reason=?, judge_note=?, "
        "judged_at=datetime('now'), judged_moment_lines=COALESCE(?, moment_lines), "
        "judge_attempts=judge_attempts+1, updated_at=datetime('now') WHERE lemma=?",
        (reason or "other", note[:400] if note else None, moment_lines, lemma))


def _live_duplicate(cx, lemma: str, reading: str, gloss: Optional[str],
                    accepted_now: dict[str, tuple[str, str]]) -> Optional[str]:
    """Another lemma with a live card (or accepted earlier in this run) sharing
    the reading AND the first JMdict sense (§5.5 duplicate collapse)."""
    sense = _first_sense(gloss)
    if not reading:
        return None
    for other, (o_reading, o_sense) in accepted_now.items():
        if other != lemma and o_reading == reading and sense and o_sense == sense:
            return other
    rows = cx.execute(
        "SELECT lemma, reading, gloss FROM srs_cards WHERE reading=? AND lemma<>? "
        "AND state NOT IN ('rejected')", (reading, lemma)).fetchall()
    for r in rows:
        if not sense or _first_sense(r["gloss"]) == sense:
            return r["lemma"]
    return None


def _show_of(cx, episode_id) -> Optional[str]:
    """`titles.romaji|english` of an episode — the anime rule for sibling cards."""
    if not episode_id:
        return None
    r = cx.execute(
        "SELECT t.romaji, t.english FROM episodes e LEFT JOIN titles t ON t.anilist_id=e.anilist_id "
        "WHERE e.id=?", (episode_id,)).fetchone()
    return (r["romaji"] or r["english"]) if r else None


def _blind_gate(cx, lemmas: list[str], moments_by_lemma: dict[str, list[dict]]) -> dict[int, dict]:
    """The translation-blind clarity test for every pending moment of a batch
    (2026-09-03): `{srs_moments.id: verdict}`. `BLIND_WORKERS` calls run in
    parallel, each on its own connection. A moment without a verdict (line
    gone, LLM failure) counts as failed."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from .blind_judge import coarse_pos, judge_moment_by_id

    items: list[tuple[int, dict]] = []
    for lemma in lemmas:
        w = cx.execute("SELECT gloss, pos1 FROM srs_words WHERE lemma=?", (lemma,)).fetchone()
        ref = (w["gloss"] if w else None) or None
        pos = coarse_pos(w["pos1"]) if w else None
        for m in moments_by_lemma.get(lemma) or []:
            if not m.get("line_id"):
                continue
            items.append((int(m["id"]), dict(
                lemma=lemma, line_id=int(m["line_id"]), target_surface=m.get("target_surface"),
                pos=pos, reference=ref, translation=m.get("translation"))))
    out: dict[int, dict] = {}
    if not items:
        return out
    with ThreadPoolExecutor(max_workers=min(BLIND_WORKERS, len(items))) as ex:
        futs = {ex.submit(judge_moment_by_id, **kw): mid for mid, kw in items}
        for fut in as_completed(futs):
            mid = futs[fut]
            try:
                out[mid] = fut.result()
            except Exception as e:                       # never lose the batch over one moment
                log.warning("blind judge failed for moment %s: %s", mid, e)
                out[mid] = {"passed": False, "error": f"{type(e).__name__}: {e}",
                            "inferability": "none", "confidence": 0.0, "match": "unchecked",
                            "best_guess": "", "cues": "", "evidence_line_ids": []}
    return out


def _blind_fail_verdict(bv: Optional[dict]) -> dict:
    """The `srs_moments` verdict row for a moment the blind gate rejected."""
    from .blind_judge import fail_note

    return {"clarity": float((bv or {}).get("confidence") or 0.0),
            "translation_renders_word": False, "clean_utterance": False,
            "note": fail_note(bv), "translation_clean": "", "evidence_line_ids": []}


def _create_card(cx, word: dict, moment: dict, verdict: dict, judged: dict, *,
                 position: str = "bottom", alt_ids: Optional[list[int]] = None,
                 study_now: bool = False) -> Optional[int]:
    """Snapshot the accepted moment and create the card (§5.7). Returns the card
    id, or None when the snapshot is impossible (line gone / invalid surface)."""
    import dataclasses

    from . import service as _svc
    from .snapshot import CardSpec, snapshot_line

    lemma = word["lemma"]
    # The judge already returns a trimmed cue per moment (amendments §B2). When it
    # did, `snapshot_line`'s own Haiku cleaning call would be thrown away below, so
    # skip it (`clean=False`) — one saved LLM call per generated card.
    judge_clean = (verdict.get("translation_clean") or "").strip()
    try:
        # the judge's evidence lines become the moment's `extend` (§6.2): the
        # clip covers them and the card front shows them around the target.
        snap = snapshot_line(moment["line_id"], lemma, moment["target_surface"],
                             reading=judged.get("reading") or word.get("reading"),
                             clean=not judge_clean,
                             evidence_line_ids=verdict.get("evidence_line_ids") or [])
    except (LookupError, ValueError) as e:
        log.warning("snapshot failed for %s (moment %s): %s", lemma, moment["id"], e)
        return None
    fields = {f.name for f in dataclasses.fields(CardSpec)}
    kwargs = {k: v for k, v in (snap or {}).items() if k in fields}
    kwargs["lemma"] = lemma
    spec = CardSpec(**kwargs)
    spec.reading = judged.get("reading") or word.get("reading") or spec.reading
    spec.pos = judged.get("pos") or word.get("pos1")
    spec.gloss = word.get("gloss") or spec.gloss
    spec.meaning_short = judged.get("meaning_short")
    spec.meaning_full = judged.get("meaning_full")
    spec.why_clear = judged.get("why_clear")
    spec.usage_note = judged.get("usage_note") or ""
    spec.tags = judged.get("tags") or []
    spec.freq_rank = word.get("freq_rank")
    spec.source = "auto"
    spec.score = word.get("score")
    spec.clarity = verdict.get("clarity")
    spec.usefulness = judged.get("usefulness")
    spec.priority = judged.get("priority")
    spec.alt_moment_ids = alt_ids or []

    # translation: cleaned human cue → MT when the human one does not render the
    # word (§5.5 / amendments §B2)
    clean = judge_clean
    if clean:
        spec.translation = clean
        spec.translation_source = spec.translation_source or "human"
    if not verdict.get("translation_renders_word") or not spec.translation:
        mt = _mt_translation(cx, moment)
        if mt:
            spec.translation = mt
            spec.translation_source = "mt"
    return int(_svc.create_card(spec, position=position, study_now=study_now))


def _apply_word(cx, wx, word: dict, rows: list[dict], judged: dict, model: str,
                accepted_now: dict[str, tuple[str, str]], *, position: str = "bottom",
                card_id: Optional[int] = None,
                blind: Optional[dict[int, dict]] = None) -> dict:
    """Write one word's verdicts, create its card(s). Returns per-word counters.

    `blind` (2026-09-03) carries the translation-blind verdicts by moment id:
    a passing blind confidence IS the moment's clarity and its cues ARE the
    card's `why_clear`; the writer's own clarity only goes into the note. Up to
    `MAX_CARDS_PER_LEMMA` cards are created per word, each from a different
    anime (`franchise_key`) — the primary at `position`, siblings at the bottom.
    """
    from .blind_judge import fail_note, why_clear_text
    from .franchise import franchise_key

    lemma = word["lemma"]
    stats = {"moments": 0, "accepted": 0, "rejected": 0, "cards": 0}
    word_ok = bool(judged.get("word_ok"))
    by_row: dict[int, dict] = {}
    for v in judged.get("moments") or []:
        row = _moment_for_index(rows, int(v.get("moment") or 0))
        if row is None:
            continue
        by_row[int(row["id"])] = v

    accepted_rows: list[tuple[dict, dict]] = []
    for row in rows:
        v = by_row.get(int(row["id"]))
        if v is None:
            v = {"clarity": 0.0, "translation_renders_word": False, "clean_utterance": False,
                 "note": "not_rated", "translation_clean": "", "evidence_line_ids": []}
        if blind is not None:
            bv = blind.get(int(row["id"]))
            v = dict(v)
            if not (bv and bv.get("passed")):
                v["clarity"] = float((bv or {}).get("confidence") or 0.0)
                v["clean_utterance"] = False            # a blind-failed moment never passes
                v["evidence_line_ids"] = []
                v["note"] = (fail_note(bv) + " · " + str(v.get("note") or ""))[:300]
            else:
                v["note"] = (f"writer clarity {float(v.get('clarity') or 0):.2f} · blind "
                             f"{bv.get('inferability')} {float(bv.get('confidence') or 0):.2f} · "
                             f"{v.get('note') or ''}")[:300]
                v["clarity"] = float(bv.get("confidence") or 0.0)
                # the clip must cover the lines the blind inference leaned on AND
                # the lines the writer names for a complete utterance (union,
                # blind first; build_extend validates and caps them)
                merged: list[int] = []
                for lid in (*(bv.get("evidence_line_ids") or []), *(v.get("evidence_line_ids") or [])):
                    try:
                        lid = int(lid)
                    except (TypeError, ValueError):
                        continue
                    if lid > 0 and lid not in merged:
                        merged.append(lid)
                v["evidence_line_ids"] = merged
        ok = _accepted(word_ok, v, row)
        _write_moment_verdict(wx, row, v, model, ok)
        stats["moments"] += 1
        if ok:
            accepted_rows.append((row, v))

    if not word_ok:
        _reject_word(wx, lemma, judged.get("reason_category") or "other",
                     judged.get("why_clear") or "", int(word.get("moment_lines") or 0))
        stats["rejected"] = 1
        return stats
    if not accepted_rows:
        _reject_word(wx, lemma, "no_clear_moment", judged.get("why_clear") or "",
                     int(word.get("moment_lines") or 0))
        stats["rejected"] = 1
        return stats

    # best_moment first, then clarity; prefer a moment whose video is on disk
    best_row = _moment_for_index(rows, int(judged.get("best_moment") or 0))
    def _key(pair):
        row, v = pair
        from . import clips
        return (0 if best_row is not None and row["id"] == best_row["id"] else 1,
                0 if clips.source_available(row["episode_id"]) else 1,
                -float(v.get("clarity") or 0.0))
    accepted_rows.sort(key=_key)
    primary, primary_v = accepted_rows[0]
    alt_ids = [int(r["id"]) for r, _v in accepted_rows[1:]]

    reading = judged.get("reading") or word.get("reading") or ""
    dup = _live_duplicate(cx, lemma, reading, word.get("gloss"), accepted_now)
    if dup:
        _reject_word(wx, lemma, f"duplicate_of:{dup}", judged.get("why_clear") or "",
                     int(word.get("moment_lines") or 0))
        stats["rejected"] = 1
        return stats

    if card_id is not None:
        wx.execute(
            "UPDATE srs_cards SET alt_moment_ids_json=? WHERE id=?",
            (json.dumps([int(primary["id"])] + alt_ids), card_id))
        wx.execute("UPDATE srs_words SET judge_status='accepted', judged_at=datetime('now'), "
                   "judge_note=?, card_id=?, updated_at=datetime('now') WHERE lemma=?",
                   (judged.get("why_clear"), card_id, lemma))
        stats["accepted"] = 1
        return stats

    wx.commit()          # let create_card use its own connection safely
    from .service import SrsConflict

    created: list[int] = []
    used_franchises: set[str] = set()
    for row, v in accepted_rows:
        if len(created) >= MAX_CARDS_PER_LEMMA:
            break
        fr = franchise_key(_show_of(cx, row["episode_id"]))
        if created and fr in used_franchises:
            continue                                     # siblings: different anime only
        alts = [int(r["id"]) for r, _x in accepted_rows if r["id"] != row["id"]]
        judged_card = judged
        bv = (blind or {}).get(int(row["id"]))
        if bv and bv.get("passed"):
            judged_card = dict(judged, why_clear=why_clear_text(bv))
        try:
            new_id = _create_card(cx, word, row, v, judged_card,
                                  position=position if not created else "bottom", alt_ids=alts)
        except SrsConflict as e:                         # word full / anime taken
            log.info("sibling card for %s skipped: %s", lemma, e)
            new_id = None
        if new_id:
            created.append(new_id)
            used_franchises.add(fr)
    if not created:
        wx.execute("UPDATE srs_words SET judge_status='error', judge_note=?, "
                   "judge_attempts=judge_attempts+1, updated_at=datetime('now') WHERE lemma=?",
                   ("snapshot failed for every accepted moment", lemma))
        return stats
    wx.execute("UPDATE srs_words SET judge_status='accepted', judge_reason=NULL, judge_note=?, "
               "judged_at=datetime('now'), judged_moment_lines=moment_lines, card_id=?, "
               "updated_at=datetime('now') WHERE lemma=?",
               (judged.get("why_clear"), created[0], lemma))
    accepted_now[lemma] = (reading, _first_sense(word.get("gloss")))
    stats["accepted"] = 1
    stats["cards"] = len(created)
    return stats


# ---------------------------------------------------------------------------
# jobs (§9.1)
# ---------------------------------------------------------------------------

def _no_key_reset(run_id: int) -> None:
    """No Anthropic key: park the run instead of burning job attempts (§5.13)."""
    from ..db import cursor, kv_get, kv_set

    with cursor() as wx:
        lemmas = [r["lemma"] for r in wx.execute(
            "SELECT DISTINCT lemma FROM srs_moments WHERE run_id=?", (run_id,))]
        wx.execute("DELETE FROM srs_moments WHERE run_id=? AND judged_at IS NULL", (run_id,))
        for chunk in (lemmas[i:i + 400] for i in range(0, len(lemmas), 400)):
            q = ",".join("?" * len(chunk))
            wx.execute(f"UPDATE srs_words SET judge_status='unjudged' "
                       f"WHERE judge_status='pending' AND lemma IN ({q})", chunk)
        wx.execute("UPDATE srs_generation_runs SET state='error', error='llm_unavailable', "
                   "finished_at=datetime('now') WHERE id=? AND state IN ('planning','judging')",
                   (run_id,))
    day = _srs_day()
    if (kv_get("srs.nokey.warned_day") or "") != day:
        kv_set("srs.nokey.warned_day", day)
        _record("SRS generation paused — no Anthropic API key", "warning",
                "Add ANTHROPIC_API_KEY to .env, or add cards from Moments.")
    _publish({"what": "generation", "run_id": run_id, "state": "error"})


def _cost(model: str, in_tok: int, out_tok: int) -> float:
    pin, pout = _PRICES.get(model, (3.0, 15.0))
    return (in_tok * pin + out_tok * pout) / 1_000_000.0


def _finish_check(run_id: int) -> None:
    """Guarded finish: exactly one worker records the success event (§5.8)."""
    from ..db import connect, cursor

    cx = connect()
    try:
        left = cx.execute(
            "SELECT COUNT(*) AS n FROM srs_moments WHERE run_id=? AND judged_at IS NULL",
            (run_id,)).fetchone()["n"]
        if left:
            return
        run = cx.execute("SELECT * FROM srs_generation_runs WHERE id=?", (run_id,)).fetchone()
    finally:
        cx.close()
    if run is None:
        return
    with cursor() as wx:
        n = wx.execute(
            "UPDATE srs_generation_runs SET state='done', finished_at=datetime('now') "
            "WHERE id=? AND state='judging'", (run_id,)).rowcount
    if n != 1:
        return
    cx = connect()
    try:
        run = cx.execute("SELECT * FROM srs_generation_runs WHERE id=?", (run_id,)).fetchone()
    finally:
        cx.close()
    model = ""
    try:
        from ..config import settings as _settings
        model = _settings.translation_model
    except Exception:
        pass
    cost = _cost(model, int(run["llm_in_tokens"] or 0), int(run["llm_out_tokens"] or 0))
    if "blind_in_tokens" in run.keys():
        cost += _cost(BLIND_JUDGE_MODEL, int(run["blind_in_tokens"] or 0),
                      int(run["blind_out_tokens"] or 0))
    created = int(run["cards_created"] or 0)
    _record(f"Added {created} new card{'s' if created != 1 else ''} to your stack",
            "success" if created else "info",
            f"judged {int(run['words_planned'] or 0)} words · accepted "
            f"{int(run['accepted'] or 0)} · blind gate {BLIND_JUDGE_MODEL} + {model} ≈ ${cost:.2f}")
    _publish({"what": "generation", "run_id": run_id, "state": "done"})


def _job_generate(payload: dict) -> None:
    """`srs_generate {trigger, want?}` — stale sweep + census + plan + fan-out.
    Raises on a census/DB error (§9.1)."""
    from ..db import connect

    trigger = str(payload.get("trigger") or "manual")
    want = payload.get("want")
    cx = connect()
    try:
        sweep_stale_runs(cx)
    finally:
        cx.close()
    plan_run(trigger, int(want) if want is not None else None)


def _job_judge_batch(payload: dict) -> None:
    """`srs_judge_batch {run_id, batch_no}` — one judge call, heartbeat, card
    creation, guarded finish. Raises when the judge returns `None` twice."""
    from .. import llm as _llm
    from ..db import connect, cursor
    from . import judge as _judge
    from .settings import get_settings

    run_id = int(payload.get("run_id") or 0)
    batch_no = int(payload.get("batch_no") or 0)
    if not run_id:
        raise ValueError("srs_judge_batch payload without run_id")
    cfg = get_settings()

    with cursor() as wx:
        wx.execute("UPDATE srs_generation_runs SET heartbeat_at=datetime('now') WHERE id=?",
                   (run_id,))

    cx = connect()
    try:
        run = cx.execute("SELECT * FROM srs_generation_runs WHERE id=?", (run_id,)).fetchone()
        if run is None:
            log.info("srs_judge_batch: run %s is gone", run_id)
            return
        if run["state"] not in ("planning", "judging"):
            log.info("srs_judge_batch: run %s is %s", run_id, run["state"])
            return
        rows = cx.execute(
            "SELECT * FROM srs_moments WHERE run_id=? AND batch_no=? AND judged_at IS NULL "
            "ORDER BY lemma, line_score DESC, id", (run_id, batch_no)).fetchall()
        if not rows:
            _finish_check(run_id)
            return
        if not _llm.available():
            _no_key_reset(run_id)
            return

        moments_by_lemma: dict[str, list[dict]] = {}
        for r in rows:
            moments_by_lemma.setdefault(r["lemma"], []).append(dict(r))
        lemmas = list(moments_by_lemma)
        _fill_missing_translations(moments_by_lemma)

        # --- translation-blind clarity gate (2026-09-03) ---------------------
        # Every moment must be inferable from the Japanese alone BEFORE the
        # writer sees it. Failed moments are recorded here; a word with no
        # passing moment is rejected without a writer call.
        b_in0, b_out0 = _judge._usage_snapshot(BLIND_JUDGE_MODEL)
        blind = _blind_gate(cx, lemmas, moments_by_lemma)
        b_in1, b_out1 = _judge._usage_snapshot(BLIND_JUDGE_MODEL)
        passing: dict[str, list[dict]] = {}
        failed_rows: list[dict] = []
        for lemma in lemmas:
            keep = [m for m in moments_by_lemma[lemma]
                    if (blind.get(int(m["id"])) or {}).get("passed")]
            if keep:
                passing[lemma] = keep
            failed_rows.extend(m for m in moments_by_lemma[lemma] if m not in keep)
        gate_rejected = 0
        with cursor() as wx:
            for m in failed_rows:
                _write_moment_verdict(wx, m, _blind_fail_verdict(blind.get(int(m["id"]))),
                                      f"blind:{BLIND_JUDGE_MODEL}", False)
            for lemma in lemmas:
                if lemma in passing:
                    continue
                w_row = cx.execute("SELECT moment_lines FROM srs_words WHERE lemma=?",
                                   (lemma,)).fetchone()
                notes = "; ".join(_blind_fail_verdict(blind.get(int(m["id"])))["note"]
                                  for m in moments_by_lemma[lemma][:3])
                _reject_word(wx, lemma, "no_clear_moment", f"blind gate: {notes}",
                             int(w_row["moment_lines"] or 0) if w_row else 0)
                gate_rejected += 1
            wx.execute(
                "UPDATE srs_generation_runs SET moments_judged=moments_judged+?, "
                "rejected=rejected+?, blind_in_tokens=blind_in_tokens+?, "
                "blind_out_tokens=blind_out_tokens+?, heartbeat_at=datetime('now') WHERE id=?",
                (len(failed_rows), gate_rejected, max(0, b_in1 - b_in0),
                 max(0, b_out1 - b_out0), run_id))
        if not passing:
            log.info("run %s batch %s: blind gate passed no moment (%d words rejected)",
                     run_id, batch_no, gate_rejected)
            with cursor() as wx:
                wx.execute("UPDATE srs_generation_runs SET batches_done=batches_done+1 "
                           "WHERE id=?", (run_id,))
            _publish({"what": "generation", "run_id": run_id, "state": "judging"})
            _finish_check(run_id)
            return
        lemmas = list(passing)
        moments_by_lemma = passing
        packets, rows_by_word = _build_packets(cx, lemmas, moments_by_lemma)

        result = _judge.judge_packets(packets, cfg)
        if result is None:
            time.sleep(5)
            result = _judge.judge_packets(packets, cfg)
        if result is None:
            reason = ""
            try:
                reason = str(_llm.last_error())          # WP-A adds last_error()
            except Exception:
                reason = "no response"
            raise RuntimeError(f"judge unavailable: {reason}")

        words_by_index: dict[int, dict] = {}
        for w in result.words:
            words_by_index[int(w.get("word") or 0)] = w

        totals = {"moments": 0, "accepted": 0, "rejected": 0, "cards": 0}
        accepted_now: dict[str, tuple[str, str]] = {}
        covered = 0
        with cursor() as wx:
            for i, lemma in enumerate(lemmas, start=1):
                judged = words_by_index.get(i)
                if judged is None:
                    wx.execute("UPDATE srs_words SET judge_attempts=judge_attempts+1, "
                               "updated_at=datetime('now') WHERE lemma=?", (lemma,))
                    wx.execute("UPDATE srs_words SET judge_status='error', "
                               "judge_note='judge answer did not cover this word' "
                               "WHERE lemma=? AND judge_attempts>=3", (lemma,))
                    continue
                covered += 1
                w_row = cx.execute("SELECT * FROM srs_words WHERE lemma=?", (lemma,)).fetchone()
                word = dict(w_row) if w_row else {"lemma": lemma}
                st = _apply_word(cx, wx, word, rows_by_word[i - 1], judged, result.model,
                                 accepted_now, blind=blind)
                for k in totals:
                    totals[k] += st[k]
            wx.execute(
                "UPDATE srs_generation_runs SET batches_done=batches_done+1, "
                "moments_judged=moments_judged+?, accepted=accepted+?, rejected=rejected+?, "
                "cards_created=cards_created+?, llm_calls=llm_calls+1, "
                "llm_in_tokens=llm_in_tokens+?, llm_out_tokens=llm_out_tokens+?, "
                "heartbeat_at=datetime('now') WHERE id=?",
                (totals["moments"], totals["accepted"], totals["rejected"], totals["cards"],
                 result.in_tokens, result.out_tokens, run_id))
        log.info("run %s batch %s: %d words covered, %d accepted, %d cards",
                 run_id, batch_no, covered, totals["accepted"], totals["cards"])
        _publish({"what": "generation", "run_id": run_id, "state": "judging"})
    finally:
        cx.close()
    _finish_check(run_id)


def _job_find_moments(payload: dict) -> None:
    """`srs_find_moments {lemma, card_id?}` — rank ≤5 unseen lines for one word,
    one judge call, create the card at the top (or extend an existing card's
    alternates). Raises when the judge returns `None` twice."""
    from .. import llm as _llm
    from ..db import connect, cursor
    from . import census, judge as _judge
    from .settings import get_settings

    lemma = str(payload.get("lemma") or "").strip()
    card_id = payload.get("card_id")
    if not lemma:
        raise ValueError("srs_find_moments payload without lemma")
    cfg = get_settings()

    moments = census.rank_moments(lemma, cfg, limit=MOMENTS_PER_WORD, exclude_judged=True)
    if not moments:
        _record(f"No clear moment found for 〈{lemma}〉 yet", "info")
        return
    if not _llm.available():
        from ..db import kv_get, kv_set
        day = _srs_day()
        if (kv_get("srs.nokey.warned_day") or "") != day:
            kv_set("srs.nokey.warned_day", day)
            _record("SRS generation paused — no Anthropic API key", "warning")
        return

    cx = connect()
    try:
        ids = [m.moment_id for m in moments if m.moment_id]
        q = ",".join("?" * len(ids))
        rows = [dict(r) for r in cx.execute(
            f"SELECT * FROM srs_moments WHERE id IN ({q}) ORDER BY line_score DESC", ids)]
        _fill_missing_translations({lemma: rows})

        # translation-blind clarity gate (2026-09-03): only inferable moments
        # reach the writer; a word with none is rejected (unless we were only
        # looking for alternates of an existing card).
        blind = _blind_gate(cx, [lemma], {lemma: rows})
        keep = [m for m in rows if (blind.get(int(m["id"])) or {}).get("passed")]
        with cursor() as wx:
            for m in rows:
                if m not in keep:
                    _write_moment_verdict(wx, m, _blind_fail_verdict(blind.get(int(m["id"]))),
                                          f"blind:{BLIND_JUDGE_MODEL}", False)
            if not keep and card_id is None:
                _reject_word(wx, lemma, "no_clear_moment", "blind gate: no inferable moment", None)
        if not keep:
            _record(f"No clear moment found for 〈{lemma}〉 yet", "info")
            return
        rows = keep
        packets, rows_by_word = _build_packets(cx, [lemma], {lemma: rows})
        result = _judge.judge_packets(packets, cfg)
        if result is None:
            time.sleep(5)
            result = _judge.judge_packets(packets, cfg)
        if result is None:
            reason = ""
            try:
                reason = str(_llm.last_error())
            except Exception:
                reason = "no response"
            raise RuntimeError(f"judge unavailable: {reason}")
        judged = result.words[0] if result.words else None
        if judged is None:
            raise RuntimeError("judge answer did not cover the word")
        w_row = cx.execute("SELECT * FROM srs_words WHERE lemma=?", (lemma,)).fetchone()
        word = dict(w_row) if w_row else {"lemma": lemma}
        with cursor() as wx:
            st = _apply_word(cx, wx, word, rows_by_word[0], judged, result.model, {},
                             position="top", card_id=int(card_id) if card_id else None,
                             blind=blind)
        if not st["accepted"]:
            _record(f"No clear moment found for 〈{lemma}〉 yet", "info")
    finally:
        cx.close()
        census.invalidate_cache()


def register_jobs() -> None:
    """Register the real generation handlers (§9.1): `srs_generate` (enqueued at
    priority 60), `srs_judge_batch` (55), `srs_find_moments` (45)."""
    from ..jobs.service import register
    register("srs_generate", _job_generate)
    register("srs_judge_batch", _job_judge_batch)
    register("srs_find_moments", _job_find_moments)
