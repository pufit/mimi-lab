"""The moment judge — one `claude_json` call per 4 words × ≤5 moments (§5.5).

The pipeline is a judgment gate (amendments §A): mechanical filters only widen
and order the pool; this module decides. The system prompt carries the bars of
the agent curation that produced the initial deck verbatim (amendments §C).

`JUDGE_SCHEMA` contains no numeric/length keywords — Anthropic structured
outputs reject `minimum`/`maximum`/… with HTTP 400 and `llm.claude_json` turns
any HTTP error into `None`, i.e. "judge unavailable" for every batch. Ranges are
clamped here after parsing.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any, Optional

from ..models import SrsSettings
from .constants import JUDGE_MAX_TOKENS, JUDGE_TIMEOUT_S, MAX_EVIDENCE_LINES

log = logging.getLogger("mimi_lab.srs.judge")

# The fixed system prompt carrying the initial-deck curation bars verbatim
# (§5.5 "Who this deck is for" … "Output", amendments §C).
JUDGE_SYSTEM: str = """You are an expert Japanese teacher and sentence-mining curator building a personal SRS deck.

### Who this deck is for
An intermediate Japanese learner (Migaku tracks ~3,100 KNOWN words; roughly JLPT N3→N2) who
watches anime with Japanese subtitles. Every card shows a 3-second clip of ONE
subtitle line with audio, the Japanese line with the target word highlighted, and on the back the
English translation, the meaning of the word IN THAT SCENE and why it is clear.
The learner's instruction: "find words that are meaningful and not just N+1, but its meaning is super clear
from the context. Don't just stick to mechanical rules (e.g. 1 unknown word)."

### Input
For each WORD: lemma, hiragana reading from the tokenizer (verify it), JMdict gloss (may be missing),
JPDB rank, occurrences/episodes in the learner's library, optional hints (the tokenizer splits the form; the
reading matches a word the learner knows; the English capitalises it like a name; for a single kanji, the
compound it appears in), and up to 5 MOMENTS, mechanically pre-ranked (a HINT, not a decision).
Each moment: show, episode, the target line marked >>, two dialogue lines before and after with
their English, the English translation of the target line — labelled when it is SHARED with a
neighbouring cue (the English track is time-aligned; it may describe the neighbour) or when it is a
MACHINE translation of this very line (not independent evidence: rely on the Japanese context) —
target_surface, other_unknowns (other words Migaku marks not-known — a hint only), repeats (a text
seen twice in the corpus may be a recap or a song).

### Word-level bar — is this real, useful vocabulary for this learner?
ACCEPT nouns, verbs, adjectives, adverbs, and genuinely useful set expressions. REJECT with a
reason_category: particles/auxiliaries/conjunction combos (function_word); fragments of a compound
that only occur inside a longer word (教 from 教団, 感 from 違和感 — unless the moments show it used
standalone) (fragment); pieces of a fixed expression (しれる from かもしれない) (fixed_expression);
proper nouns, character names, titles, place names (name); onomatopoeia and interjections unless
they are common anime speech they will keep meeting (interjection); tokenizer mistakes — target_surface
does not correspond to the lemma, or the reading is wrong for that word (tokenizer_error); words they
surely know already (too_basic); one-character oddities, archaic/dialect/vulgar items tied to one
character's quirk (odd_register). A missing JMdict gloss does NOT by itself disqualify a real word
(俺様).

### Moment-level bar — what is already decided, and what is yours
Every moment listed has ALREADY passed a translation-blind cloze test: with the target word masked
and NO English shown, a judge inferred its meaning from the Japanese lines alone and the guess
matched the actual sense. Clarity is pre-assigned from that test — do not re-judge whether the
meaning is inferable, and NEVER treat the English translation as evidence of clarity: the learner
infers before the back of the card is revealed; the translation is a check, not a cue. A repeat of
the target word in a neighbouring line is not a cue either (it is the same gap).
Your moment-level job: the line is one complete natural utterance (not cut mid-sentence, not a
rolled/duplicated cue, not song lyrics, not two speakers merged; roughly 1.5–6 seconds); the
translation exists and actually renders the word (translation_renders_word=false when the English is
a loose localization that drops it); translation_clean; and the ORDER — best_moment/alt_moments by
how well the moment TEACHES the word (the word carries the sentence's main information, natural
register, typical usage). Still return your own clarity estimate per moment (it is recorded; the
blind score decides).

### Do NOT be mechanical
other_unknowns is only a hint. A line with 1–2 other unknown words is a fine card when the blind
test passed; unknown words can be names, grammar pieces or tokenizer noise. Reject a word
(word_ok=false, reason_category "no_clear_moment") only when none of the listed moments is a usable
utterance; word-level rejections (fragment, name, too_basic, …) apply as before. It is better to
reject than to accept a mediocre moment.

### Output
One entry per WORD (same order, `word` = its index). For accepted words: verified reading; pos
(noun|verb|adjective|adverb|expression|other); meaning_short (the sense USED in the best moment, ≤6
English words, learner-friendly); meaning_full (fuller dictionary-style meaning, one line); why_clear
(one sentence naming the exact cues in the best moment); usage_note (nuance/register/collocation/
kana-vs-kanji, or ""); usefulness 0–1; priority 1–5 (5 = learn first: common in the learner's shows + good
rank + leverage); tags; best_moment (index) and alt_moments (other acceptable indexes, best first).
For EVERY moment: clarity 0–1 (how unmistakable the meaning is in THAT moment; ≥ 0.75 is card
quality), translation_renders_word, clean_utterance, note (≤ 15 words), evidence_line_ids, and
translation_clean — the English that translates ONLY this Japanese line.
evidence_line_ids: the ids (shown in brackets after each context line) of the context lines the
meaning DEPENDS on — the ones the learner must also hear, because your why_clear leans on them or
because the target sentence alone would be ambiguous without them. Return [] when the target
sentence carries the meaning by itself; fewer is better (they lengthen the clip). Never include
the target line's own id, and only lines shown for THAT moment. The subtitle track is time-aligned, so the
stored English is often merged with a neighbouring cue ("…sheets... Sh-She's very demanding.");
return just the part that renders this line, lightly cleaned, or "" when the given English already
translates exactly this line and nothing else."""

# Anthropic structured outputs REJECT numeric/length keywords: a schema with
# `minimum`/`maximum` returns HTTP 400 and `llm.claude_json` turns that into
# `None`, i.e. "judge unavailable" for every batch. Ranges are clamped in
# `judge_packets` after parsing instead (§5.5).
JUDGE_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["words"],
    "properties": {
        "words": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "word", "word_ok", "reason_category", "reading", "pos", "meaning_short",
                    "meaning_full", "why_clear", "usage_note", "usefulness", "priority", "tags",
                    "best_moment", "alt_moments", "moments",
                ],
                "properties": {
                    "word": {"type": "integer"},
                    "word_ok": {"type": "boolean"},
                    "reason_category": {
                        "type": "string",
                        "enum": [
                            "", "fragment", "fixed_expression", "function_word", "name",
                            "interjection", "tokenizer_error", "too_basic", "no_clear_moment",
                            "odd_register", "other",
                        ],
                    },
                    "reading": {"type": "string"},
                    "pos": {
                        "type": "string",
                        "enum": ["noun", "verb", "adjective", "adverb", "expression", "other"],
                    },
                    "meaning_short": {"type": "string"},
                    "meaning_full": {"type": "string"},
                    "why_clear": {"type": "string"},
                    "usage_note": {"type": "string"},
                    "usefulness": {"type": "number"},
                    "priority": {"type": "integer"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "best_moment": {"type": "integer"},
                    "alt_moments": {"type": "array", "items": {"type": "integer"}},
                    "moments": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": [
                                "moment", "clarity", "translation_renders_word",
                                "clean_utterance", "note", "translation_clean",
                                "evidence_line_ids",
                            ],
                            "properties": {
                                "moment": {"type": "integer"},
                                "clarity": {"type": "number"},
                                "translation_renders_word": {"type": "boolean"},
                                "clean_utterance": {"type": "boolean"},
                                "note": {"type": "string"},
                                "translation_clean": {"type": "string"},
                                # context line ids the meaning depends on ([] when
                                # the sentence suffices) — no minItems/maxItems:
                                # numeric keywords make the call 400 (§5.5)
                                "evidence_line_ids": {
                                    "type": "array", "items": {"type": "integer"},
                                },
                            },
                        },
                    },
                },
            },
        },
    },
}


@dataclass
class WordPacket:
    """One word and its ≤5 candidate moments as handed to the judge (§5.5)."""
    lemma: str
    reading: Optional[str] = None
    gloss: Optional[str] = None
    freq_rank: Optional[int] = None
    occ: int = 0
    eps: int = 0
    hints: list[str] = field(default_factory=list)
    moments: list[dict] = field(default_factory=list)   # census.MomentCandidate dicts


@dataclass
class JudgeResult:
    """Parsed + clamped judge answer for one call."""
    words: list[dict] = field(default_factory=list)
    model: str = ""
    raw: Optional[dict] = None
    in_tokens: int = 0
    out_tokens: int = 0


def _ms(ms: Optional[int]) -> str:
    t = int(ms or 0) // 1000
    return f"{t // 60:02d}:{t % 60:02d}"


def _label_translation(m: dict) -> str:
    """The English of the target line with its provenance label (§5.5)."""
    tr = m.get("translation")
    if not tr:
        return "EN: (none — judge from the Japanese context alone)"
    if (m.get("translation_source") or "") == "mt":
        return f"EN (machine translation of this line — not independent evidence): {tr}"
    if m.get("translation_shared"):
        return f"EN (shared with a neighbouring cue — may describe that line): {tr}"
    return f"EN: {tr}"


def build_packets(words: list[WordPacket]) -> str:
    """Render the user message: one block per word with its moments, ±2 context
    lines and translations, `other_unknowns` and the census hints (§5.5)."""
    out: list[str] = []
    for wi, w in enumerate(words, start=1):
        gloss = f" — JMdict: {w.gloss}" if w.gloss else " — JMdict: (no gloss)"
        rank = f" — JPDB rank {w.freq_rank}" if w.freq_rank else " — JPDB rank: unranked"
        reading = f" ({w.reading})" if w.reading else ""
        out.append(f"WORD {wi}: {w.lemma}{reading}{gloss}{rank} — "
                   f"{w.occ}× in {w.eps} episode{'s' if w.eps != 1 else ''}")
        out.append(f"  hints: {'; '.join(w.hints) if w.hints else 'none'}")
        for mi, m in enumerate(w.moments, start=1):
            watched = " (watched)" if m.get("watched") else ""
            others = m.get("other_unknowns_list") or []
            others_s = ", ".join(others) if others else ""
            head = (f"  MOMENT {mi} [m {m.get('moment_id')}] {m.get('show') or '?'} "
                    f"E{m.get('ep_number') or '?'} {_ms(m.get('start_ms'))}{watched} — "
                    f"target_surface: {m.get('target_surface') or ''} — "
                    f"other_unknowns: [{others_s}] — repeats: {m.get('repeats', 1)}")
            out.append(head)
            for i, c in enumerate(m.get("context_before") or [], start=1):
                n = len(m.get("context_before") or [])
                out.append(f"    -{n - i + 1} [line {c.get('line_id')}]: {c.get('text', '')}"
                           f"{' | ' + c['translation'] if c.get('translation') else ''}")
            out.append(f"    >>  {m.get('text', '')}")
            out.append(f"        {_label_translation(m)}")
            for i, c in enumerate(m.get("context_after") or [], start=1):
                out.append(f"    +{i} [line {c.get('line_id')}]: {c.get('text', '')}"
                           f"{' | ' + c['translation'] if c.get('translation') else ''}")
        out.append("")
    out.append(f"Judge all {len(words)} words. Return one entry per WORD in the same order, "
               f"`word` = the WORD number, `moment` / `best_moment` / `alt_moments` = the "
               f"MOMENT numbers shown above, `evidence_line_ids` = the [line <id>] numbers of "
               f"the context lines that moment's meaning depends on (empty when the target "
               f"sentence alone is enough).")
    return "\n".join(out)


def _call(system: str, user: str, cfg: SrsSettings) -> Optional[dict]:
    """The single Anthropic call (selftests monkeypatch this or
    `app.llm.claude_json`)."""
    from .. import llm as _llm
    from ..config import settings as _settings

    return _llm.claude_json(
        system, user,
        model=getattr(_settings, "translation_model", None),
        schema=JUDGE_SCHEMA,
        max_tokens=JUDGE_MAX_TOKENS,
        timeout=JUDGE_TIMEOUT_S,
    )


def _usage_snapshot(model: str) -> tuple[int, int]:
    try:
        from ..db import kv_get
        return (int(kv_get(f"llm.usage.{model}.in") or 0),
                int(kv_get(f"llm.usage.{model}.out") or 0))
    except Exception:
        return (0, 0)


def _num(v: Any) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def _clamp_moment(raw: Any) -> dict:
    """Clamp one moment verdict; out-of-range / non-finite values reject the
    moment with `note='out_of_range'` rather than trusting it (§5.5)."""
    if not isinstance(raw, dict):
        return {"moment": 0, "clarity": 0.0, "translation_renders_word": False,
                "clean_utterance": False, "note": "out_of_range", "translation_clean": "",
                "evidence_line_ids": [], "out_of_range": True}
    clarity = _num(raw.get("clarity"))
    bad = clarity is None or not (0.0 <= clarity <= 1.0)
    try:
        moment = int(raw.get("moment"))
    except (TypeError, ValueError):
        moment = 0
        bad = True
    evidence: list[int] = []
    for v in (raw.get("evidence_line_ids") or []) if isinstance(
            raw.get("evidence_line_ids"), list) else []:
        try:
            lid = int(v)
        except (TypeError, ValueError):
            continue
        if lid > 0 and lid not in evidence:
            evidence.append(lid)
    return {
        "moment": moment,
        "clarity": 0.0 if bad else round(float(clarity), 4),
        # ids are validated against the moment's own ±2 neighbours when the
        # snapshot is built (`snapshot.build_extend`), so a hallucinated id is
        # dropped there rather than lengthening a clip.
        "evidence_line_ids": evidence[:MAX_EVIDENCE_LINES],
        "translation_renders_word": bool(raw.get("translation_renders_word")),
        "clean_utterance": bool(raw.get("clean_utterance")) and not bad,
        "note": "out_of_range" if bad else str(raw.get("note") or "")[:300],
        "translation_clean": str(raw.get("translation_clean") or "").strip()[:600],
        "out_of_range": bad,
    }


def _clamp_word(raw: Any) -> Optional[dict]:
    if not isinstance(raw, dict):
        return None
    try:
        idx = int(raw.get("word"))
    except (TypeError, ValueError):
        return None
    usefulness = _num(raw.get("usefulness"))
    usefulness = None if usefulness is None else max(0.0, min(1.0, usefulness))
    prio = _num(raw.get("priority"))
    priority = None if prio is None else int(max(1, min(5, round(prio))))
    tags = raw.get("tags")
    tags = [str(t)[:40] for t in tags][:12] if isinstance(tags, list) else []
    alts = raw.get("alt_moments")
    alt_moments: list[int] = []
    if isinstance(alts, list):
        for a in alts:
            try:
                alt_moments.append(int(a))
            except (TypeError, ValueError):
                continue
    try:
        best = int(raw.get("best_moment"))
    except (TypeError, ValueError):
        best = 0
    moments = ([_clamp_moment(m) for m in raw["moments"]]
               if isinstance(raw.get("moments"), list) else [])
    return {
        "word": idx,
        "word_ok": bool(raw.get("word_ok")),
        "reason_category": str(raw.get("reason_category") or "")[:40],
        "reading": str(raw.get("reading") or "")[:60],
        "pos": str(raw.get("pos") or "")[:20],
        "meaning_short": str(raw.get("meaning_short") or "")[:120],
        "meaning_full": str(raw.get("meaning_full") or "")[:400],
        "why_clear": str(raw.get("why_clear") or "")[:400],
        "usage_note": str(raw.get("usage_note") or "")[:400],
        "usefulness": usefulness,
        "priority": priority,
        "tags": tags,
        "best_moment": best,
        "alt_moments": alt_moments,
        "moments": moments,
    }


def judge_packets(words: list[WordPacket], cfg: SrsSettings) -> Optional[JudgeResult]:
    """One `claude_json` call on `settings.translation_model`
    (`max_tokens=JUDGE_MAX_TOKENS`, `timeout=JUDGE_TIMEOUT_S`,
    `schema=JUDGE_SCHEMA`). Returns `None` on any failure (the caller retries
    once, then raises). Clamps `clarity`/`usefulness` to [0,1] and `priority` to
    1..5 after parsing; an out-of-range or non-finite value marks that moment
    rejected with `note='out_of_range'`."""
    from ..config import settings as _settings

    if not words:
        return JudgeResult(words=[], model="")
    if not JUDGE_SYSTEM.strip():
        raise RuntimeError("JUDGE_SYSTEM is empty — refusing to call the judge")

    model = getattr(_settings, "translation_model", "") or ""
    in0, out0 = _usage_snapshot(model)
    user = build_packets(words)
    try:
        raw = _call(JUDGE_SYSTEM, user, cfg)
    except Exception as e:                       # claude_json never raises, but a
        log.warning("judge call raised: %s", e)  # monkeypatched _call might
        return None
    if not isinstance(raw, dict):
        return None
    items = raw.get("words")
    if not isinstance(items, list):
        return None
    parsed = [w for w in (_clamp_word(i) for i in items) if w is not None]
    if not parsed:
        return None
    in1, out1 = _usage_snapshot(model)
    return JudgeResult(
        words=parsed, model=model, raw=raw,
        in_tokens=max(0, in1 - in0), out_tokens=max(0, out1 - out0),
    )


def _schema_is_clean(schema: Any) -> bool:
    """Selftest helper: no numeric/length keywords anywhere and every object
    carries `additionalProperties: false` (§5.5)."""
    banned = {
        "minimum", "maximum", "multipleOf", "minLength", "maxLength",
        "minItems", "maxItems", "pattern", "format",
    }
    if isinstance(schema, dict):
        if banned & set(schema):
            return False
        if schema.get("type") == "object" and schema.get("additionalProperties") is not False:
            return False
        return all(_schema_is_clean(v) for v in schema.values())
    if isinstance(schema, list):
        return all(_schema_is_clean(v) for v in schema)
    return True
