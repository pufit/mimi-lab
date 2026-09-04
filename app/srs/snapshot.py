"""Card snapshots — the self-contained moment stored on `srs_cards` (§5.7).

`norm_text`, `dialogue_neighbours` and `continuation` are the ONE definition of
each rule; census (WP-B) re-exports/imports them so the generator and the card
assembler can never drift apart (§11). `snapshot_line` itself is WP-A day 1.
"""
from __future__ import annotations

import json
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Optional

from .constants import (
    BLEED_MS,
    CLIP_LEAD_MS,
    CLIP_TAIL_MS,
    EXTENDED_MAX_CLIP_MS,
    MAX_EVIDENCE_IDX_DISTANCE,
    MAX_EVIDENCE_LINES,
    SIGN_EXTRA_MS,
)

log = logging.getLogger("mimi_lab.srs.snapshot")

# Invisible bidi/zero-width controls found in real subtitle lines (a real show's S2,
# amendments §B2). They break substring matching and highlight offsets, so every
# text that enters the SRS is stripped of them before anything else happens.
BIDI_RE = re.compile(
    "[\u200b\u200c\u200d\u200e\u200f\u202a-\u202e\u2066-\u2069\ufeff]"
)

# Music/karaoke marks are not punctuation to Unicode but carry no identity.
_NOTE_CHARS = "♪♫♬♩♭♯〜～"

# The next cue must start within this gap for the sentence to be treated as
# continuing into it (§5.4 continuation rule).
CONTINUATION_GAP_MS = 250

# Characters that end a sentence: anything else means the cue may continue.
_TERMINAL_CHARS = "。．.！!？?」』）)〉》\u201d\"'"
# Characters that explicitly signal "to be continued in the next cue".
_CONTINUE_CHARS = "―—‐-…、,，"


def strip_bidi(text: str) -> str:
    """Remove invisible bidi/zero-width controls (amendments §B2)."""
    if not text:
        return ""
    return BIDI_RE.sub("", text)


def norm_text(text: str) -> str:
    """Durable identity of a subtitle line: spaces, punctuation and music marks
    stripped (§2.1, §5.2). Together with `episode_id` (and `start_ms` as a
    tie-break) this survives a subtitle re-ingest, which renumbers every
    `subtitle_lines.id` (§2.7)."""
    if not text:
        return ""
    out: list[str] = []
    for ch in strip_bidi(text):
        if ch.isspace() or ch in _NOTE_CHARS:
            continue
        if unicodedata.category(ch).startswith("P"):
            continue
        out.append(ch)
    return "".join(out)


def _row_to_line(r: Any) -> dict:
    return {
        "line_id": r["id"],
        "idx": r["idx"],
        "start_ms": r["start_ms"],
        "end_ms": r["end_ms"],
        "text": strip_bidi(r["text"] or ""),
        "text_furigana": r["text_furigana"],
        "translation": r["translation"],
    }


def dialogue_neighbours(cx, line: dict, n: int = 2) -> tuple[list[dict], list[dict]]:
    """The `n` nearest DIALOGUE lines before and after `line`, by time (§5.4).

    "Dialogue" = not `census.is_junk_line` (ASS drawings, sign/no-Japanese lines,
    simplified-Chinese lines) and not a cue whose span fully contains the target
    (long-running signs). Ordering is by time, never by `idx±1` — subtitle tracks
    interleave signs and dialogue.

    `line` is a dict with at least `episode_id`, `start_ms`, `end_ms` and
    `line_id` (or `id`). Returns `(before, after)`, both in chronological order;
    missing neighbours (episode start/end) are simply absent.

    Lyric lines are NOT filtered here: that needs corpus-wide repeat counts,
    which only the census has (§5.2). The census applies its own lyric filter on
    top of this list.
    """
    from .census import is_junk_line          # late import: census is WP-B

    if n <= 0:
        return [], []
    episode_id = line.get("episode_id")
    start_ms = int(line.get("start_ms") or 0)
    end_ms = int(line.get("end_ms") or 0)
    line_id = line.get("line_id", line.get("id"))
    if episode_id is None:
        return [], []

    fetch = max(4 * n, 8)
    cols = "id, idx, start_ms, end_ms, text, text_furigana, translation"
    target_idx = line.get("idx")
    if target_idx is None and line_id is not None:
        r = cx.execute("SELECT idx FROM subtitle_lines WHERE id=?", (line_id,)).fetchone()
        target_idx = r["idx"] if r else None
    if target_idx is None:
        target_idx = -1                 # unknown row order: same-start rows count as "after"
    span_ms = max(0, end_ms - start_ms)

    def _usable(r) -> bool:
        if line_id is not None and r["id"] == line_id:
            return False
        if not (r["text"] or "").strip():
            return False
        if is_junk_line(r["text"] or ""):
            return False
        # A cue whose span contains the target AND runs well beyond it is a
        # long-running sign, not a neighbour. A row with the same (or a barely
        # wider) span is the other half of a two-line cue — the ingest stores
        # each line of a cue as its own row with identical timings — and that
        # IS a neighbour. (2026-09-03: such rows used to be dropped as "signs",
        # so the judge, the card context and the evidence saw half a sentence.)
        if r["start_ms"] <= start_ms and r["end_ms"] >= end_ms:
            if (r["end_ms"] - r["start_ms"]) - span_ms > SIGN_EXTRA_MS:
                return False
        return True

    # Cue order = (start time, row order): the rows of one two-line cue share
    # their timings and are told apart by idx; a previous cue that overlaps the
    # target (another speaker talking over it) still counts as "before".
    before_rows = cx.execute(
        f"SELECT {cols} FROM subtitle_lines WHERE episode_id=? "
        f"AND (start_ms < ? OR (start_ms = ? AND idx < ?)) "
        f"ORDER BY start_ms DESC, idx DESC, id DESC LIMIT ?",
        (episode_id, start_ms, start_ms, target_idx, fetch),
    ).fetchall()
    before = [_row_to_line(r) for r in before_rows if _usable(r)][:n]
    before.reverse()

    after_rows = cx.execute(
        f"SELECT {cols} FROM subtitle_lines WHERE episode_id=? "
        f"AND (start_ms > ? OR (start_ms = ? AND idx > ?)) "
        f"ORDER BY start_ms ASC, idx ASC, id ASC LIMIT ?",
        (episode_id, start_ms, start_ms, target_idx, fetch),
    ).fetchall()
    after = [_row_to_line(r) for r in after_rows if _usable(r)][:n]
    return before, after


def same_cue_lines(cx, line: dict) -> list[int]:
    """Ids of the OTHER rows of the target's two-line cue — same episode,
    identical start/end — in row order, junk/empty excluded (2026-09-03). They
    were on screen together with the target, so they are always part of the
    moment (shown on the front, covered by the clip)."""
    from .census import is_junk_line          # late import: census is WP-B

    episode_id = line.get("episode_id")
    target_id = line.get("line_id", line.get("id"))
    if episode_id is None or line.get("start_ms") is None:
        return []
    out: list[int] = []
    for r in cx.execute(
        "SELECT id, text FROM subtitle_lines WHERE episode_id=? AND start_ms=? AND end_ms=? "
        "AND id<>? ORDER BY idx, id",
        (episode_id, int(line.get("start_ms") or 0), int(line.get("end_ms") or 0),
         int(target_id) if target_id is not None else -1),
    ):
        if (r["text"] or "").strip() and not is_junk_line(r["text"] or ""):
            out.append(int(r["id"]))
    return out


def continuation(line: dict, next_line: Optional[dict]) -> list[dict]:
    """`[]`, or `[{line_id, start_ms, end_ms, text}]` when the sentence continues
    into the next dialogue cue (§5.4).

    Fires when the next cue starts within `CONTINUATION_GAP_MS` of this one AND
    this cue either ends with a continuation mark (`―—…、`) or has no terminal
    punctuation at all. Playback needs only the timings, so the result survives a
    re-ingest even when `line_id` goes stale (§2.4 `extend_json`).
    """
    if not next_line:
        return []
    text = strip_bidi((line.get("text") or "")).strip()
    if not text:
        return []
    gap = int(next_line.get("start_ms") or 0) - int(line.get("end_ms") or 0)
    if gap > CONTINUATION_GAP_MS:
        return []
    last = text[-1]
    continues = last in _CONTINUE_CHARS or last not in _TERMINAL_CHARS
    if not continues:
        return []
    return [{
        "line_id": next_line.get("line_id", next_line.get("id")),
        "idx": next_line.get("idx"),
        "start_ms": int(next_line.get("start_ms") or 0),
        "end_ms": int(next_line.get("end_ms") or 0),
        "text": strip_bidi(next_line.get("text") or ""),
        "role": "continuation",
    }]


# ---------------------------------------------------------------------------
# Evidence lines (§5.7 / §6.2 "Evidence lines")
# ---------------------------------------------------------------------------

def _extend_span_ms(line: dict, entries: list[dict]) -> int:
    """The clip duration the `entries` imply (padding included) — the quantity
    `EXTENDED_MAX_CLIP_MS` caps."""
    ls = int(line.get("start_ms") or 0)
    le = int(line.get("end_ms") or 0)
    first = min([ls] + [int(x.get("start_ms") or 0) for x in entries])
    last = max([le] + [int(x.get("end_ms") or 0) for x in entries])
    return max(0, last - first) + CLIP_LEAD_MS + CLIP_TAIL_MS


def sort_extend(entries: list[dict]) -> list[dict]:
    """Chronological order (the order the lines are heard and displayed)."""
    return sorted(
        entries,
        key=lambda x: (int(x.get("start_ms") or 0), int(x.get("idx") or 0),
                       int(x.get("line_id") or 0)),
    )


def truncate_extend(line: dict, entries: list[dict],
                    cap_ms: int = EXTENDED_MAX_CLIP_MS) -> tuple[list[dict], bool]:
    """Drop the farthest EVIDENCE lines until the implied clip fits `cap_ms`
    (§6.2). Continuation lines are part of the sentence and are never dropped.
    Returns `(kept, truncated)`."""
    kept = sort_extend(entries)
    ls = int(line.get("start_ms") or 0)
    truncated = False
    while kept and _extend_span_ms(line, kept) > cap_ms:
        droppable = [i for i, x in enumerate(kept)
                     if (x.get("role") or "evidence") == "evidence"]
        if not droppable:
            break
        far = max(droppable, key=lambda i: abs(int(kept[i].get("start_ms") or 0) - ls))
        kept.pop(far)
        truncated = True
    return kept, truncated


def build_extend(
    cx,
    line: dict,
    evidence_line_ids: Optional[list] = None,
    *,
    next_line: Optional[dict] = None,
    allowed_ids: Optional[set] = None,
    cap_ms: int = EXTENDED_MAX_CLIP_MS,
) -> tuple[list[dict], bool]:
    """Every extra dialogue line the MOMENT includes, chronologically (§5.7).

    `extend` used to hold only the continuation cue; it now holds every line the
    clip window must cover: the `evidence_line_ids` the judge/curation named
    (`role='evidence'`, before AND after the target) plus the continuation cue
    (`role='continuation'`). Each entry is
    `{line_id, idx, start_ms, end_ms, text, role}`.

    An evidence id is accepted only when it resolves to a row in the SAME episode
    that is a neighbour of the target: either in `allowed_ids` (the ±2 dialogue
    neighbours the caller already computed) or within `MAX_EVIDENCE_IDX_DISTANCE`
    subtitle indices. At most `MAX_EVIDENCE_LINES` are kept, and the farthest are
    dropped until the implied clip fits `cap_ms`.

    Returns `(entries, truncated)`; `truncated` is True when a resolved evidence
    line was dropped for the cap (recorded as `extend_truncated` in meta.json).
    """
    target_id = line.get("line_id", line.get("id"))
    episode_id = line.get("episode_id")
    target_idx = line.get("idx")
    entries: list[dict] = []
    seen: set[int] = set()

    # the other line(s) of the target's own two-line cue always belong to the
    # moment (2026-09-03) — they come first and are never "far"
    same_cue = same_cue_lines(cx, line)
    wanted = list(same_cue) + [x for x in (evidence_line_ids or [])]

    for raw in wanted:
        try:
            lid = int(raw)
        except (TypeError, ValueError):
            continue
        if lid in seen or (target_id is not None and lid == int(target_id)):
            continue
        row = cx.execute(
            "SELECT id, episode_id, idx, start_ms, end_ms, text FROM subtitle_lines WHERE id=?",
            (lid,),
        ).fetchone()
        if row is None:
            log.debug("evidence line %s is gone — skipped", lid)
            continue
        if episode_id is not None and int(row["episode_id"]) != int(episode_id):
            continue
        near = (allowed_ids is not None and lid in allowed_ids) or lid in same_cue
        if not near and target_idx is not None and row["idx"] is not None:
            near = abs(int(row["idx"]) - int(target_idx)) <= MAX_EVIDENCE_IDX_DISTANCE
        if not near:
            log.debug("evidence line %s is not a ±%d neighbour of %s — skipped",
                      lid, MAX_EVIDENCE_IDX_DISTANCE, target_id)
            continue
        text = strip_bidi(row["text"] or "")
        if not text.strip():
            continue
        seen.add(lid)
        entries.append({
            "line_id": lid,
            "idx": row["idx"],
            "start_ms": int(row["start_ms"] or 0),
            "end_ms": int(row["end_ms"] or 0),
            "text": text,
            "role": "evidence",
        })
        if len(entries) >= MAX_EVIDENCE_LINES:
            break

    for cont in continuation(line, next_line):
        lid = cont.get("line_id")
        if lid is not None and int(lid) in seen:
            continue                      # already carried as evidence (shown on the front)
        entries.append(cont)

    return truncate_extend(line, entries, cap_ms)


@dataclass
class CardSpec:
    """Everything `service.create_card` needs to write one `srs_cards` row.

    Field names match the columns of §2.1 (`tags`/`extend`/`alt_moment_ids` are
    the Python-side lists behind `tags_json`/`extend_json`/`alt_moment_ids_json`).
    Produced by `snapshot_line()` + the judge (auto), the importer (curated) or
    the manual-create route.
    """
    lemma: str
    reading: Optional[str] = None
    pos: Optional[str] = None
    gloss: Optional[str] = None
    meaning_short: Optional[str] = None
    meaning_full: Optional[str] = None
    why_clear: Optional[str] = None
    usage_note: Optional[str] = None
    tags: list[str] = field(default_factory=list)
    freq_rank: Optional[int] = None
    source: str = "auto"                     # auto | manual | curated-initial | confirm
    score: Optional[float] = None
    clarity: Optional[float] = None
    usefulness: Optional[float] = None
    priority: Optional[int] = None
    # --- primary moment snapshot (self-contained; line_id is a soft reference)
    line_id: Optional[int] = None
    episode_id: Optional[int] = None
    anilist_id: Optional[int] = None
    show_title: Optional[str] = None
    ep_number: Optional[int] = None
    start_ms: int = 0
    end_ms: int = 0
    text: str = ""
    norm_text: str = ""
    text_furigana: Optional[str] = None
    translation: Optional[str] = None
    translation_source: Optional[str] = None  # human | mt | user | None
    target_surface: str = ""
    tokens_json: Optional[str] = None         # §2.4 {"src":…, "tokens":[…]}
    context_json: Optional[str] = None        # §2.4 ±2 dialogue lines
    # → extend_json: every EXTRA line the moment includes, chronologically —
    # {line_id, idx, start_ms, end_ms, text, role: 'evidence'|'continuation'}.
    # `evidence` lines are the neighbours the meaning depends on (shown on the
    # card front, Japanese only) and, like the continuation cue, are covered by
    # the clip window (§6.2). Rows written before the evidence work carry
    # entries without `role`/`idx`; readers must treat a missing role as
    # 'continuation'.
    extend: list[dict] = field(default_factory=list)
    alt_moment_ids: list[int] = field(default_factory=list)  # → alt_moment_ids_json


# ---------------------------------------------------------------------------
# Translation hygiene (amendments §B2) — the stored English cue is the one that
# OVERLAPS the Japanese cue and is frequently a merge of two spoken lines.
# ---------------------------------------------------------------------------

_SENTENCE_END_RE = re.compile(r"[.!?…](\s|$)")
MERGED_LENGTH_RATIO = 2.2

_CLEAN_SYSTEM = (
    "You clean machine-aligned English subtitles. The English cue given to you often merges "
    "the translation of the target Japanese line with a neighbouring line. Return ONLY the "
    "English that translates the target Japanese line, verbatim from the given English where "
    "possible (do not retranslate, do not add or embellish). If the English contains nothing "
    "that translates the target line, return the English unchanged. "
    'Return JSON {"t": "<english>"}.'
)
_CLEAN_SCHEMA = {
    "type": "object",
    "properties": {"t": {"type": "string"}},
    "required": ["t"],
    "additionalProperties": False,
}


def looks_merged(ja: str, en: str, neighbour_translations: tuple[str, ...] = ()) -> bool:
    """Heuristic gate for the cleaning call (§B2): two or more sentence
    terminators, a length far above the Japanese, or a cue shared verbatim with
    a neighbouring line."""
    ja = (ja or "").strip()
    en = (en or "").strip()
    if not ja or not en:
        return False
    if len(_SENTENCE_END_RE.findall(en)) >= 2:
        return True
    if len(en) > MERGED_LENGTH_RATIO * max(1, len(ja)):
        return True
    return any(en == (n or "").strip() for n in neighbour_translations if n)


def clean_translation(
    ja: str,
    raw_en: Optional[str],
    *,
    prev_ja: Optional[str] = None,
    next_ja: Optional[str] = None,
    neighbour_translations: tuple[str, ...] = (),
) -> Optional[str]:
    """`raw_en` trimmed to the English that translates THIS line only (§B2).

    One `claude_json` call on the fast model, gated by `looks_merged`; any
    failure (no key, network, bad JSON) falls back to the raw cue unchanged.
    """
    en = (raw_en or "").strip()
    if not en:
        return None
    if not looks_merged(ja, en, neighbour_translations):
        return en
    try:
        from ..config import settings
        from ..llm import claude_json

        res = claude_json(
            _CLEAN_SYSTEM,
            "Previous Japanese line: " + (prev_ja or "—") + "\n"
            "TARGET Japanese line: " + ja + "\n"
            "Next Japanese line: " + (next_ja or "—") + "\n\n"
            "English cue overlapping the target line:\n" + en,
            model=settings.anthropic_model,
            schema=_CLEAN_SCHEMA,
            max_tokens=400,
            timeout=20.0,
        )
        out = (res or {}).get("t") if isinstance(res, dict) else None
        out = (str(out).strip() or None) if out else None
        return out or en
    except Exception as e:                      # never fail a card over a cue
        log.debug("translation cleaning unavailable: %s", e)
        return en


# ---------------------------------------------------------------------------
# Tokens (§2.4)
# ---------------------------------------------------------------------------

def build_tokens(
    text: str,
    lemma: str,
    target_surface: Optional[str],
    *,
    reading: Optional[str] = None,
) -> tuple[dict, Optional[str]]:
    """`({"src": "migaku"|"local", "tokens": [...]}, target_surface)` (§2.4).

    Exactly one token carries `t: true` when the tokenizer isolates the word:
    the token whose dictForm/lemma equals `lemma`, else surface ==
    `target_surface`, else (a kana variant folded into a kanji lemma) the token
    whose reading equals the lemma's reading. When nothing matches every token
    is `t: false` and the UI falls back to substring highlighting.
    """
    from ..learn import migaku_tok
    from ..learn.service import _known_lookup, _to_hira, _token_status, tokenize

    src = "migaku"
    rows = migaku_tok.tokenize_lines([text])
    toks = (rows or [None])[0] if rows else None
    if not toks:
        src = "local"
        toks = [
            {"surface": t["surface"], "dictForm": t["lemma"], "reading": t["reading"]}
            for t in tokenize(text)
        ]

    known, ignored = _known_lookup()
    want_reading = _to_hira(reading) if reading else None
    out: list[dict] = []
    for t in toks or []:
        surface = t.get("surface") or ""
        dict_form = t.get("dictForm") or t.get("lemma") or surface
        tok_reading = _to_hira(t.get("reading"))
        out.append({
            "s": surface,
            "r": tok_reading or None,
            "t": False,
            "k": _token_status(dict_form, surface, known, ignored, reading=tok_reading),
            "_lemma": dict_form,
        })

    hit = None
    for i, t in enumerate(out):
        if t["_lemma"] == lemma:
            hit = i
            break
    if hit is None and target_surface:
        for i, t in enumerate(out):
            if t["s"] == target_surface:
                hit = i
                break
    if hit is None and want_reading:
        for i, t in enumerate(out):
            if t["r"] == want_reading:
                hit = i
                break
    if hit is not None:
        out[hit]["t"] = True
        if not target_surface:
            target_surface = out[hit]["s"]

    for t in out:
        t.pop("_lemma", None)
        if t["r"] and t["r"] == _to_hira(t["s"]):
            t["r"] = None
    return {"src": src, "tokens": out}, target_surface


def snapshot_line(
    line_id: int,
    lemma: str,
    target_surface: Optional[str] = None,
    *,
    reading: Optional[str] = None,
    clean: bool = True,
    evidence_line_ids: Optional[list] = None,
) -> dict:
    """Build the moment snapshot for one line (§5.7): text/furigana/translation,
    tokens, ±2 dialogue context lines and the `extend` lines the moment includes
    (the `evidence_line_ids` the meaning depends on + the continuation cue).

    `clean=False` skips the §B2 merged-cue trimming call — the caller already has
    a better translation (the importer's curated field, the judge's
    `translation_clean`) and would only pay for a result it throws away.

    Raises `LookupError` when the line is gone and `ValueError("invalid_surface")`
    when `target_surface` is not a substring of the (bidi-stripped) text.
    """
    from ..db import connect
    from ..learn.service import furigana

    with connect() as cx:
        row = cx.execute(
            "SELECT sl.id, sl.episode_id, sl.idx, sl.start_ms, sl.end_ms, sl.text, "
            "       sl.text_furigana, sl.translation, "
            "       e.anilist_id, e.ep_number, t.romaji, t.english "
            "FROM subtitle_lines sl "
            "JOIN episodes e ON e.id = sl.episode_id "
            "LEFT JOIN titles t ON t.anilist_id = e.anilist_id "
            "WHERE sl.id=?",
            (line_id,),
        ).fetchone()
        if not row:
            raise LookupError(f"subtitle line {line_id} not found")
        line = _row_to_line(row)
        line["episode_id"] = row["episode_id"]
        before, after = dialogue_neighbours(cx, line, 2)
        extend, extend_truncated = build_extend(
            cx, line, evidence_line_ids,
            next_line=after[0] if after else None,
            allowed_ids={n["line_id"] for n in (*before, *after) if n.get("line_id")},
        )

    text = line["text"]
    if not text:
        raise ValueError("invalid_surface")

    surface = strip_bidi(target_surface) if target_surface else None
    tokens_doc, surface = build_tokens(text, lemma, surface, reading=reading)
    if not surface:
        surface = lemma if lemma in text else None
    if not surface or surface not in text:
        raise ValueError("invalid_surface")

    text_furigana = strip_bidi(row["text_furigana"] or "") or None
    if not text_furigana:
        try:
            text_furigana = furigana(text)
        except Exception as e:                  # tokenizer trouble is not fatal
            log.debug("furigana failed for line %s: %s", line_id, e)
            text_furigana = None

    raw_translation = (row["translation"] or "").strip() or None
    translation_source: Optional[str] = "human" if raw_translation else None
    if not raw_translation:
        try:
            from ..learn.service import translate_line

            got = translate_line(line_id).get("translation")
            if got:
                raw_translation = str(got).strip()
                translation_source = "mt"
        except Exception as e:
            log.debug("MT translation failed for line %s: %s", line_id, e)
    translation = raw_translation
    if clean and raw_translation and translation_source == "human":
        translation = clean_translation(
            text,
            raw_translation,
            prev_ja=before[-1]["text"] if before else None,
            next_ja=after[0]["text"] if after else None,
            neighbour_translations=tuple(
                n.get("translation") or "" for n in (*before, *after)
            ),
        )

    context = [
        {**n, "is_target": False} for n in before
    ] + [{
        "line_id": line["line_id"], "idx": line["idx"], "start_ms": line["start_ms"],
        "end_ms": line["end_ms"], "text": text, "text_furigana": text_furigana,
        "translation": translation, "is_target": True,
    }] + [
        {**n, "is_target": False} for n in after
    ]

    return {
        "line_id": line_id,
        "episode_id": row["episode_id"],
        "anilist_id": row["anilist_id"],
        "show_title": row["romaji"] or row["english"],
        "ep_number": row["ep_number"],
        "idx": line["idx"],
        "start_ms": line["start_ms"],
        "end_ms": line["end_ms"],
        "text": text,
        "text_raw": row["text"] or "",
        "norm_text": norm_text(text),
        "text_furigana": text_furigana,
        "translation": translation,
        "translation_raw": raw_translation,
        "translation_source": translation_source,
        "target_surface": surface,
        "tokens_json": json.dumps(tokens_doc, ensure_ascii=False),
        "context_json": json.dumps(context, ensure_ascii=False),
        "extend": extend,
        "extend_truncated": extend_truncated,
        "evidence_line_ids": [x["line_id"] for x in extend
                              if x.get("role") == "evidence" and x.get("line_id")],
    }


__all__ = [
    "BIDI_RE", "CardSpec", "build_extend", "build_tokens", "clean_translation",
    "continuation", "dialogue_neighbours", "looks_merged", "norm_text", "snapshot_line",
    "sort_extend", "strip_bidi", "truncate_extend",
]
