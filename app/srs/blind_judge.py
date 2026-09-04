"""Translation-blind clarity gate — the cloze test every card moment must pass.

The user (2026-09-03), on the 攻撃 card (stack #2): "the context is unclear to me".
The first judge admitted the English translation as a clarity source and counted
a repeat of the unknown word in a neighbouring line as evidence — half of the
initial deck's `why_clear` texts leaned on the translation. His rule:

    Knowing every other word and watching the clip, you can infer the target's
    meaning BEFORE the translation is revealed; the translation is a check, not
    a cue. "Knowing every other word" is not a mechanical gate — unknown words
    can be grammar, names or bad tokenization; the judge decides.

This module is that test:
  1. every occurrence of the target lemma in the target line ± `BLIND_CONTEXT_LINES`
     dialogue lines is masked (［？］; trailing okurigana kept: 救って → ［？］って);
  2. `BLIND_JUDGE_MODEL` (Opus 5) lists what the masked word could mean from the
     Japanese alone — no translation, no frames — and whether the context singles
     out one meaning (single / narrow / broad / none) with the exact cues;
  3. the translation model grades the blind best guess against the card's actual
     meaning (exact / close / wrong).
A moment PASSES when the inference is single/narrow, `confidence ≥
BLIND_MIN_CONFIDENCE`, and the guess matches. Migaku's not-known tokens are
passed as a hint only.

`_blind_call` / `_match_call` are the two network seams (selftests replace them).
"""
from __future__ import annotations

import logging
import math
import re
import time
from typing import Any, Optional

from .constants import (
    BLIND_CONTEXT_AFTER,
    BLIND_CONTEXT_AFTER_MAX,
    BLIND_CONTEXT_BEFORE,
    BLIND_CONTEXT_BEFORE_MAX,
    BLIND_JUDGE_MODEL,
    BLIND_MATCH_MAX_TOKENS,
    BLIND_MATCH_TIMEOUT_S,
    BLIND_MAX_TOKENS,
    BLIND_MIN_CONFIDENCE,
    BLIND_TIMEOUT_S,
    MAX_EVIDENCE_LINES,
)
from .snapshot import dialogue_neighbours, strip_bidi

log = logging.getLogger("mimi_lab.srs.blind_judge")

MASK = "［？］"
_KANJI_RE = re.compile(r"[々㐀-䶿一-鿿豈-﫿]")

BLIND_SYSTEM = """You run a cloze test for a Japanese learner's video flashcard.

The learner is intermediate (about JLPT N3→N2, ~3,100 known words) and watches anime with Japanese subtitles. On the card front they see the subtitle lines below in order and hear the audio — but NO English translation and no dictionary. One word is masked as ［？］ everywhere it occurs (every form of the same word, in every line shown; trailing okurigana may remain, e.g. ［？］って for a te-form verb, ［？］する for a noun taking する).

Question: could this learner, knowing the other words, work out what ［？］ means from these lines alone — and land on the RIGHT meaning?

Input: the show, the target line marked ▶ and the surrounding dialogue in cue order — several lines before and after (Japanese only, each with an id; lines that shared one on-screen cue with the target appear as their own lines right next to it), the coarse part of speech of the masked word, and a hint list of words in these lines that the learner's vocabulary tracker marks as not yet known. The hint list is only a hint: names, grammar pieces, tokenizer fragments and words that are obvious in context do not block anything — decide yourself whether an unknown word actually prevents the inference. The viewer has watched the scene up to this point, so the earlier lines are legitimately in their mind.

Do:
1. candidates: what ［？］ could plausibly mean here (English, most plausible first, 1–5 entries) — think like the learner: which meanings fit the words and the situation these lines describe?
2. inferability: "single" — the lines force one meaning; "narrow" — one meaning up to near-synonyms (attack/assault/strike); "broad" — several DISTINCT meanings fit (attack / kill / stop / drive away); "none" — no real cue.
3. confidence 0–1: how sure you are the learner would land on your best_guess (or a near-synonym) from these lines.
4. cues: quote the exact Japanese that pins the meaning down and say in one sentence how. evidence_line_ids: ids of lines OTHER than the target that those cues come from (fewest possible; [] when the target line alone suffices).
5. blockers: other words in the shown lines whose unknown-ness would stop the inference, or "".
6. need_more_context: true ONLY when the shown lines are too few to decide and earlier/later dialogue would plausibly settle it (a scene whose topic is set up before the window) — you will then be shown a wider window once. Otherwise false: judge with what is shown.

Rules:
- Reason only from the visible Japanese. Never identify the masked word from memory of this show, from character counts, or from what "usually" appears in such lines.
- The learner has no translation. Do not assume one.
- Another ［？］ in a neighbouring line is NOT a cue by itself — it is the same gap. Only its surroundings can help.
- Genre priors alone (a battle anime, so probably "attack") make a meaning plausible, not inferable: without a concrete cue in the lines that excludes the alternatives, that is "broad".
- Be strict: a card whose meaning is only guessable teaches nothing. When torn between "narrow" and "broad", choose "broad".
Return JSON exactly matching the schema."""

BLIND_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["candidates", "best_guess", "inferability", "confidence", "cues",
                 "evidence_line_ids", "blockers", "need_more_context", "note"],
    "properties": {
        "candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["meaning", "plausibility"],
                "properties": {
                    "meaning": {"type": "string"},
                    "plausibility": {"type": "string", "enum": ["high", "medium", "low"]},
                },
            },
        },
        "best_guess": {"type": "string"},
        "inferability": {"type": "string", "enum": ["single", "narrow", "broad", "none"]},
        "confidence": {"type": "number"},
        "cues": {"type": "string"},
        "evidence_line_ids": {"type": "array", "items": {"type": "integer"}},
        "blockers": {"type": "string"},
        "need_more_context": {"type": "boolean"},
        "note": {"type": "string"},
    },
}

MATCH_SYSTEM = """You grade a blind guess. A judge saw Japanese subtitle lines with one word masked and guessed its meaning from context alone. You get the actual word, its meaning on the card, the English translation of the line, and the judge's best guess plus candidate list with plausibility.

match = "exact" when the best guess is the actual sense (wording may differ); "close" when it is a near-synonym or the same concept in this context (attack ↔ assault; be scared ↔ be afraid), or when the actual sense is one of the judge's HIGH-plausibility candidates and the judge rated the context single/narrow; otherwise "wrong" — a different meaning, the wrong sense of a polysemous word, or only a vague category ("some verb of motion").
reason: ≤ 20 words. Return JSON {match, reason}."""

MATCH_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["match", "reason"],
    "properties": {
        "match": {"type": "string", "enum": ["exact", "close", "wrong"]},
        "reason": {"type": "string"},
    },
}

_COARSE_POS = {
    "名詞": "noun", "動詞": "verb", "形容詞": "adjective", "形状詞": "adjective",
    "副詞": "adverb", "感動詞": "interjection", "連体詞": "adnominal",
}


# ---------------------------------------------------------------------------
# masking
# ---------------------------------------------------------------------------

def mask_surface(surface: str) -> str:
    """The kanji part of a token becomes ［？］, trailing okurigana stay
    (救って → ［？］って, 攻撃 → ［？］); an all-kana surface is fully masked."""
    if not surface:
        return MASK
    last = None
    for i, ch in enumerate(surface):
        if _KANJI_RE.match(ch):
            last = i
    if last is None:
        return MASK
    return MASK + surface[last + 1:]


def mask_text(text: str, tokens: list[tuple[str, str]], lemma: str,
              target_surface: Optional[str] = None) -> tuple[str, int]:
    """`(masked, n)` — every token of `lemma` masked. `tokens` are `(lemma,
    surface)` in line order (`line_lemmas`). When the surfaces concatenate to
    the text the line is rebuilt token by token (exact); otherwise the matching
    surfaces are replaced longest-first; finally `target_surface`/`lemma`
    literal fallbacks. `n` is the number of masked occurrences."""
    text = strip_bidi(text or "")
    if tokens and "".join(s for _l, s in tokens) == text:
        out: list[str] = []
        n = 0
        for l, s in tokens:
            if l == lemma and s:
                out.append(mask_surface(s))
                n += 1
            else:
                out.append(s)
        return "".join(out), n
    masked, n = text, 0
    hits = {s for l, s in tokens if l == lemma and s}
    for s in sorted(hits, key=len, reverse=True):
        c = masked.count(s)
        if c:
            masked = masked.replace(s, mask_surface(s))
            n += c
    if n == 0 and target_surface and target_surface in masked:
        n = masked.count(target_surface)
        masked = masked.replace(target_surface, mask_surface(target_surface))
    if n == 0 and lemma and lemma in masked:
        n = masked.count(lemma)
        masked = masked.replace(lemma, mask_surface(lemma))
    return masked, n


def line_tokens(cx, line_id: int) -> list[tuple[str, str]]:
    """`(lemma, surface)` pairs of one subtitle line in order. Prefers the
    Migaku tokens when a line carries more than one `token_source`."""
    rows = cx.execute(
        "SELECT lemma, surface, token_source FROM line_lemmas WHERE line_id=? ORDER BY rowid",
        (line_id,),
    ).fetchall()
    if not rows:
        return []
    sources = [r["token_source"] or "local" for r in rows]
    pick = next((s for s in sources if s.startswith("migaku")), sources[0])
    return [(r["lemma"] or "", r["surface"] or "") for r in rows
            if (r["token_source"] or "local") == pick]


def unknown_hints(tokens_by_line: dict[int, list[tuple[str, str]]], lemma: str,
                  limit: int = 12) -> list[str]:
    """Lemmas in the shown lines that Migaku marks not-known, target excluded —
    a hint for the judge, never a gate."""
    try:
        from ..learn import service as ls
        known, ignored = ls._known_lookup()
    except Exception as e:                       # pragma: no cover
        log.debug("known lookup unavailable: %s", e)
        return []
    out: list[str] = []
    for toks in tokens_by_line.values():
        for l, s in toks:
            if not l or l == lemma or l in out:
                continue
            try:
                if ls._token_status(l, s, known, ignored) == "UNKNOWN" and ls._is_content(l):
                    out.append(l)
            except Exception:
                continue
            if len(out) >= limit:
                return out
    return out


def coarse_pos(pos: Optional[str]) -> Optional[str]:
    """UniDic pos1 or the judge's pos → noun|verb|adjective|adverb|… (or None)."""
    if not pos:
        return None
    p = str(pos).strip()
    if p in _COARSE_POS:
        return _COARSE_POS[p]
    low = p.lower()
    if low in ("noun", "verb", "adjective", "adverb", "expression", "other"):
        return low
    return None


# ---------------------------------------------------------------------------
# prompt + calls
# ---------------------------------------------------------------------------

def build_user(*, show: Optional[str], ep_number: Optional[int], pos: Optional[str],
               before: list[dict], target: dict, after: list[dict], hints: list[str]) -> str:
    lines: list[str] = []
    for c in before:
        lines.append(f"  [line {c['line_id']}] {c['masked']}")
    lines.append(f"▶ [line {target['line_id']}] {target['masked']}")
    for c in after:
        lines.append(f"  [line {c['line_id']}] {c['masked']}")
    head = f"Show: {show or 'unknown'}" + (f", episode {ep_number}" if ep_number else "")
    return (
        f"{head}\nMasked word: ［？］ — part of speech: {pos or 'unknown'}\n"
        f"Not-known hints (hint only): {', '.join(hints) if hints else 'none'}\n\n"
        f"Lines (▶ = target):\n" + "\n".join(lines) +
        "\n\nWhat could ［？］ mean here, and does the context single it out?"
    )


def _blind_call(user: str) -> Optional[dict]:
    """The Opus call (selftests replace this)."""
    from .. import llm as _llm

    return _llm.claude_json(
        BLIND_SYSTEM, user, model=BLIND_JUDGE_MODEL, schema=BLIND_SCHEMA,
        max_tokens=BLIND_MAX_TOKENS, timeout=BLIND_TIMEOUT_S,
    )


def _match_call(user: str) -> Optional[dict]:
    """The guess-vs-meaning call on the translation model (selftests replace this)."""
    from .. import llm as _llm
    from ..config import settings as _settings

    return _llm.claude_json(
        MATCH_SYSTEM, user, model=getattr(_settings, "translation_model", None),
        schema=MATCH_SCHEMA, max_tokens=BLIND_MATCH_MAX_TOKENS, timeout=BLIND_MATCH_TIMEOUT_S,
    )


def _num(v: Any) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def _error(kind: str, **extra) -> dict:
    v = {
        "passed": False, "error": kind, "inferability": "none", "confidence": 0.0,
        "best_guess": "", "candidates": [], "cues": "", "evidence_line_ids": [],
        "blockers": "", "need_more_context": False, "note": "", "match": "unchecked",
        "match_reason": "", "masked_text": "", "context": [], "hints": [],
        "model": BLIND_JUDGE_MODEL,
    }
    v.update(extra)
    return v


def _clamp(raw: Any, shown_ids: set[int]) -> dict:
    if not isinstance(raw, dict):
        return _error("no_answer")
    inf = str(raw.get("inferability") or "").strip().lower()
    if inf not in ("single", "narrow", "broad", "none"):
        inf = "none"
    conf = _num(raw.get("confidence"))
    conf = 0.0 if conf is None else max(0.0, min(1.0, conf))
    cands: list[dict] = []
    raw_c = raw.get("candidates")
    for c in (raw_c if isinstance(raw_c, list) else []):
        if isinstance(c, dict) and c.get("meaning"):
            pl = str(c.get("plausibility") or "").lower()
            cands.append({"meaning": str(c["meaning"])[:80],
                          "plausibility": pl if pl in ("high", "medium", "low") else "medium"})
    ev: list[int] = []
    raw_e = raw.get("evidence_line_ids")
    for x in (raw_e if isinstance(raw_e, list) else []):
        try:
            lid = int(x)
        except (TypeError, ValueError):
            continue
        if lid in shown_ids and lid not in ev:
            ev.append(lid)
    return {
        "error": None, "inferability": inf, "confidence": round(conf, 3),
        "best_guess": str(raw.get("best_guess") or "").strip()[:120],
        "candidates": cands[:5], "cues": str(raw.get("cues") or "").strip()[:500],
        "evidence_line_ids": ev[:MAX_EVIDENCE_LINES],
        "blockers": str(raw.get("blockers") or "").strip()[:200],
        "need_more_context": bool(raw.get("need_more_context")),
        "note": str(raw.get("note") or "").strip()[:300],
    }


def _call_twice(fn, user: str) -> Optional[dict]:
    res = fn(user)
    if res is None:
        time.sleep(3)
        res = fn(user)
    return res


# ---------------------------------------------------------------------------
# the gate
# ---------------------------------------------------------------------------

def judge_moment(cx, *, lemma: str, line_id: int, target_surface: Optional[str] = None,
                 pos: Optional[str] = None, reference: Optional[str] = None,
                 translation: Optional[str] = None, show: Optional[str] = None,
                 ep_number: Optional[int] = None,
                 before_n: int = BLIND_CONTEXT_BEFORE, after_n: int = BLIND_CONTEXT_AFTER,
                 allow_expand: bool = True) -> dict:
    """Run the blind test for `lemma` at subtitle line `line_id`.

    `reference` is the card's actual meaning (gloss / meaning_short+full) and
    `translation` the line's English — both are used ONLY for the grading step,
    never shown to the blind judge. The judge sees `before_n`/`after_n` dialogue
    lines around the target; when it flags `need_more_context` and the moment
    did not pass, it is re-run ONCE with the `*_MAX` window (the LLM adjusts its
    own context, the user 2026-09-03) and the wider verdict is returned
    (`context_passes` = 2). Returns a verdict dict with `passed`,
    `inferability`, `confidence`, `best_guess`, `candidates`, `cues`,
    `evidence_line_ids` (validated against the shown neighbours), `blockers`,
    `match`, `match_reason`, `masked_text`, `context`, `hints`, `error`.
    """
    v = _judge_once(cx, lemma=lemma, line_id=line_id, target_surface=target_surface, pos=pos,
                    reference=reference, translation=translation, show=show,
                    ep_number=ep_number, before_n=before_n, after_n=after_n)
    v["context_passes"] = 1
    v["window"] = [before_n, after_n]
    if (allow_expand and not v.get("passed") and not v.get("error")
            and v.get("need_more_context")
            and (before_n < BLIND_CONTEXT_BEFORE_MAX or after_n < BLIND_CONTEXT_AFTER_MAX)):
        w = _judge_once(cx, lemma=lemma, line_id=line_id, target_surface=target_surface, pos=pos,
                        reference=reference, translation=translation, show=show,
                        ep_number=ep_number, before_n=max(before_n, BLIND_CONTEXT_BEFORE_MAX),
                        after_n=max(after_n, BLIND_CONTEXT_AFTER_MAX))
        if not w.get("error"):
            w["context_passes"] = 2
            w["window"] = [max(before_n, BLIND_CONTEXT_BEFORE_MAX), max(after_n, BLIND_CONTEXT_AFTER_MAX)]
            w["first_pass"] = {k: v.get(k) for k in ("inferability", "confidence", "best_guess", "match")}
            return w
    return v


def _judge_once(cx, *, lemma: str, line_id: int, target_surface: Optional[str], pos: Optional[str],
                reference: Optional[str], translation: Optional[str], show: Optional[str],
                ep_number: Optional[int], before_n: int, after_n: int) -> dict:
    row = cx.execute(
        "SELECT sl.id, sl.episode_id, sl.idx, sl.start_ms, sl.end_ms, sl.text, "
        "       e.ep_number, t.romaji, t.english "
        "FROM subtitle_lines sl JOIN episodes e ON e.id = sl.episode_id "
        "LEFT JOIN titles t ON t.anilist_id = e.anilist_id WHERE sl.id=?",
        (line_id,),
    ).fetchone()
    if row is None:
        return _error("line_gone")
    line = {"line_id": row["id"], "episode_id": row["episode_id"], "idx": row["idx"],
            "start_ms": row["start_ms"], "end_ms": row["end_ms"],
            "text": strip_bidi(row["text"] or "")}
    if show is None:
        show = row["romaji"] or row["english"]
    if ep_number is None:
        ep_number = row["ep_number"]
    before, after = dialogue_neighbours(cx, line, max(before_n, after_n, 1))
    before = before[-before_n:] if before_n > 0 else []
    after = after[:after_n] if after_n > 0 else []

    tokens_by_line: dict[int, list[tuple[str, str]]] = {}
    for c in (*before, line, *after):
        tokens_by_line[int(c["line_id"])] = line_tokens(cx, int(c["line_id"]))
    masked_target, n = mask_text(line["text"], tokens_by_line[int(line["line_id"])], lemma,
                                 target_surface)
    if n == 0:
        return _error("target_not_found", masked_text=line["text"])

    def _ctx(c: dict) -> dict:
        m, _k = mask_text(c["text"], tokens_by_line[int(c["line_id"])], lemma)
        return {"line_id": int(c["line_id"]), "masked": m}

    ctx_before = [_ctx(c) for c in before]
    ctx_after = [_ctx(c) for c in after]
    shown_ids = {c["line_id"] for c in (*ctx_before, *ctx_after)}
    hints = unknown_hints(tokens_by_line, lemma)
    user = build_user(show=show, ep_number=ep_number, pos=coarse_pos(pos),
                      before=ctx_before, target={"line_id": int(line["line_id"]),
                                                 "masked": masked_target},
                      after=ctx_after, hints=hints)
    raw = _call_twice(_blind_call, user)
    v = _clamp(raw, shown_ids)
    v.update({"masked_text": masked_target, "context": ctx_before + [
        {"line_id": int(line["line_id"]), "masked": masked_target, "target": True}] + ctx_after,
        "hints": hints, "model": BLIND_JUDGE_MODEL, "match": "unchecked", "match_reason": "",
        "passed": False})
    if v["error"]:
        return v

    if reference or translation:
        cands = "; ".join(f"{c['meaning']} ({c['plausibility']})" for c in v["candidates"]) or "—"
        muser = (
            f"Actual word: {lemma}\nMeaning on the card: {reference or '(no gloss)'}\n"
            f"English translation of the line: {translation or '(none)'}\n\n"
            f"Judge's best guess: {v['best_guess'] or '(none)'}\n"
            f"Judge's candidates: {cands}\nJudge's inferability: {v['inferability']}"
        )
        mraw = _call_twice(_match_call, muser)
        if isinstance(mraw, dict) and str(mraw.get("match") or "").lower() in ("exact", "close", "wrong"):
            v["match"] = str(mraw["match"]).lower()
            v["match_reason"] = str(mraw.get("reason") or "").strip()[:200]
        else:
            v["error"] = "match_unavailable"
            return v
    v["passed"] = passes(v)
    return v


def passes(v: Optional[dict]) -> bool:
    """The pass rule, recomputed from the verdict's fields (so stored verdicts
    survive a threshold change): single/narrow inference, the guess matches the
    actual meaning, confidence at or above the floor."""
    if not v or v.get("error"):
        return False
    try:
        conf = float(v.get("confidence") or 0.0)
    except (TypeError, ValueError):
        return False
    return bool(
        v.get("inferability") in ("single", "narrow")
        and conf >= BLIND_MIN_CONFIDENCE
        and v.get("match") in ("exact", "close")
    )


def judge_moment_by_id(**kwargs) -> dict:
    """`judge_moment` on its own connection — for thread pools."""
    from ..db import connect

    cx = connect()
    try:
        return judge_moment(cx, **kwargs)
    finally:
        cx.close()


def why_clear_text(v: dict) -> str:
    """The card's `why_clear` from a passing blind verdict — names the Japanese
    cues, never the translation."""
    cues = (v.get("cues") or "").strip()
    head = (f"Inferable without the translation ({v.get('inferability')}, "
            f"{float(v.get('confidence') or 0):.2f})")
    return (f"{head}: {cues}" if cues else head)[:400]


def fail_note(v: Optional[dict]) -> str:
    """Short reason for a failed moment (srs_moments.note / card notes)."""
    if not v:
        return "blind: no verdict"
    if v.get("error"):
        return f"blind: {v['error']}"
    return (f"blind: {v.get('inferability')} ({float(v.get('confidence') or 0):.2f}); "
            f"guess '{v.get('best_guess')}' → {v.get('match')}")[:300]
