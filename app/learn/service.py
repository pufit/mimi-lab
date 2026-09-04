"""Learning layer: tokenization, furigana, comprehension (Migaku-faithful),
Moments word-search, clip extraction, and new-word gloss/frequency.

Tokenizer: fugashi.Tagger() (UniDic) singleton. Readings are normalized to
hiragana (jaconv.kata2hira). The "lemma" we store is the orthographic base
form (`orthBase`) which matches Migaku's dictForm convention far better than
UniDic's `lemma` field (e.g. これ stays これ, not 此れ; conjugated verbs go to
their dictionary surface form). We fall back lemma -> surface.

Comprehension reproduces Migaku's own decompiled formula (notes/MIGAKU_INTEGRATION.md
§3): per-sentence C4e (ignored removed from denominator + uniqueUnknown
penalty), page-weighted E4e (readability blend), S4e rating label. "Known" =
known_words.status == 'KNOWN'; IGNORED is excluded from the denominator;
LEARNING/UNKNOWN = not known.
"""
from __future__ import annotations

import datetime as _dt
import json
import math
import re
import subprocess
import time
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Optional

import jaconv

from ..config import settings
from ..db import connect, cursor, kv_get, kv_set
from ..models import ComprehensionResult, Moment, NewWord, SubtitleLine

# --------------------------------------------------------------------------
# Tokenizer
# --------------------------------------------------------------------------

_TAGGER = None


def _tagger():
    """fugashi.Tagger() singleton (lazy — UniDic load is ~0.3s)."""
    global _TAGGER
    if _TAGGER is None:
        import fugashi

        _TAGGER = fugashi.Tagger()
    return _TAGGER


# Part-of-speech (pos1) buckets we treat as non-content (skippable).
_SKIP_POS = {"補助記号", "空白", "記号"}

# Grammar / function-word POS. Still counted in comprehension (Migaku counts
# particles too), but never surfaced as "new words to study" — particles,
# auxiliaries and interjections aren't vocabulary (fixes e.g. かあ/なぁ).
_NON_VOCAB_POS = {"助詞", "助動詞", "感動詞"}

# Casual / contracted-form overrides: map a surface (or orthBase) onto the
# dictionary form Migaku would record. fugashi+UniDic already lemmatizes most
# conjugations (e.g. しています -> する + いる), so this table only patches the
# casual spoken contractions UniDic tends to mis-segment or leave surface-y.
# Kept small & documented; extend as drift is observed.
_CONTRACTIONS: dict[str, str] = {
    "てる": "ている",
    "でる": "でいる",
    "とく": "ておく",
    "どく": "でおく",
    "ちゃう": "てしまう",
    "じゃう": "でしまう",
    "なきゃ": "なければ",
    "なくちゃ": "なくては",
    "きゃ": "ければ",
    "ねえ": "ない",
    "めえ": "まい",
}

_PUNCT_RE = re.compile(r"^[\s　-〿＀-￯!-/:-@]+$")
_KANJI_RE = re.compile(r"[一-鿿㐀-䶿]")
_KANA_RE = re.compile(r"[぀-ヿ]")

# A token carries linguistic content iff it contains at least one letter-like
# character: kana, kanji (incl. CJK ext-A), iteration/abbreviation marks,
# half-width katakana, or latin/digit (incl. full-width). Everything else is a
# pure symbol/punctuation token — e.g. …  →  ♬  《》  ♬～  ⚟（ — which Migaku's
# analyzer emits with an EMPTY part-of-speech. fugashi tagged those 補助記号 (so
# _SKIP_POS dropped them); Migaku does not, and _PUNCT_RE only covers the
# CJK-symbols / half-+full-width / ASCII punctuation ranges — it MISSES the
# General-Punctuation (…, U+2026), Arrows (→) and Misc-Symbols (♬, ⚟) blocks.
# Such tokens carry no comprehension signal (Migaku itself excludes them), so we
# must drop them or they get miscounted as unknown words and tank the score.
# NB: katakana range stops at ヺ (U+30FA) and resumes at ヽ (U+30FD) to EXCLUDE
# ・(U+30FB) and ー(U+30FC): a lone middle-dot / prolonged-sound mark is
# punctuation, not a word (inside a word the surrounding kana already mark it).
_CONTENT_RE = re.compile(
    r"[぀-ヺヽ-ヿ"        # hiragana + katakana (U+3040–30FF) minus ・ ー
    r"㐀-䶿一-鿿"        # CJK ext-A + unified ideographs
    r"々〆〤"            # iteration / abbreviation marks
    r"ｦ-ﾟ"              # half-width katakana
    r"0-9A-Za-z"        # ASCII alphanumerics
    r"０-９Ａ-Ｚａ-ｚ"    # full-width alphanumerics
    r"]"
)


def _is_content(*cands: Optional[str]) -> bool:
    """True if any candidate (dictForm / surface) has a linguistic character.

    Used to drop symbol-only tokens that Migaku's tokenizer emits with empty
    POS (the old POS-based / _PUNCT_RE filters miss them). See _CONTENT_RE."""
    return any(bool(c) and bool(_CONTENT_RE.search(c)) for c in cands)


def _to_hira(s: Optional[str]) -> str:
    if not s:
        return ""
    return jaconv.kata2hira(s)


def _feat(word, *names):
    f = word.feature
    for n in names:
        v = getattr(f, n, None)
        if v and v != "*":
            return v
    return None


def _lemma_of(word) -> str:
    """Migaku-style dictForm: prefer orthographic base form, fall back to the
    UniDic lemma (with the trailing `-reading` lemma_id suffix stripped), then
    the raw surface."""
    base = _feat(word, "orthBase", "lemmaBase")
    if not base:
        lemma = _feat(word, "lemma")
        if lemma:
            base = lemma.split("-")[0]
    if not base:
        base = word.surface
    return _CONTRACTIONS.get(base, base)


def _reading_of(word) -> str:
    """Contextual reading in hiragana (kana/pron is the surface reading, which
    is what we want for furigana)."""
    kana = _feat(word, "kana", "pron", "kanaBase", "pronBase")
    return _to_hira(kana)


def tokenize(text: str) -> list[dict]:
    """Tokenize Japanese text into content tokens.

    Returns a list of {surface, lemma, reading, pos}; reading is hiragana.
    Pure punctuation / whitespace tokens are skipped.
    """
    if not text or not text.strip():
        return []
    out: list[dict] = []
    for word in _tagger()(text):
        surface = word.surface
        if not surface or _PUNCT_RE.match(surface):
            continue
        pos1 = _feat(word, "pos1") or ""
        if pos1 in _SKIP_POS:
            continue
        out.append(
            {
                "surface": surface,
                "lemma": _lemma_of(word),
                "reading": _reading_of(word),
                "pos": pos1,
            }
        )
    return out


# --------------------------------------------------------------------------
# Furigana
# --------------------------------------------------------------------------


def _ruby_word(surface: str, reading: str) -> str:
    """Build a ruby fragment for one word. If the surface has no kanji, or the
    reading is empty/equal to the surface, emit the bare surface (no ruby)."""
    if not surface:
        return ""
    if not reading or not _KANJI_RE.search(surface):
        return _escape(surface)
    # If surface already equals its reading (all-kana word), skip ruby.
    if _to_hira(surface) == reading:
        return _escape(surface)
    return f"<ruby>{_escape(surface)}<rt>{_escape(reading)}</rt></ruby>"


def _escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def furigana(text: str) -> str:
    """Return an HTML ruby string for `text` (best-effort, per-word ruby).

    Non-content punctuation is preserved verbatim between words by walking the
    tagger output and emitting each surface (kanji words get <ruby>)."""
    if not text or not text.strip():
        return _escape(text or "")
    parts: list[str] = []
    for word in _tagger()(text):
        surface = word.surface
        if not surface:
            continue
        if _PUNCT_RE.match(surface):
            parts.append(_escape(surface))
            continue
        reading = _reading_of(word)
        parts.append(_ruby_word(surface, reading))
    return "".join(parts)


# --------------------------------------------------------------------------
# Comprehension — Migaku's decompiled formula (notes/MIGAKU_INTEGRATION.md §3)
# --------------------------------------------------------------------------


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _c4e(total: int, unknown: int, known: int, ignored: int, unique_unknown: int) -> float:
    """Per-sentence comprehension score.

    s = total - ignored  (ignored removed from the denominator)
    all-known -> 100
    else score = (known/s)*100 * penalty
      penalty = uniqueUnknown<=2 ? 1 : 1 - clamp(uniqueUnknown/s * 2, 0, 1)
    """
    s = total - ignored
    if s <= 0:
        return 100.0
    if unknown <= 0:
        return 100.0
    if unique_unknown <= 2:
        penalty = 1.0
    else:
        penalty = 1.0 - _clamp((unique_unknown / s) * 2.0, 0.0, 1.0)
    return (known / s) * 100.0 * penalty


def _s4e(t: float) -> str:
    """Rating label."""
    if t >= 99:
        return "Exceptional"
    if t >= 95:
        return "Excellent"
    if t >= 90:
        return "Great"
    if t >= 80:
        return "Good"
    if t >= 70:
        return "Approachable"
    if t >= 60:
        return "Challenging"
    return "Ambitious"


def _e4e(scores: list[tuple[float, int]], one_unknown_snts: int, mult_unknown_snts: int) -> float:
    """Page general comprehension.

    scores: list of (sentence_score, sentence_weight) where weight = words in
    the sentence (post-ignore denominator). word-count-weighted mean; if every
    sentence is 100 -> 100; then a readability penalty
      a = 100 - (multUnknownSnts*0.7 + oneUnknownSnts*0.3)
    is blended in:
      final = floor(weightedMean - (weightedMean - a)/2 * (weightedMean*0.01))
    clamped 0..100.
    """
    if not scores:
        return 0.0
    total_w = sum(w for _, w in scores)
    if total_w <= 0:
        return 0.0
    weighted_mean = sum(sc * w for sc, w in scores) / total_w
    if all(abs(sc - 100.0) < 1e-9 for sc, _ in scores):
        return 100.0
    # Migaku's page penalty uses raw sentence counts, tuned for a single
    # on-screen "page" (a few sentences). Normalize to per-sentence proportions
    # so it scales to a full episode (hundreds of sentences) instead of driving
    # `a` hugely negative and collapsing the score to 0.
    n = len(scores)
    one_frac = one_unknown_snts / n
    mult_frac = mult_unknown_snts / n
    a = 100.0 - (mult_frac * 0.7 + one_frac * 0.3) * 100.0
    final = weighted_mean - (weighted_mean - a) / 2.0 * (weighted_mean * 0.01)
    return _clamp(math.floor(final), 0.0, 100.0)


def _known_lookup() -> tuple[set[str], set[str]]:
    """Load known-word identity sets from known_words.

    Returns (known_forms, ignored_forms) where each is a set of dict_form
    strings (status KNOWN / IGNORED respectively). Matching is by dict_form
    alone — reading is not required to match, since our tokenizer's reading may
    differ slightly from Migaku's. LEARNING/UNKNOWN are simply absent from
    known_forms (= not known)."""
    known: set[str] = set()
    ignored: set[str] = set()
    with connect() as cx:
        for row in cx.execute("SELECT dict_form, status FROM known_words"):
            st = (row["status"] or "").upper()
            df = row["dict_form"]
            if not df:
                continue
            if st == "KNOWN":
                known.add(df)
            elif st == "IGNORED":
                ignored.add(df)
    # SRS overlay (SRS_DESIGN §2.6): a word the study deck considers known
    # counts as known everywhere, and beats a stale Migaku IGNORED row. Guarded:
    # a very old DB without the srs_* tables must keep working.
    try:
        from ..srs.service import known_forms as _srs_known

        srs = _srs_known()
        known |= srs
        ignored -= srs
    except Exception as e:
        log.debug("srs overlay unavailable: %s", e)
    return known, ignored


def _srs_known_predicate(alias: str = "ll") -> str:
    """SQL fragment excluding lemmas the SRS deck counts as known (§2.6.2).

    Returns `''` when the SRS package is unavailable, so every caller can
    interpolate it unconditionally.
    """
    try:
        from ..srs.service import KNOWN_SQL

        return (f" AND NOT EXISTS (SELECT 1 FROM srs_cards sc "
                f"WHERE sc.lemma = {alias}.lemma AND {KNOWN_SQL})")
    except Exception as e:
        log.debug("srs known predicate unavailable: %s", e)
        return ""


def _token_status(lemma: str, surface: Optional[str], known: set[str],
                  ignored: set[str], reading: Optional[str] = None) -> str:
    """Classify a token as KNOWN / IGNORED / UNKNOWN.

    Tries dict_form, then surface, then the kana READING against the known/
    ignored sets. The reading fallback matches words Migaku tracks under their
    kana form — e.g. the token ２人 (read ふたり) is KNOWN because the user knows
    the word ふたり, which Migaku stores in kana. Migaku unifies these by
    reading/lexeme; matching the reading against our (kana-form) known set
    reproduces that with low homophone risk, since kanji words are stored under
    kanji dict_forms (so a reading only matches a genuinely kana-lexeme word)."""
    for cand in (lemma, surface):
        if not cand:
            continue
        if cand in known:
            return "KNOWN"
        if cand in ignored:
            return "IGNORED"
    if reading:
        if reading in known:
            return "KNOWN"
        if reading in ignored:
            return "IGNORED"
    return "UNKNOWN"


def comprehension_aligned(
    episode_id: int,
    known_sets: Optional[tuple[set[str], set[str]]] = None,
) -> ComprehensionResult:
    """Compute comprehension for an episode from its locally-tokenized
    line_lemmas joined against the known-word set, using Migaku's formula.

    Persists the result onto the episodes row (comprehension_pct/rating,
    source='aligned', new_word_count) and returns a ComprehensionResult.

    `known_sets` lets bulk callers (recompute_all) load the known-words table
    ONCE instead of per episode, avoiding repeated full-table loads during a
    bulk re-rank.
    """
    known, ignored = known_sets if known_sets is not None else _known_lookup()

    # Group tokens by line, preserving line order.
    with connect() as cx:
        rows = cx.execute(
            "SELECT line_id, lemma, surface, reading, pos FROM line_lemmas "
            "WHERE episode_id=? AND token_source IN ('local','migaku','migaku-local') "
            "ORDER BY line_id",
            (episode_id,),
        ).fetchall()

    by_line: dict[int, list] = {}
    for r in rows:
        by_line.setdefault(r["line_id"], []).append(r)

    scores: list[tuple[float, int]] = []  # (sentence_score, weight)
    one_unknown_snts = 0
    mult_unknown_snts = 0
    total_tokens = 0
    known_tokens = 0
    # new-word aggregation (unknown lemmas across the episode)
    unknown_counts: dict[str, int] = {}
    unknown_reading: dict[str, str] = {}

    for _line_id, toks in by_line.items():
        n_known = n_ignored = n_unknown = 0
        unique_unknown_forms: set[str] = set()
        for t in toks:
            # Drop symbol-only tokens (…, →, ♬, 《》, ♬～, ⚟（ …). Migaku's
            # analyzer emits these with an EMPTY pos, so neither _SKIP_POS nor the
            # old _PUNCT_RE caught them — they were miscounted as unknown content
            # words and tanked comprehension on punctuation-heavy subs. They carry
            # no comprehension signal; Migaku itself excludes them. (Defensive:
            # also handles rows ingested before the ingest/backfill filter fix.)
            if not _is_content(t["lemma"], t["surface"]):
                continue
            st = _token_status(t["lemma"], t["surface"], known, ignored, reading=t["reading"])
            if st == "KNOWN":
                n_known += 1
            elif st == "IGNORED":
                n_ignored += 1
            else:
                n_unknown += 1
                # only real vocabulary feeds the "study these first" list —
                # skip particles/auxiliaries/interjections.
                if (t["pos"] or "") in _NON_VOCAB_POS:
                    continue
                form = t["lemma"] or t["surface"]
                unique_unknown_forms.add(form)
                unknown_counts[form] = unknown_counts.get(form, 0) + 1
                if form not in unknown_reading and t["reading"]:
                    unknown_reading[form] = t["reading"]
        total = n_known + n_ignored + n_unknown
        if total == 0:
            continue
        u_unique = len(unique_unknown_forms)
        sc = _c4e(total, n_unknown, n_known, n_ignored, u_unique)
        weight = max(0, total - n_ignored)
        scores.append((sc, weight))
        total_tokens += total
        known_tokens += n_known
        if u_unique == 1:
            one_unknown_snts += 1
        elif u_unique >= 2:
            mult_unknown_snts += 1

    # Page comprehension = Migaku's own decompiled formula: per-sentence C4e
    # (ignored removed from the denominator + a uniqueUnknown penalty), then the
    # word-count-weighted page mean E4e with its readability blend.
    #
    # A flat known/total ratio was misleading before symbol-only tokens were
    # filtered. Those junk tokens (…, →, ♬) counted as unknowns, which both
    # depressed C4e and inflated the multi-unknown readability penalty, biasing
    # the formula low. With them dropped (above) the formula is accurate AND
    # consistent; the flat ratio is not — it ignores sentence structure, so it
    # over-scores hard dialogue and under-scores symbol-heavy monologue.
    # Synthetic cross-series fixtures show a flat known/total ratio is materially
    # less stable than the structured formula.
    page = _e4e(scores, one_unknown_snts, mult_unknown_snts)
    rating = _s4e(page)
    new_words = _build_new_words(unknown_counts, unknown_reading)
    unknown_unique = len(new_words)

    pct = round(float(page), 2)
    with cursor() as cx:
        cx.execute(
            "UPDATE episodes SET comprehension_pct=?, comprehension_rating=?, "
            "comprehension_source='aligned', new_word_count=?, updated_at=datetime('now') "
            "WHERE id=?",
            (pct, rating, unknown_unique, episode_id),
        )

    return ComprehensionResult(
        episode_id=episode_id,
        comprehension_pct=pct,
        rating=rating,
        source="aligned",
        total_tokens=total_tokens,
        known_tokens=known_tokens,
        unknown_unique=unknown_unique,
        new_words=new_words,
    )


def record_exact_comprehension(episode_id: int, stats: dict) -> ComprehensionResult:
    """Persist Migaku-exact comprehension PUSHED by the Connector
    (notes/REMOTE_PLAYBACK_DESIGN.md §3, P2).

    Topology change: Migaku's Player runs on the *viewing* machine now, so the
    Connector scrapes its ComprehensionStats panel after an episode is opened and
    POSTs the result here (/api/learn/comprehension/upload). `stats` is Migaku's
    own full-content panel output {pct, rating, known, unknown, ignored, learning}.

    We store the exact pct/rating (source='exact') + the unique-unknown count. As
    before, we do NOT overwrite line_lemmas — Migaku's SubtitleBrowser is a
    virtualized list (only ~8 on-screen rows in the DOM), so a token scrape is
    partial; our full 'local' tokenization stays the corpus of record.
    """
    stats = stats or {}
    # VALIDATE before persisting. A Migaku UI rename makes the scrape return
    # pct=None; the old coercion (`or 0.0`) stored that as a permanent 0%
    # 'exact' score that recomputes never overwrite — silent data poisoning.
    raw_pct = stats.get("pct")
    try:
        pct = round(float(raw_pct), 2)
    except (TypeError, ValueError):
        raise ValueError(f"exact comprehension upload rejected: pct={raw_pct!r} "
                         "is not a number (Migaku panel scrape drift?)")
    if not (0.0 <= pct <= 100.0):
        raise ValueError(f"exact comprehension upload rejected: pct={pct} out of range")
    rating = stats.get("rating") or _s4e(pct)
    known = int(stats.get("known", 0) or 0)
    unknown_unique = int(stats.get("unknown", 0) or 0)
    ignored = int(stats.get("ignored", 0) or 0)

    # Formula-drift canary: where an aligned score exists, a big divergence from
    # Migaku's own number means either tokenizer or formula drift — the one
    # coupling class that otherwise fails silently.
    try:
        with connect() as cx:
            prev = cx.execute(
                "SELECT comprehension_pct, comprehension_source FROM episodes WHERE id=?",
                (episode_id,),
            ).fetchone()
        if prev and prev["comprehension_source"] == "aligned" and prev["comprehension_pct"] is not None:
            delta = abs(float(prev["comprehension_pct"]) - pct)
            if delta > 3.0:
                log.warning("comprehension drift: episode %s aligned=%.1f vs exact=%.1f (Δ%.1f)",
                            episode_id, prev["comprehension_pct"], pct, delta)
                _record_event(
                    "comprehension",
                    f"Aligned vs Migaku-exact diverge by {delta:.1f} pts",
                    "warning",
                    detail=(f"Episode {episode_id}: aligned {prev['comprehension_pct']:.1f}% vs "
                            f"Migaku {pct:.1f}%. If this repeats, re-run the tokenizer backfill "
                            "and check the drift report in Settings."),
                    meta={"episode_id": episode_id, "aligned": prev["comprehension_pct"], "exact": pct},
                )
    except Exception:
        pass

    with cursor() as cx:
        cx.execute(
            "UPDATE episodes SET comprehension_pct=?, comprehension_rating=?, "
            "comprehension_source='exact', new_word_count=?, updated_at=datetime('now') WHERE id=?",
            (pct, rating, unknown_unique, episode_id),
        )
    return ComprehensionResult(
        episode_id=episode_id,
        comprehension_pct=pct,
        rating=rating,
        source="exact",
        total_tokens=known + unknown_unique + ignored,
        known_tokens=known,
        unknown_unique=unknown_unique,
        new_words=[],
    )


# --------------------------------------------------------------------------
# New words (freq rank + gloss)
# --------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _gloss_db_path() -> Optional[str]:
    p = Path(__file__).resolve().parent / "data" / "glosses.sqlite"
    return str(p) if p.exists() else None


def _glosses_for(forms: Iterable[str]) -> dict[str, str]:
    """Best-effort gloss lookup. Returns {form: gloss}. Empty if no gloss DB."""
    forms = [f for f in forms if f]
    if not forms:
        return {}
    path = _gloss_db_path()
    if not path:
        return {}
    import sqlite3

    out: dict[str, str] = {}
    try:
        gx = sqlite3.connect(path)
        gx.row_factory = sqlite3.Row
        qmarks = ",".join("?" * len(forms))
        for row in gx.execute(
            f"SELECT form, gloss FROM glosses WHERE form IN ({qmarks})", list(forms)
        ):
            if row["form"] not in out:
                out[row["form"]] = row["gloss"]
        gx.close()
    except Exception:
        return out
    return out


def _build_new_words(counts: dict[str, int], readings: dict[str, str]) -> list[NewWord]:
    """Attach freq rank (lemma_freq) + gloss (best-effort) to unknown forms,
    sorted by frequency rank (commonest first), then by occurrence count."""
    if not counts:
        return []
    forms = list(counts.keys())
    ranks: dict[str, Optional[int]] = {f: None for f in forms}
    with connect() as cx:
        qmarks = ",".join("?" * len(forms))
        for row in cx.execute(
            f"SELECT lemma, rank FROM lemma_freq WHERE lemma IN ({qmarks})", forms
        ):
            ranks[row["lemma"]] = row["rank"]
    glosses = _glosses_for(forms)

    words = [
        NewWord(
            lemma=f,
            reading=readings.get(f) or None,
            freq_rank=ranks.get(f),
            gloss=glosses.get(f),
            count=counts[f],
        )
        for f in forms
    ]
    # Drop tokenizer junk: all-kana fragments with no dictionary entry — e.g.
    # かあ from a cut-off "おかあ…". A real word has either kanji or a JMdict
    # gloss; a bare-kana fragment with neither is noise, not vocabulary.
    words = [w for w in words if _KANJI_RE.search(w.lemma) or w.gloss]
    # sort: known rank first (ascending), then unranked by count desc
    words.sort(key=lambda w: (w.freq_rank is None, w.freq_rank or 0, -w.count))
    return words


def new_words(episode_id: int, limit: int = 200) -> list[NewWord]:
    """Unknown lemmas in an episode with freq rank + gloss (best-effort)."""
    known, ignored = _known_lookup()
    counts: dict[str, int] = {}
    readings: dict[str, str] = {}
    with connect() as cx:
        rows = cx.execute(
            "SELECT lemma, surface, reading, pos FROM line_lemmas WHERE episode_id=?",
            (episode_id,),
        ).fetchall()
    for r in rows:
        if (r["pos"] or "") in _NON_VOCAB_POS:
            continue
        st = _token_status(r["lemma"], r["surface"], known, ignored, reading=r["reading"])
        if st != "UNKNOWN":
            continue
        form = r["lemma"] or r["surface"]
        if not form:
            continue
        counts[form] = counts.get(form, 0) + 1
        if form not in readings and r["reading"]:
            readings[form] = r["reading"]
    words = _build_new_words(counts, readings)
    return words[:limit]


# --------------------------------------------------------------------------
# Moments (word-search) + clip extraction
# --------------------------------------------------------------------------


def _clip_urls(line_id: int) -> tuple[Optional[str], Optional[str]]:
    """Static /clips URLs when BOTH files exist on disk, else (None, None).

    The old fallback emitted the POST-only /api/learn/clip endpoint string as
    if it were media URLs: the UI rendered it as a broken <img> + a 405 audio
    source, and (being truthy) it suppressed the explicit "Make clip" button.
    Clip URLs now mean exactly "these files exist"; extraction is always an
    explicit POST (clip button, sentence player warm-up, apkg export)."""
    img = settings.clips_dir / f"line_{line_id}.jpg"
    aud = settings.clips_dir / f"line_{line_id}.m4a"
    if img.exists() and aud.exists():
        return f"/clips/line_{line_id}.jpg", f"/clips/line_{line_id}.m4a"
    return None, None


def _moment_from_row(r, unknown_count: Optional[int] = None) -> Moment:
    img, aud = _clip_urls(r["line_id"])
    return Moment(
        line_id=r["line_id"],
        anilist_id=r["anilist_id"],
        episode_id=r["episode_id"],
        # show name first — the episode title ("Episode 1") made results anonymous
        title=r["romaji"] or r["english"] or r["title"],
        episode_title=r["title"],
        ep_number=r["ep_number"],
        text=r["text"],
        text_furigana=r["text_furigana"],
        translation=r["translation"],
        start_ms=r["start_ms"],
        end_ms=r["end_ms"],
        video_path=r["video_path"],
        image_url=img,
        audio_url=aud,
        unknown_count=unknown_count,
    )


_MOMENT_SELECT = """
SELECT sl.id AS line_id, sl.episode_id AS episode_id, sl.idx AS idx,
       sl.start_ms AS start_ms, sl.end_ms AS end_ms, sl.text AS text,
       sl.text_furigana AS text_furigana, sl.translation AS translation,
       e.anilist_id AS anilist_id, e.ep_number AS ep_number,
       e.video_path AS video_path, e.title AS title,
       t.romaji AS romaji, t.english AS english
FROM {src}
JOIN subtitle_lines sl ON sl.id = {line_col}
JOIN episodes e ON e.id = sl.episode_id
JOIN titles t ON t.anilist_id = e.anilist_id
"""


def _unknown_counts(cx, line_ids: list[int]) -> dict[int, int]:
    """Distinct not-yet-known lemma count per line (for i+1 ranking).

    A lemma counts as unknown when no known_words row marks it KNOWN or
    IGNORED (ignored words are excluded from difficulty, matching the
    comprehension denominator).
    """
    counts: dict[int, int] = {}
    CHUNK = 400
    for i in range(0, len(line_ids), CHUNK):
        chunk = line_ids[i:i + CHUNK]
        marks = ",".join("?" for _ in chunk)
        rows = cx.execute(
            f"SELECT ll.line_id AS line_id, COUNT(DISTINCT ll.lemma) AS unk "
            f"FROM line_lemmas ll "
            f"WHERE ll.line_id IN ({marks}) "
            f"AND NOT EXISTS (SELECT 1 FROM known_words kw WHERE kw.dict_form = ll.lemma "
            f"                AND kw.status IN ('KNOWN','IGNORED')) "
            f"{_srs_known_predicate('ll')} "
            f"GROUP BY ll.line_id",
            chunk,
        ).fetchall()
        for r in rows:
            counts[r["line_id"]] = r["unk"]
    return counts


def moments_search(
    query: str,
    limit: int = 50,
    offset: int = 0,
    anilist_id: Optional[int] = None,
    sort: str = "position",
    downloaded_only: bool = True,
) -> list[Moment]:
    """Find every subtitle line where `query` appears.

    By default only DOWNLOADED content is searched (episodes with a local
    video file) — every returned moment is playable/clippable, instead of
    drowning results in the ~97% of the corpus that is analysis-only subs.
    `downloaded_only=False` restores the whole-corpus search.

    Primary path: exact dictionary-form match via line_lemmas.lemma, then
    surface form — two separate indexed lookups (the old `lemma=? OR surface=?`
    defeated the index and made lookup latency grow linearly with corpus size).
    Fallback:
    free-text substring via subtitle_fts (trigram, needs >=3 chars).

    sort='position' (show/episode/line order) or 'iplus1' (fewest unknown
    words first — best mining sentences on top). `anilist_id` filters to one
    show; `offset` pages through results.
    """
    query = (query or "").strip()
    if not query:
        return []

    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))
    # candidate pool: enough to page + rank; bounded so a particle like の
    # can't drag thousands of rows through scoring
    pool = min(800, offset + limit if sort == "position" else max(400, offset + limit))

    seen: set[int] = set()
    rows_out: list = []
    show_cond = "AND e.anilist_id = ? " if anilist_id else ""
    if downloaded_only:
        show_cond += "AND e.video_path IS NOT NULL AND e.video_path != '' "

    with connect() as cx:
        # --- primary: lemma, then surface — both hit their own index ---
        for col in ("ll.lemma", "ll.surface"):
            if len(rows_out) >= pool:
                break
            sql = (
                _MOMENT_SELECT.format(src="line_lemmas ll", line_col="ll.line_id")
                + f"WHERE {col} = ? {show_cond}"
                "GROUP BY sl.id ORDER BY e.anilist_id, e.ep_number, sl.idx LIMIT ?"
            )
            args: list = [query]
            if anilist_id:
                args.append(anilist_id)
            args.append(pool)
            for r in cx.execute(sql, args):
                if r["line_id"] in seen:
                    continue
                seen.add(r["line_id"])
                rows_out.append(r)

        # --- fallback: free-text (substring) via FTS trigram ---
        if len(rows_out) < pool and len(query) >= 3:
            fts_sql = (
                _MOMENT_SELECT.format(src="subtitle_fts f", line_col="f.line_id")
                + f"WHERE subtitle_fts MATCH ? {show_cond}"
                "ORDER BY e.anilist_id, e.ep_number, sl.idx LIMIT ?"
            )
            try:
                # phrase-quote to treat the query literally
                match = '"' + query.replace('"', '""') + '"'
                args = [match]
                if anilist_id:
                    args.append(anilist_id)
                args.append(pool)
                for r in cx.execute(fts_sql, args):
                    if r["line_id"] in seen:
                        continue
                    seen.add(r["line_id"])
                    rows_out.append(r)
                    if len(rows_out) >= pool:
                        break
            except Exception:
                pass

        unk = _unknown_counts(cx, [r["line_id"] for r in rows_out])

    moments = [_moment_from_row(r, unknown_count=unk.get(r["line_id"], 0)) for r in rows_out]
    if sort == "iplus1":
        moments.sort(key=lambda m: (m.unknown_count if m.unknown_count is not None else 99,
                                    m.anilist_id, m.ep_number or 0, m.start_ms))
    return moments[offset:offset + limit]


def translate_line(line_id: int) -> dict:
    """On-demand MT for ONE subtitle line (the Moments 'translate' button).

    Bulk-translating an entire library is wasteful; translating only the lines
    a user opens, on demand, and caching into subtitle_lines.translation keeps
    cost proportional to actual use. Uses the fast default model with
    the surrounding lines as context. Idempotent: returns the cached
    translation when present.
    """
    from .. import llm as _llm

    with connect() as cx:
        row = cx.execute(
            "SELECT id, episode_id, idx, text, translation FROM subtitle_lines WHERE id=?",
            (line_id,),
        ).fetchone()
        if not row:
            raise ValueError(f"line {line_id} not found")
        if row["translation"]:
            return {"line_id": line_id, "translation": row["translation"], "cached": True}
        if not _llm.available():
            return {"line_id": line_id, "translation": None, "error": "LLM not configured"}
        ctx_rows = cx.execute(
            "SELECT idx, text FROM subtitle_lines WHERE episode_id=? "
            "AND idx BETWEEN ? AND ? ORDER BY idx",
            (row["episode_id"], row["idx"] - 2, row["idx"] + 2),
        ).fetchall()

    context = "\n".join(r["text"] for r in ctx_rows)
    res = _llm.claude_json(
        "You translate Japanese anime subtitle lines to natural, concise English. "
        "Use the surrounding lines only as context; translate ONLY the target line. "
        'Return JSON {"t": "<english>"}.',
        f"Context:\n{context}\n\nTarget line:\n{row['text']}",
        max_tokens=200,
        timeout=20.0,
    )
    tr = (res or {}).get("t") if isinstance(res, dict) else None
    tr = (str(tr).strip() or None) if tr else None
    if tr:
        with cursor() as cx:
            cx.execute("UPDATE subtitle_lines SET translation=? WHERE id=?", (tr, line_id))
    return {"line_id": line_id, "translation": tr, "cached": False}


def _run(cmd: list[str], timeout: int = 120) -> tuple[int, str]:
    try:
        p = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
        return p.returncode, (p.stderr or "")[-2000:]
    except subprocess.TimeoutExpired:
        return 124, "timeout"
    except FileNotFoundError as e:
        return 127, str(e)


def extract_clip(line_id: int) -> dict:
    """Extract a screenshot + audio clip for a subtitle line via ffmpeg.

    Screenshot: -ss start -frames:v 1 ; audio: -ss start -to end -c:a aac.
    Writes into settings.clips_dir as line_<id>.jpg /line_<id>.m4a and returns
    {image_url, audio_url} served at /clips/...  Requires the episode video_path.
    """
    with connect() as cx:
        row = cx.execute(
            "SELECT sl.start_ms AS start_ms, sl.end_ms AS end_ms, e.video_path AS video_path "
            "FROM subtitle_lines sl JOIN episodes e ON e.id = sl.episode_id "
            "WHERE sl.id=?",
            (line_id,),
        ).fetchone()
    if not row:
        raise RuntimeError(f"line {line_id} not found")
    video = row["video_path"]
    if not video or not Path(video).exists():
        raise RuntimeError(f"line {line_id}: episode has no playable video_path ({video!r})")

    settings.clips_dir.mkdir(parents=True, exist_ok=True)
    start_s = max(0, row["start_ms"]) / 1000.0
    end_s = max(row["end_ms"], row["start_ms"] + 500) / 1000.0
    img_path = settings.clips_dir / f"line_{line_id}.jpg"
    aud_path = settings.clips_dir / f"line_{line_id}.m4a"

    # screenshot — seek before input for speed; one frame at line start.
    rc_i, err_i = _run(
        [
            "ffmpeg", "-y", "-ss", f"{start_s:.3f}", "-i", video,
            "-frames:v", "1", "-q:v", "3", str(img_path),
        ]
    )
    # audio clip — accurate trim with -ss/-to, re-encode to AAC.
    rc_a, err_a = _run(
        [
            "ffmpeg", "-y", "-ss", f"{start_s:.3f}", "-to", f"{end_s:.3f}",
            "-i", video, "-vn", "-c:a", "aac", "-b:a", "128k", str(aud_path),
        ]
    )
    if rc_i != 0 and not img_path.exists():
        raise RuntimeError(f"ffmpeg screenshot failed: {err_i}")
    if rc_a != 0 and not aud_path.exists():
        raise RuntimeError(f"ffmpeg audio failed: {err_a}")

    return {
        "image_url": f"/clips/line_{line_id}.jpg" if img_path.exists() else None,
        "audio_url": f"/clips/line_{line_id}.m4a" if aud_path.exists() else None,
    }


# --------------------------------------------------------------------------
# Misc helpers used by routers
# --------------------------------------------------------------------------


def episode_lines(episode_id: int) -> list[SubtitleLine]:
    with connect() as cx:
        rows = cx.execute(
            "SELECT id, episode_id, idx, start_ms, end_ms, text, text_furigana, translation "
            "FROM subtitle_lines WHERE episode_id=? ORDER BY idx",
            (episode_id,),
        ).fetchall()
    return [SubtitleLine(**dict(r)) for r in rows]


# --------------------------------------------------------------------------
# Job handlers (pipeline automation)
# --------------------------------------------------------------------------
import logging  # noqa: E402

log = logging.getLogger("mimi_lab.learn")


def _record_event(category, title, kind="info", detail=None, meta=None) -> None:
    """Record a pipeline event; never raises (lazy import to avoid cycles)."""
    try:
        from ..events import service as events
        events.record(category, title, kind, detail=detail, meta=meta)
    except Exception as e:  # pragma: no cover
        log.debug("event record (%s) failed: %s", category, e)


def _episode_label(episode_id: int) -> str:
    """Human label like 'Example Series · Episode 5' (falls back to the id)."""
    try:
        with connect() as cx:
            r = cx.execute(
                "SELECT e.ep_number, t.romaji, t.english FROM episodes e "
                "JOIN titles t ON t.anilist_id = e.anilist_id WHERE e.id=?",
                (episode_id,),
            ).fetchone()
        if r:
            return f"{r['romaji'] or r['english'] or 'Unknown'} · Episode {r['ep_number']}"
    except Exception:
        pass
    return f"episode {episode_id}"


def _job_comprehension(payload: dict) -> None:
    """`comprehension` job: aligned (local) comprehension for an episode.

    No per-episode event — during a whole-title/bulk fill this fires thousands of
    times and floods the notification feed. Progress is already signalled by the
    per-title 'subtitle' event, and the score shows on the episode in the UI.
    """
    episode_id = payload.get("episode_id")
    if episode_id is None:
        raise RuntimeError("comprehension: missing episode_id")
    comprehension_aligned(int(episode_id))


def _job_comprehension_exact(payload: dict) -> None:
    """Deprecated no-op (kept registered).

    Migaku-exact comprehension is no longer pulled by the server: the Connector
    scrapes Migaku where it runs and POSTs the result to
    /api/learn/comprehension/upload (see `record_exact_comprehension`). A
    background job can't reach the Connector (its WS lives on the app loop), so
    this handler just logs — registered so any stale 'comprehension_exact' rows
    drain cleanly instead of erroring as 'no handler'.
    """
    log.info(
        "comprehension_exact job is deprecated (now Connector push); ignoring payload=%s",
        payload,
    )


# --------------------------------------------------------------------------- #
# Migaku drift self-test — detect when a Migaku extension update breaks/changes
# the reverse-engineered tokenizer, instead of silently degrading to fugashi.
# --------------------------------------------------------------------------- #
_DRIFT_BASELINE_KEY = "migaku.drift.baseline"
_TESTSHOW_SRT = "lib/TestShow/TestShow - S01E01.ja.srt"


def _testshow_lines() -> list[str]:
    from ..config import ROOT
    import pysubs2
    path = ROOT / _TESTSHOW_SRT
    subs = pysubs2.load(str(path))
    return [ev.text for ev in subs if not ev.is_comment and ev.text.strip()]


def migaku_drift_check(record: bool = True) -> dict:
    """Tokenize the fixed TestShow clip through Migaku's sidecar and compare the
    result to a stored baseline.

    Migaku is a closed extension we drive by reverse engineering; an auto-update
    can silently change its tokenizer (→ comprehension shifts) or break the
    sidecar entirely (→ we degrade to fugashi) with no signal. This
    turns that into an explicit, notified health check:

      * sidecar offline           → warning event (running degraded on fugashi)
      * extension version changed  → warning (verify comprehension parity)
      * token output changed       → warning (segmentation drift)
      * first run                  → record baseline

    Returns a report dict; safe to run on a schedule or on demand.
    """
    import hashlib
    import json as _json
    from ..db import kv_get, kv_set
    from . import migaku_tok

    report: dict = {"status": "unknown", "ext": None, "token_count": None}

    h = migaku_tok.health()
    ext = h.get("ext")
    report["ext"] = ext

    try:
        texts = _testshow_lines()
    except Exception as e:
        report["status"] = "no_fixture"
        report["error"] = str(e)
        log.warning("drift check: could not load TestShow fixture: %s", e)
        return report

    toks = migaku_tok.tokenize_lines(texts)
    if toks is None:
        report["status"] = "sidecar_offline"
        if record:
            _record_event(
                "system", "Migaku tokenizer offline",
                "warning",
                detail="Comprehension is running on the fugashi fallback. Restart "
                       "the tokenizer sidecar (com.mimilab.tokenizer).",
            )
        return report

    forms = [t.get("dictForm") or t.get("surface") or "" for line in toks for t in (line or [])]
    forms = [f for f in forms if f]
    n = len(forms)
    forms_hash = hashlib.sha1("\n".join(sorted(set(forms))).encode("utf-8")).hexdigest()[:16]
    report.update(status="ok", token_count=n, forms_hash=forms_hash)

    raw = kv_get(_DRIFT_BASELINE_KEY)
    baseline = None
    if raw:
        try:
            baseline = _json.loads(raw)
        except Exception:
            baseline = None

    new_baseline = {"ext": ext, "token_count": n, "forms_hash": forms_hash}
    if baseline is None:
        kv_set(_DRIFT_BASELINE_KEY, _json.dumps(new_baseline))
        report["status"] = "baseline_set"
        log.info("drift check: baseline set (ext=%s, %d tokens)", ext, n)
        return report

    drift_reasons = []
    if baseline.get("ext") != ext:
        drift_reasons.append(f"extension {baseline.get('ext')} → {ext}")
    if baseline.get("forms_hash") != forms_hash or baseline.get("token_count") != n:
        drift_reasons.append(
            f"tokens {baseline.get('token_count')} → {n} (segmentation changed)"
        )

    if drift_reasons:
        report["status"] = "drift"
        report["drift"] = drift_reasons
        # advance the baseline so we warn once per change, not every run
        kv_set(_DRIFT_BASELINE_KEY, _json.dumps(new_baseline))
        if record:
            _record_event(
                "system", "Migaku tokenizer drift detected", "warning",
                detail="; ".join(drift_reasons)
                + ". Re-verify comprehension parity and re-run the library backfill "
                  "(tools/backfill_migaku_comprehension.py) if needed.",
                meta={"baseline": baseline, "current": new_baseline},
            )
        log.warning("drift check: %s", "; ".join(drift_reasons))
    return report


def _job_migaku_drift(payload: dict) -> None:
    migaku_drift_check(record=True)


def recompute_all_comprehension() -> dict:
    """Re-score aligned comprehension for every episode that has a corpus, so the
    backlog ranking reflects the user's CURRENT known-words. Skips episodes whose
    score came from Migaku directly (`comprehension_source='exact'`). Triggered
    after a known-words sync changes the KNOWN count."""
    with connect() as cx:
        rows = cx.execute(
            "SELECT DISTINCT ll.episode_id AS id FROM line_lemmas ll "
            "JOIN episodes e ON e.id = ll.episode_id "
            "WHERE e.comprehension_source IS NULL OR e.comprehension_source <> 'exact'"
        ).fetchall()
    ids = [r["id"] for r in rows]
    known_sets = _known_lookup()  # load once for the whole sweep
    ok = 0
    for eid in ids:
        try:
            comprehension_aligned(int(eid), known_sets=known_sets)
            ok += 1
        except Exception as e:  # pragma: no cover - keep going through the set
            log.debug("re-rank: comprehension failed for ep %s: %s", eid, e)
    log.info("re-ranked comprehension for %d/%d episode(s)", ok, len(ids))
    _record_event(
        "comprehension", f"Re-ranked backlog: {ok} episode(s) rescored", "info",
        detail="Comprehension updated against your current known words.",
    )
    # trend capture: episodes.comprehension_pct is overwritten in place, so the
    # Stats dashboard's history lives in comprehension_history — snapshot after
    # every library-wide rescore (and daily via maintenance).
    try:
        from ..maintenance import capture_comprehension_history
        capture_comprehension_history()
    except Exception:
        pass
    # the word-leverage ranking depends on the known set — invalidate its cache
    try:
        from ..db import kv_set
        kv_set("leverage.dirty", "1")
    except Exception:
        pass
    return {"rescored": ok, "total": len(ids)}


def _job_recompute_all(payload: dict) -> None:
    recompute_all_comprehension()


def sweet_spot(lo: float = 80.0, hi: float = 95.0, limit: int = 60,
               include_watched: bool = False) -> list[dict]:
    """Titles whose NEXT episode falls in the comprehension 'sweet spot' — the
    i+1 band where immersion is most effective.

    One entry per title (the old per-episode list showed the same show 8+
    times). "Next" = last watched episode + 1 (or episode 1 for an unstarted
    show) — recommending a mid-season entry point for a show you haven't
    started is not a real recommendation. `band_episodes` counts how many
    upcoming episodes of that show sit in the band ("runway at your level").

    include_watched=True switches back to the raw per-episode list (rewatch
    mode).
    """
    lo, hi = float(lo), float(hi)
    with connect() as cx:
        if include_watched:
            rows = cx.execute(
                "SELECT e.id AS episode_id, e.anilist_id, e.ep_number, e.comprehension_pct, "
                "       e.comprehension_rating, e.comprehension_source, e.new_word_count, "
                "       e.watched, e.video_path, 1 AS band_episodes, "
                "       t.romaji, t.english, t.cover_url "
                "FROM episodes e JOIN titles t ON t.anilist_id = e.anilist_id "
                "WHERE e.comprehension_pct IS NOT NULL "
                "  AND e.comprehension_pct >= ? AND e.comprehension_pct < ? "
                "ORDER BY e.comprehension_pct DESC, e.new_word_count ASC LIMIT ?",
                (lo, hi, int(limit)),
            ).fetchall()
        else:
            rows = cx.execute(
                "WITH next_eps AS ("
                "  SELECT anilist_id, "
                "         COALESCE(MAX(CASE WHEN watched=1 THEN ep_number END), 0) + 1 AS next_ep "
                "  FROM episodes GROUP BY anilist_id"
                "), band_counts AS ("
                "  SELECT e2.anilist_id, COUNT(*) AS band_episodes "
                "  FROM episodes e2 JOIN next_eps n2 ON n2.anilist_id = e2.anilist_id "
                "  WHERE COALESCE(e2.watched,0)=0 AND e2.ep_number >= n2.next_ep "
                "    AND e2.comprehension_pct >= ? AND e2.comprehension_pct < ? "
                "  GROUP BY e2.anilist_id"
                ") "
                "SELECT e.id AS episode_id, e.anilist_id, e.ep_number, e.comprehension_pct, "
                "       e.comprehension_rating, e.comprehension_source, e.new_word_count, "
                "       e.watched, e.video_path, COALESCE(bc.band_episodes, 1) AS band_episodes, "
                "       t.romaji, t.english, t.cover_url "
                "FROM episodes e "
                "JOIN next_eps n ON n.anilist_id = e.anilist_id AND e.ep_number = n.next_ep "
                "JOIN titles t ON t.anilist_id = e.anilist_id "
                "LEFT JOIN band_counts bc ON bc.anilist_id = e.anilist_id "
                "WHERE e.comprehension_pct IS NOT NULL "
                "  AND e.comprehension_pct >= ? AND e.comprehension_pct < ? "
                "  AND COALESCE(e.watched,0)=0 "
                "ORDER BY e.comprehension_pct DESC, e.new_word_count ASC LIMIT ?",
                (lo, hi, lo, hi, int(limit)),
            ).fetchall()
    return [
        {
            "episode_id": r["episode_id"],
            "anilist_id": r["anilist_id"],
            "ep_number": r["ep_number"],
            "title": r["english"] or r["romaji"] or f"AniList {r['anilist_id']}",
            "cover_url": r["cover_url"],
            "comprehension_pct": r["comprehension_pct"],
            "comprehension_rating": r["comprehension_rating"],
            "comprehension_source": r["comprehension_source"],
            "new_word_count": r["new_word_count"],
            "watched": bool(r["watched"]),
            "has_video": bool(r["video_path"]),
            "band_episodes": r["band_episodes"],
        }
        for r in rows
    ]


# --------------------------------------------------------------------------
# Word leverage — "which words unlock the most content"
# --------------------------------------------------------------------------
# In a synthetic bottom-heavy comprehension distribution, many episodes sit
# below the target while relatively few occupy the sweet spot. For every unknown
# lemma we can compute how much closer each near-threshold episode gets if the
# user learns it — turning the corpus into a personalized curriculum. All data
# is already in line_lemmas × known_words × lemma_freq; this is just the join.

_LEVERAGE_CACHE_KEY = "leverage.cache"
_LEVERAGE_DIRTY_KEY = "leverage.dirty"
_LEVERAGE_TARGET = 80.0  # an episode "unlocks" when it crosses into the sweet spot


def _compute_word_leverage(lo: float = 60.0, hi: float = 80.0, top: int = 100) -> dict:
    """One pass over the near-threshold band. Linearized model:
    learning lemma w lifts episode e by occurrences(w,e)/total_tokens(e)*100 pts.
    Greedy selection then ranks words by episodes-crossed (cumulative)."""
    t0 = time.time()
    with connect() as cx:
        eps = {r["id"]: {"pct": r["comprehension_pct"], "total": 0}
               for r in cx.execute(
                   "SELECT id, comprehension_pct FROM episodes "
                   "WHERE comprehension_pct >= ? AND comprehension_pct < ? "
                   "AND COALESCE(watched,0)=0",
                   (lo, hi))}
        if not eps:
            return {"words": [], "band": [lo, hi], "episodes_in_band": 0}
        marks = ",".join("?" for _ in eps)
        ids = list(eps)
        for r in cx.execute(
                f"SELECT episode_id, COUNT(*) AS n FROM line_lemmas "
                f"WHERE episode_id IN ({marks}) GROUP BY episode_id", ids):
            eps[r["episode_id"]]["total"] = r["n"]
        # unknown-lemma occurrences per (lemma, episode). Junk POS excluded, and
        # the lemma must exist in the JPDB frequency list — that keeps real
        # vocabulary and drops character names / digits / OP-credit noise.
        non_vocab = tuple(_NON_VOCAB_POS)
        rows = cx.execute(
            f"SELECT ll.lemma AS lemma, ll.episode_id AS eid, COUNT(*) AS n, "
            f"       MAX(ll.reading) AS reading "
            f"FROM line_lemmas ll "
            f"WHERE ll.episode_id IN ({marks}) "
            f"AND ll.pos NOT IN ({','.join('?' for _ in non_vocab)}) "
            f"AND EXISTS (SELECT 1 FROM lemma_freq lf WHERE lf.lemma = ll.lemma "
            f"            AND lf.rank <= 30000) "
            f"AND NOT EXISTS (SELECT 1 FROM known_words kw WHERE kw.dict_form = ll.lemma "
            f"                AND kw.status IN ('KNOWN','IGNORED')) "
            f"{_srs_known_predicate('ll')} "
            f"GROUP BY ll.lemma, ll.episode_id",
            ids + list(non_vocab),
        ).fetchall()

    digit_re = re.compile(r"^[0-9０-９]+$")
    by_lemma: dict[str, dict] = {}
    for r in rows:
        if not _is_content(r["lemma"]) or digit_re.match(r["lemma"] or ""):
            continue
        d = by_lemma.setdefault(r["lemma"], {"eps": {}, "occ": 0, "reading": r["reading"]})
        d["eps"][r["eid"]] = r["n"]
        d["occ"] += r["n"]

    # greedy: repeatedly take the word that crosses the most episodes over the
    # target (ties: most occurrences), then apply its lift and continue.
    pct_now = {eid: e["pct"] for eid, e in eps.items()}
    totals = {eid: max(1, e["total"]) for eid, e in eps.items()}
    picked: list[dict] = []
    candidates = dict(by_lemma)
    unlocked_so_far = 0
    for _ in range(min(top, len(candidates))):
        best_lemma, best_cross, best_gain = None, -1, -1.0
        for lemma, d in candidates.items():
            cross = 0
            gain = 0.0
            for eid, n in d["eps"].items():
                lift = n / totals[eid] * 100.0
                p = pct_now[eid]
                if p < _LEVERAGE_TARGET <= p + lift:
                    cross += 1
                gain += min(lift, max(0.0, _LEVERAGE_TARGET - p))
            if (cross, gain, d["occ"]) > (best_cross, best_gain, -1):
                best_lemma, best_cross, best_gain = lemma, cross, gain
        if best_lemma is None:
            break
        d = candidates.pop(best_lemma)
        for eid, n in d["eps"].items():
            pct_now[eid] = pct_now[eid] + n / totals[eid] * 100.0
        unlocked_so_far += best_cross
        picked.append({
            "lemma": best_lemma,
            "reading": _to_hira(d["reading"]),
            "occurrences": d["occ"],
            "episodes": len(d["eps"]),
            "crossings": best_cross,
            "cumulative_unlocked": unlocked_so_far,
        })

    # decorate with gloss + frequency rank
    forms = [w["lemma"] for w in picked]
    glosses = _glosses_for(forms)
    with connect() as cx:
        freq = {r["lemma"]: r["rank"] for r in cx.execute(
            f"SELECT lemma, rank FROM lemma_freq WHERE lemma IN ({','.join('?' for _ in forms)})",
            forms)} if forms else {}
    for w in picked:
        w["gloss"] = glosses.get(w["lemma"])
        w["freq_rank"] = freq.get(w["lemma"])

    return {
        "words": picked,
        "band": [lo, hi],
        "target": _LEVERAGE_TARGET,
        "episodes_in_band": len(eps),
        "computed_in_s": round(time.time() - t0, 1),
        "computed_at": _dt.datetime.now().isoformat(timespec="seconds"),
    }


_LEVERAGE_DEPTH = 200  # compute deep once; the router slices to its `top`


def word_leverage(refresh: bool = False, top: int = 100) -> dict:
    """Cached leverage ranking; recomputed when the known-set changed
    (recompute_all sets leverage.dirty) or on demand. The cache always holds a
    deep list (_LEVERAGE_DEPTH); `top` just slices it, so top=3 and top=100
    share one computation."""
    out = None
    if not refresh and kv_get(_LEVERAGE_DIRTY_KEY) != "1":
        cached = kv_get(_LEVERAGE_CACHE_KEY)
        if cached:
            try:
                out = json.loads(cached)
            except Exception:
                out = None
    if out is None:
        out = _compute_word_leverage(top=_LEVERAGE_DEPTH)
        try:
            kv_set(_LEVERAGE_CACHE_KEY, json.dumps(out, ensure_ascii=False))
            kv_set(_LEVERAGE_DIRTY_KEY, "0")
        except Exception:
            pass
    return {**out, "words": out.get("words", [])[:top]}


# --------------------------------------------------------------------------
# Episode transcript (pre-watch reading view)
# --------------------------------------------------------------------------
def episode_transcript(episode_id: int) -> dict:
    """Full episode transcript with per-token known-status coloring.

    line_lemmas has no token-order column, so tokens are (re)tokenized on
    demand and joined against the live known-set.
    """
    with connect() as cx:
        lines = [dict(r) for r in cx.execute(
            "SELECT id, idx, start_ms, end_ms, text, text_furigana, translation "
            "FROM subtitle_lines WHERE episode_id=? ORDER BY idx",
            (episode_id,))]
        ep = cx.execute(
            "SELECT e.id, e.ep_number, e.anilist_id, e.video_path, e.comprehension_pct, "
            "t.romaji, t.english FROM episodes e JOIN titles t ON t.anilist_id=e.anilist_id "
            "WHERE e.id=?", (episode_id,)).fetchone()
    if not ep:
        raise ValueError(f"episode {episode_id} not found")
    if not lines:
        return {"episode_id": episode_id, "title": ep["romaji"] or ep["english"],
                "ep_number": ep["ep_number"], "lines": [], "source": None}

    texts = [ln["text"] for ln in lines]
    source = "migaku-local"
    from . import migaku_tok
    toks = migaku_tok.tokenize_lines(texts)
    if toks is None:
        source = "local"
        toks = [tokenize(t) for t in texts]

    known, ignored = _known_lookup()
    # words with an active SRS card are neither known nor plain unknown — the
    # transcript colours them LEARNING (sky blue), like Migaku does (§2.6.3).
    try:
        from ..srs.service import active_forms as _srs_active

        srs_active = _srs_active()
    except Exception as e:
        log.debug("srs active overlay unavailable: %s", e)
        srs_active = set()
    out_lines = []
    for ln, line_toks in zip(lines, toks):
        tokens = []
        for t in line_toks or []:
            lemma = t.get("dictForm") or t.get("lemma") or t.get("surface")
            surface = t.get("surface")
            if not _is_content(lemma, surface):
                continue
            reading = _to_hira(t.get("reading"))
            status = _token_status(lemma, surface, known, ignored, reading=reading)
            if status == "UNKNOWN" and lemma in srs_active:
                status = "LEARNING"
            tokens.append({
                "surface": surface,
                "dict_form": lemma,
                "reading": reading,
                "status": status,
            })
        out_lines.append({
            "line_id": ln["id"], "idx": ln["idx"],
            "start_ms": ln["start_ms"], "end_ms": ln["end_ms"],
            "text": ln["text"], "text_furigana": ln["text_furigana"],
            "translation": ln["translation"], "tokens": tokens,
        })
    return {
        "episode_id": episode_id,
        "title": ep["romaji"] or ep["english"],
        "ep_number": ep["ep_number"],
        "anilist_id": ep["anilist_id"],
        "has_video": bool(ep["video_path"]),
        "comprehension_pct": ep["comprehension_pct"],
        "source": source,
        "lines": out_lines,
    }


# --------------------------------------------------------------------------
# Stats time series (the dashboard)
# --------------------------------------------------------------------------
def learning_stats(days: int = 120) -> dict:
    """Known-words + comprehension history + watch activity for the Stats page."""
    since = f"-{max(7, int(days))} days"
    with connect() as cx:
        known = [dict(r) for r in cx.execute(
            "SELECT captured_at, known, learning, unknown, ignored FROM known_snapshots "
            "WHERE captured_at >= datetime('now', ?) ORDER BY captured_at", (since,))]
        compr = [dict(r) for r in cx.execute(
            "SELECT captured_at, scored_episodes, avg_pct, sweet_count, almost_count, "
            "easy_count, hard_count FROM comprehension_history "
            "WHERE captured_at >= datetime('now', ?) ORDER BY captured_at", (since,))]
        watched = cx.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(duration_ms),0) AS ms FROM episodes WHERE watched=1"
        ).fetchone()
        # the viewing timeline comes from watch_history (one row per completed
        # viewing — rewatches count as their own immersion, because they are)
        views = cx.execute(
            "SELECT COUNT(*) AS total, COALESCE(SUM(is_rewatch),0) AS rewatches, "
            "COALESCE(SUM(COALESCE(e.duration_ms, 24*60000)),0) AS ms "
            "FROM watch_history wh LEFT JOIN episodes e ON e.id = wh.episode_id"
        ).fetchone()
        watched_recent = [dict(r) for r in cx.execute(
            "SELECT date(wh.watched_at) AS day, COUNT(*) AS episodes, "
            "COALESCE(SUM(wh.is_rewatch),0) AS rewatches, "
            "COALESCE(SUM(COALESCE(e.duration_ms, 24*60000)),0)/60000 AS minutes "
            "FROM watch_history wh LEFT JOIN episodes e ON e.id = wh.episode_id "
            "WHERE wh.watched_at >= datetime('now', ?) "
            "GROUP BY day ORDER BY day", (since,))]
        in_progress = cx.execute(
            "SELECT COUNT(*) AS n FROM episodes WHERE COALESCE(watched,0)=0 "
            "AND COALESCE(watch_progress_ms,0) > 30000"
        ).fetchone()["n"]
        band_now = cx.execute(
            "SELECT COUNT(*) scored, ROUND(AVG(comprehension_pct),1) avg_pct, "
            "SUM(comprehension_pct >= 80 AND comprehension_pct < 95) sweet, "
            "SUM(comprehension_pct >= 65 AND comprehension_pct < 80) almost, "
            "SUM(comprehension_pct >= 95) easy, SUM(comprehension_pct < 65) hard "
            "FROM episodes WHERE comprehension_pct IS NOT NULL"
        ).fetchone()
    return {
        "known_series": known,
        "comprehension_series": compr,
        "watched_total": watched["n"],                      # unique episodes
        "total_views": views["total"],                       # incl. rewatches
        "rewatches": views["rewatches"],
        "watched_minutes_total": int((views["ms"] or 0) / 60000),
        "watched_by_day": watched_recent,
        "in_progress": in_progress,
        "now": dict(band_now) if band_now else {},
    }


def register_jobs() -> None:
    try:
        from ..jobs.service import register
        register("comprehension", _job_comprehension)
        register("comprehension_exact", _job_comprehension_exact)
        register("migaku_drift_check", _job_migaku_drift)
        register("comprehension_recompute_all", _job_recompute_all)
    except Exception as e:  # pragma: no cover
        log.warning("could not register learn comprehension handlers: %s", e)


# register at import time so the worker can dispatch comprehension jobs
register_jobs()
