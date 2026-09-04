"""Census — candidate extraction, scoring and moment ranking (§5.2–5.4).

Port of `tools/srs_initial_candidates.py` with the additions the design asks
for: line classification (junk / lyric / shared translation) before anything is
counted, the kana↔kanji variant merge, the eleven word filters F1–F11 persisted
as `judge_status='filtered'` (so a rule fix re-admits the word at the next
census) and the moment ranking of §5.4.

Mechanical work here only WIDENS and ORDERS the pool — no card is ever created
by a filter. The judgment gate is `judge.py` (amendments §A).
"""
from __future__ import annotations

import json
import logging
import math
import re
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from ..models import SrsSettings
from .constants import (
    BASIC_RANK,
    FRAGMENT_MIN_STANDALONE,
    LYRIC_PAIR_MIN_CHARS,
    LYRIC_REPEATS,
    MAX_FREQ_RANK,
    MAX_OTHER_UNKNOWNS,
    MOMENTS_PER_WORD,
    NAME_MAX_TITLES,
    REJUDGE_NEW_LINES,
)
from .snapshot import dialogue_neighbours, norm_text, strip_bidi  # noqa: F401  (§5.7: one owner)

log = logging.getLogger("mimi_lab.srs.census")

# ASS vector drawings ("m 12 34 b …") leak into subtitle tracks as text lines.
JUNK_DRAWING_RE = re.compile(r"^m\s*[\d.]+\s+[\d.]+\s+[bl]\s")

# Simplified-Chinese-only characters (§5.2). NOTE: the set as written in the
# design contains 来/会/里/没, which are perfectly normal Japanese kanji, so the
# test only fires on lines with NO kana at all — a Chinese cue never has kana,
# a Japanese cue essentially always does.
SIMPLIFIED_CN = set("这们坠个从吗没让说时间为什么来会样这里们")

_JA_RE = re.compile(r"[぀-ヿ一-鿿]")
_KANA_RE = re.compile(r"[぀-ヿ]")
KANJI_RE = re.compile(r"[一-鿿㐀-䶿]")

# Digits (ASCII, full-width and kanji numerals) are never vocabulary.
DIGIT_RE = re.compile(r"^[0-9０-９〇一二三四五六七八九十百千万]+$")

# A capitalised word in the English cue with no lowercase gloss word present is
# a decent "this is a name" hint for the judge (§5.2 F7).
_CAPS_RE = re.compile(r"\b[A-Z][a-z]+")

# POS that are function words / never vocabulary on their own (§5.2 F4).
_FUNC_POS_RANKED = {"接続詞", "接頭辞", "接尾辞", "連体詞"}

# `census.neighbours(line)` in the design text == snapshot.dialogue_neighbours.
neighbours = dialogue_neighbours

# The in-memory corpus is expensive to build (~50k lines + a fugashi pass) and
# every `rank_moments` call during one planning transaction wants the same one.
_CORPUS_CACHE: dict[str, Any] = {"mark": None, "corpus": None, "at": 0.0, "db": None}
# The corpus is ~450 MB for the full library, so it is cached only long enough
# for one planning run (many `rank_moments` calls) and dropped afterwards.
_CORPUS_TTL_S = 180.0


@dataclass
class MomentCandidate:
    """One ranked line for a word (§5.4); `moment_id` is the upserted
    `srs_moments.id`."""
    moment_id: Optional[int] = None
    lemma: str = ""
    line_id: Optional[int] = None
    episode_id: int = 0
    idx: Optional[int] = None
    start_ms: int = 0
    end_ms: int = 0
    text: str = ""
    norm_text: str = ""
    translation: Optional[str] = None
    translation_source: Optional[str] = None
    translation_shared: bool = False
    target_surface: Optional[str] = None
    other_unknowns: int = 0
    line_score: float = 0.0


@dataclass
class CensusResult:
    """What one census run produced (§5.2)."""
    candidates: list[dict] = field(default_factory=list)   # scored, `score DESC`
    words_scored: int = 0
    words_filtered: int = 0
    lines_scanned: int = 0
    episodes: int = 0
    census_at: Optional[str] = None


def is_junk_line(text: str) -> bool:
    """True for lines that are not Japanese dialogue: ASS drawings, signs with no
    kana/kanji, Chinese cues, mostly-Latin cues (§5.2).

    Junk lines never contribute occurrences, never become moments and are skipped
    when picking prev/next neighbours (context, clip window, rolled-cue check).
    """
    t = strip_bidi(text or "")
    if not t.strip():
        return True
    if not _JA_RE.search(t):
        return True
    if not _KANA_RE.search(t) and any(c in SIMPLIFIED_CN for c in t):
        return True
    if JUNK_DRAWING_RE.match(t) is not None:
        return True
    latin = sum(1 for c in t if c.isascii() and c.isalpha())
    return latin / max(1, len(t)) > 0.5


def corpus_mark(cx) -> str:
    """JSON corpus mark (§5.9) over episodes whose video is on disk:
    `{"eps", "max_ep", "sub_ver", "max_line"}`. Any component change re-arms the
    census; compared against kv `srs.corpus.mark`."""
    have = "COALESCE(e.video_path,'') <> ''"
    r = cx.execute(
        f"SELECT COUNT(*) AS eps, COALESCE(MAX(e.id),0) AS max_ep FROM episodes e WHERE {have}"
    ).fetchone()
    sub_ver = cx.execute(
        "SELECT COALESCE(SUM(s.version),0) AS v FROM subtitles s "
        f"JOIN episodes e ON e.id = s.episode_id WHERE {have}"
    ).fetchone()["v"]
    max_line = cx.execute(
        "SELECT COALESCE(MAX(l.id),0) AS v FROM subtitle_lines l "
        f"JOIN episodes e ON e.id = l.episode_id WHERE {have}"
    ).fetchone()["v"]
    return json.dumps(
        {"eps": r["eps"], "max_ep": r["max_ep"], "sub_ver": sub_ver, "max_line": max_line},
        sort_keys=True,
    )


# ---------------------------------------------------------------------------
# learn.service helpers (imported lazily: app.learn is a heavy module and the
# import graph must stay one-directional)
# ---------------------------------------------------------------------------

def _learn():
    from ..learn import service as _ls
    return _ls


def _first_sense(gloss: Optional[str]) -> str:
    """First JMdict sense of a gloss line ("to smash; to crush" → "to smash")."""
    if not gloss:
        return ""
    return gloss.split(";")[0].strip().lower()


def rank_benefit(rank: Optional[int]) -> float:
    """§5.3 — JPDB rank benefit; an unranked word is mildly penalised."""
    if rank is None:
        return -2.0
    if rank <= 3000:
        return 4.0
    if rank <= 6000:
        return 3.0
    if rank <= 12000:
        return 2.0
    if rank <= 20000:
        return 1.0
    return 0.0


def word_score(word: dict) -> float:
    """§5.3 — leverage, occurrences, episodes, rank benefit, next-watch hits,
    Migaku LEARNING bonus, unwatched bonus. `word` uses `srs_words` field names."""
    occ = int(word.get("occ") or 0)
    eps = int(word.get("eps") or 0)
    lev = int(word.get("leverage_crossings") or 0)
    nwh = int(word.get("next_watch_hits") or 0)
    s = (
        3.0 * min(lev, 5)
        + 2.0 * math.log1p(occ)
        + 1.5 * min(eps, 8)
        + rank_benefit(word.get("freq_rank"))
        + 2.0 * min(nwh, 3) / 3.0
        + 1.5 * (1.0 if (word.get("migaku_status") or "") == "LEARNING" else 0.0)
        + 0.5 * (1.0 if word.get("in_unwatched") else 0.0)
    )
    return round(s, 4)


# ---------------------------------------------------------------------------
# The in-memory corpus
# ---------------------------------------------------------------------------

class _Corpus:
    """Everything the census reads, loaded once (§5.2 "Read everything once")."""

    def __init__(self, cx, cfg: SrsSettings) -> None:
        ls = _learn()
        self.cfg = cfg
        self.cx = cx
        self.mark = corpus_mark(cx)

        # --- known / ignored / Migaku rows -------------------------------
        known, ignored = ls._known_lookup()
        self.known = set(known)
        self.ignored = set(ignored)
        self.kw_rows: dict[str, str] = {}
        self.known_readings: set[str] = set()
        for r in cx.execute("SELECT dict_form, reading, status FROM known_words"):
            form = r["dict_form"]
            if not form:
                continue
            st = (r["status"] or "").upper()
            self.kw_rows.setdefault(form, st)
            if st == "KNOWN":
                if r["reading"]:
                    self.known_readings.add(ls._to_hira(r["reading"]))
                if not KANJI_RE.search(form):
                    self.known_readings.add(ls._to_hira(form))
        self.learning_migaku = {f for f, s in self.kw_rows.items() if s == "LEARNING"}

        # words already being drilled count as known FOR THE CENSUS ONLY (§5.2)
        active: set[str] = set()
        try:
            from . import service as _svc
            active = set(_svc.active_forms()) | set(_svc.known_forms())
        except Exception as e:                      # WP-A may not be live yet
            log.debug("srs.service known/active forms unavailable: %s", e)
        self.assumed_known = self.known | active

        # --- episodes with a video on disk --------------------------------
        self.eps: dict[int, dict] = {}
        for r in cx.execute(
            "SELECT e.id, e.anilist_id, e.ep_number, e.video_path, e.duration_ms, "
            "       COALESCE(e.watched,0) AS watched, t.romaji, t.english "
            "FROM episodes e JOIN titles t ON t.anilist_id = e.anilist_id "
            "WHERE COALESCE(e.video_path,'') <> ''"
        ):
            if not Path(r["video_path"]).exists():
                continue
            d = dict(r)
            d["show"] = d.get("romaji") or d.get("english") or ""
            self.eps[d["id"]] = d
        ep_ids = list(self.eps)

        # --- relink episodes with dangling line ids (§2.7) -----------------
        self._relink(ep_ids)

        # --- lines + tokens ------------------------------------------------
        self.lines: dict[int, dict] = {}
        self.by_ep: dict[int, list[int]] = defaultdict(list)
        for chunk in _chunks(ep_ids, 400):
            q = ",".join("?" * len(chunk))
            for r in cx.execute(
                f"SELECT id, episode_id, idx, start_ms, end_ms, text, translation "
                f"FROM subtitle_lines WHERE episode_id IN ({q})", chunk
            ):
                text = strip_bidi(r["text"] or "")
                d = {
                    "line_id": r["id"], "id": r["id"], "episode_id": r["episode_id"],
                    "idx": r["idx"], "start_ms": r["start_ms"] or 0, "end_ms": r["end_ms"] or 0,
                    "text": text, "translation": r["translation"],
                    "translation_source": "human" if r["translation"] else None,
                    "tokens": [], "norm": norm_text(text),
                }
                d["junk"] = is_junk_line(text)
                self.lines[d["line_id"]] = d
                self.by_ep[d["episode_id"]].append(d["line_id"])
        for chunk in _chunks(ep_ids, 400):
            q = ",".join("?" * len(chunk))
            for r in cx.execute(
                f"SELECT line_id, lemma, reading, surface FROM line_lemmas "
                f"WHERE episode_id IN ({q}) ORDER BY rowid", chunk
            ):
                ln = self.lines.get(r["line_id"])
                if ln is not None:
                    ln["tokens"].append((r["lemma"], r["reading"], r["surface"]))
        for ep_id, ids in self.by_ep.items():
            ids.sort(key=lambda i: (self.lines[i]["start_ms"], self.lines[i]["idx"] or 0, i))

        # --- repeats / lyrics / shared translations ------------------------
        repeats: Counter = Counter()
        norm_eps: dict[str, set[int]] = defaultdict(set)
        for ln in self.lines.values():
            if not ln["norm"]:
                continue
            repeats[ln["norm"]] += 1
            norm_eps[ln["norm"]].add(ln["episode_id"])
        for ln in self.lines.values():
            n = ln["norm"]
            rep = repeats.get(n, 0)
            ln["repeats"] = rep
            ln["lyric"] = bool(
                rep >= LYRIC_REPEATS
                or (rep == 2 and len(n) >= LYRIC_PAIR_MIN_CHARS and len(norm_eps.get(n, ())) >= 2)
            )
        self._dialogue_by_ep: dict[int, list[int]] = {}
        for ep_id, ids in self.by_ep.items():
            dialogue = [i for i in ids if not self.lines[i]["junk"]]
            self._dialogue_by_ep[ep_id] = dialogue
            for pos, lid in enumerate(dialogue):
                ln = self.lines[lid]
                tr = ln["translation"]
                shared = False
                if tr:
                    for j in (pos - 1, pos + 1):
                        if 0 <= j < len(dialogue) and self.lines[dialogue[j]]["translation"] == tr:
                            shared = True
                            break
                ln["translation_shared"] = shared
                ln["_dpos"] = pos

        # --- fugashi POS pass ---------------------------------------------
        tagger = ls._tagger()
        pos1_by_form: dict[str, Counter] = defaultdict(Counter)
        pos2_by_form: dict[str, Counter] = defaultdict(Counter)
        for ln in self.lines.values():
            if ln["junk"]:
                continue
            try:
                words_ = list(tagger(ln["text"]))
            except Exception:
                continue
            for w in words_:
                p1 = ls._feat(w, "pos1") or ""
                p2 = ls._feat(w, "pos2") or ""
                for key in {ls._lemma_of(w), w.surface}:
                    if key:
                        pos1_by_form[key][p1] += 1
                        if p2:
                            pos2_by_form[key][p2] += 1
        self._pos1 = pos1_by_form
        self._pos2 = pos2_by_form
        self._tagger = tagger

        # --- unknown analysis per clean line -------------------------------
        self.words: dict[str, dict] = {}
        for ln in self.lines.values():
            ln["unknown"] = []
            if ln["junk"] or ln["lyric"]:
                continue
            unk: list[tuple[str, str, str, bool]] = []
            seen: set[str] = set()
            for lemma, reading, surface in ln["tokens"]:
                form = lemma or surface
                if not form or form in seen:
                    continue
                if not ls._is_content(lemma, surface) or DIGIT_RE.match(form):
                    continue
                if ls._token_status(lemma, surface, self.assumed_known, self.ignored,
                                    reading=reading) != "UNKNOWN":
                    continue
                p1, _p2 = self.pos_of(form)
                if p1 and p1 in ls._NON_VOCAB_POS:
                    continue
                standalone = True
                if len(form) == 1 and KANJI_RE.match(form):
                    standalone = not _glued_to_kanji(ln["text"], surface or form)
                seen.add(form)
                unk.append((form, reading or "", surface or "", standalone))
            ln["unknown"] = unk
            for form, reading, surface, standalone in unk:
                w = self.words.setdefault(form, _blank_word(form))
                if standalone:
                    w["occ"] += 1
                    w["eps"].add(ln["episode_id"])
                    w["lines"].append(ln["line_id"])
                    w["titles"][self.eps[ln["episode_id"]]["anilist_id"]] += 1
                    w["standalone"] += 1
                    if not self.eps[ln["episode_id"]]["watched"]:
                        w["in_unwatched"] = True
                else:
                    w["glued"] += 1
                    w["compounds"][_compound_of(ln["text"], surface or form)] += 1
                w["all_lines"].append(ln["line_id"])
                if reading:
                    w["readings"][ls._to_hira(reading)] += 1
                if surface:
                    w["surfaces"][surface] += 1

        # --- decorations: rank, gloss, leverage, next-watch -----------------
        forms = list(self.words)
        self.ranks: dict[str, int] = {}
        for chunk in _chunks(forms, 800):
            q = ",".join("?" * len(chunk))
            for r in cx.execute(f"SELECT lemma, rank FROM lemma_freq WHERE lemma IN ({q})", chunk):
                self.ranks[r["lemma"]] = r["rank"]
        try:
            self.glosses = ls._glosses_for(forms)
        except Exception as e:
            log.warning("gloss lookup failed: %s", e)
            self.glosses = {}
        self.leverage: dict[str, int] = {}
        try:
            row = cx.execute("SELECT value FROM kv WHERE key='leverage.cache'").fetchone()
            if row and row["value"]:
                for w in (json.loads(row["value"]) or {}).get("words", []):
                    self.leverage[w.get("lemma")] = int(w.get("crossings") or 0)
        except Exception as e:
            log.debug("leverage cache unreadable: %s", e)
        self.next_watch_eps: set[int] = set()
        try:
            for e in ls.sweet_spot(80.0, 95.0, limit=3)[:3]:
                eid = e.get("episode_id") if isinstance(e, dict) else None
                if eid:
                    self.next_watch_eps.add(int(eid))
        except Exception as e:
            log.debug("sweet_spot unavailable: %s", e)

        # --- previous verdicts / blocked moments ---------------------------
        self.blocked_moments: set[tuple[str, int, str]] = set()
        for r in cx.execute(
            "SELECT lemma, episode_id, norm_text FROM srs_moments "
            "WHERE verdict IN ('reject','user_rejected')"
        ):
            self.blocked_moments.add((r["lemma"], r["episode_id"], r["norm_text"]))
        self.card_primaries: dict[tuple[int, str], str] = {}
        self.card_lemmas: set[str] = set()
        for r in cx.execute("SELECT lemma, episode_id, norm_text FROM srs_cards"):
            self.card_lemmas.add(r["lemma"])
            if r["episode_id"] is not None:
                self.card_primaries[(r["episode_id"], r["norm_text"] or "")] = r["lemma"]
        self.prev_words: dict[str, dict] = {}
        for r in cx.execute(
            "SELECT lemma, judge_status, judge_reason, user_flag, canonical_of, "
            "       judged_moment_lines FROM srs_words"
        ):
            self.prev_words[r["lemma"]] = dict(r)

    # -- helpers -----------------------------------------------------------

    def _relink(self, ep_ids: list[int]) -> None:
        """`service.relink_episode` for episodes with dangling line ids (§2.7)."""
        try:
            from . import service as _svc
            rows = self.cx.execute(
                "SELECT DISTINCT episode_id FROM ("
                "  SELECT episode_id FROM srs_cards WHERE line_id IS NULL AND episode_id IS NOT NULL"
                "  UNION SELECT episode_id FROM srs_moments WHERE line_id IS NULL"
                ")"
            ).fetchall()
            for r in rows:
                if r["episode_id"] in self.eps:
                    _svc.relink_episode(self.cx, r["episode_id"])
        except NotImplementedError:
            log.debug("relink_episode not implemented yet (WP-A)")
        except Exception as e:
            log.warning("relink pass failed: %s", e)

    def pos_of(self, form: str) -> tuple[str, str]:
        p1 = self._pos1.get(form)
        p2 = self._pos2.get(form)
        return (p1.most_common(1)[0][0] if p1 else "",
                p2.most_common(1)[0][0] if p2 else "")

    def grammar_combo(self, form: str) -> bool:
        """UniDic splits the form into ≥2 tokens one of which is a particle or
        auxiliary (かも = か+も, んで, とも) — never vocabulary (§5.2 F4)."""
        ls = _learn()
        try:
            toks = list(self._tagger(form))
        except Exception:
            return False
        if len(toks) <= 1:
            return False
        return any((ls._feat(t, "pos1") or "") in ls._NON_VOCAB_POS for t in toks)

    def dialogue_neighbours_mem(self, line: dict, n: int = 1) -> tuple[list[dict], list[dict]]:
        """In-memory equivalent of `snapshot.dialogue_neighbours` (same rule:
        time-adjacent, junk skipped, span-containing signs skipped)."""
        ids = self._dialogue_by_ep.get(line["episode_id"], [])
        pos = line.get("_dpos")
        if pos is None:
            return [], []
        before: list[dict] = []
        for j in range(pos - 1, -1, -1):
            o = self.lines[ids[j]]
            if o["start_ms"] <= line["start_ms"] and o["end_ms"] >= line["end_ms"]:
                continue
            before.append(o)
            if len(before) >= n:
                break
        before.reverse()
        after: list[dict] = []
        for j in range(pos + 1, len(ids)):
            o = self.lines[ids[j]]
            if o["start_ms"] <= line["start_ms"] and o["end_ms"] >= line["end_ms"]:
                continue
            after.append(o)
            if len(after) >= n:
                break
        return before, after

    # -- moment machinery --------------------------------------------------

    def sane(self, lemma: str, line: dict) -> Optional[tuple[int, str]]:
        """`(other_unknowns, target_surface)` when the line is a usable moment for
        `lemma` (§5.4 hard rules), else None."""
        if line["junk"] or line["lyric"]:
            return None
        if line["episode_id"] not in self.eps:
            return None
        hit = next((u for u in line["unknown"] if u[0] == lemma), None)
        if hit is None or not hit[3]:                   # absent or glued (F2)
            return None
        surface = hit[2] or lemma
        if surface not in line["text"]:
            return None
        others = len([u for u in line["unknown"] if u[0] != lemma])
        if others > MAX_OTHER_UNKNOWNS:
            return None
        dur = line["end_ms"] - line["start_ms"]
        if dur < 800 or dur > 9000:
            return None
        n_tok = len(line["tokens"])
        if n_tok < 3 or n_tok > 22:
            return None
        if (lemma, line["episode_id"], line["norm"]) in self.blocked_moments:
            return None
        owner = self.card_primaries.get((line["episode_id"], line["norm"]))
        if owner is not None and owner != lemma:
            return None
        if self.cfg.moment_source == "watched_only" and not self.eps[line["episode_id"]]["watched"]:
            return None
        # rolled / duplicated cue: the next dialogue cue repeats this text
        _b, after = self.dialogue_neighbours_mem(line, 1)
        if after:
            a = after[0]["norm"]
            b = line["norm"]
            if a and b and (a.startswith(b) or b.startswith(a) or a in b or b in a):
                return None
        return others, surface

    def moment_score(self, lemma: str, line: dict, others: int) -> float:
        """§5.4 heuristic — deterministic, only picks and orders."""
        s = 10.0 - 3.0 * others
        tr = line["translation"]
        src = line.get("translation_source")
        if tr is None:
            s -= 4.0
        elif src == "mt":
            s += 1.0
        elif line.get("translation_shared"):
            s += 0.0
        else:
            s += 3.0
        dur = line["end_ms"] - line["start_ms"]
        if 1500 <= dur <= 6000:
            s += 2.0
        elif dur < 900 or dur > 9000:
            s -= 4.0
        n_tok = len(line["tokens"])
        if 5 <= n_tok <= 14:
            s += 2.0
        elif n_tok < 3 or n_tok > 22:
            s -= 4.0
        if self.eps[line["episode_id"]]["watched"]:
            s += 1.5
        if line.get("repeats") == 2:
            s -= 2.0
        _b, after = self.dialogue_neighbours_mem(line, 1)
        from .snapshot import continuation
        if continuation(line, after[0] if after else None):
            s -= 1.5
        hit = next((u for u in line["unknown"] if u[0] == lemma), None)
        if hit is not None:
            surf = hit[2] or lemma
            if line["text"].rstrip().endswith(surf) and len(line["text"]) <= 8:
                s -= 1.5
        return round(s, 3)

    def moment_lines(self, lemma: str) -> list[tuple[float, dict, int, str]]:
        """Every sane line of the word, scored, best first."""
        w = self.words.get(lemma)
        if not w:
            return []
        out: list[tuple[float, dict, int, str]] = []
        for lid in dict.fromkeys(w["lines"]):
            line = self.lines.get(lid)
            if line is None:
                continue
            ok = self.sane(lemma, line)
            if ok is None:
                continue
            others, surface = ok
            out.append((self.moment_score(lemma, line, others), line, others, surface))
        out.sort(key=lambda t: (-t[0], t[1]["line_id"]))
        return out

    def pick_moments(self, lemma: str, limit: int = MOMENTS_PER_WORD) -> list[tuple[float, dict, int, str]]:
        """Top `limit` scored lines, distinct texts, preferring distinct episodes
        (a second line from the same episode only when no other episode's line is
        within 1.0 of it) — §5.4."""
        pool = self.moment_lines(lemma)
        picked: list[tuple[float, dict, int, str]] = []
        used_eps: set[int] = set()
        seen_text: set[str] = set()
        for i, cand in enumerate(pool):
            score, line, _o, _s = cand
            if line["norm"] in seen_text:
                continue
            if line["episode_id"] in used_eps:
                better_elsewhere = any(
                    o[1]["episode_id"] not in used_eps
                    and o[1]["norm"] not in seen_text
                    and o[0] >= score - 1.0
                    for o in pool[i + 1:]
                )
                if better_elsewhere:
                    continue
            picked.append(cand)
            used_eps.add(line["episode_id"])
            seen_text.add(line["norm"])
            if len(picked) >= limit:
                break
        return picked


def _blank_word(form: str) -> dict:
    return {
        "lemma": form, "occ": 0, "eps": set(), "lines": [], "all_lines": [],
        "titles": Counter(), "readings": Counter(), "surfaces": Counter(),
        "standalone": 0, "glued": 0, "compounds": Counter(), "in_unwatched": False,
    }


def _chunks(seq: list, n: int):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _glued_to_kanji(text: str, surface: str) -> bool:
    """True when every occurrence of a single-kanji surface sits next to another
    kanji (教 inside 魔女教) — a compound fragment, not a standalone word."""
    if not surface or surface not in text:
        return False
    glued = 0
    total = 0
    start = 0
    while True:
        i = text.find(surface, start)
        if i < 0:
            break
        total += 1
        before = text[i - 1] if i > 0 else ""
        after = text[i + len(surface)] if i + len(surface) < len(text) else ""
        if (before and KANJI_RE.match(before)) or (after and KANJI_RE.match(after)):
            glued += 1
        start = i + len(surface)
    return total > 0 and glued == total


def _compound_of(text: str, surface: str, span: int = 3) -> str:
    """The kanji run a glued single-kanji surface sits in (for the judge hint)."""
    i = text.find(surface)
    if i < 0:
        return ""
    lo = i
    while lo > 0 and KANJI_RE.match(text[lo - 1]):
        lo -= 1
    hi = i + len(surface)
    while hi < len(text) and KANJI_RE.match(text[hi]):
        hi += 1
    return text[max(lo, hi - 8):hi][-8:]


def _corpus(cfg: SrsSettings, cx, *, fresh: bool = False) -> _Corpus:
    """Build (or reuse) the in-memory corpus. The cache is keyed by the corpus
    mark (and the DB path, so a selftest never sees a live-DB corpus), so a
    download or re-ingest invalidates it. `cx` is borrowed, never closed here."""
    from ..config import settings as _settings

    db_key = str(_settings.mimi_lab_db)
    mark = corpus_mark(cx)
    cached = _CORPUS_CACHE.get("corpus")
    if (not fresh and cached is not None and _CORPUS_CACHE.get("mark") == mark
            and _CORPUS_CACHE.get("db") == db_key
            and (time.time() - float(_CORPUS_CACHE.get("at") or 0)) < _CORPUS_TTL_S
            and cached.cfg.moment_source == cfg.moment_source):
        cached.cx = cx
        return cached
    c = _Corpus(cx, cfg)
    _CORPUS_CACHE.update({"mark": mark, "corpus": c, "at": time.time(), "db": db_key})
    return c


def invalidate_cache() -> None:
    """Drop the cached corpus (selftests, after a re-ingest)."""
    _CORPUS_CACHE.update({"mark": None, "corpus": None, "at": 0.0, "db": None})


# ---------------------------------------------------------------------------
# Filters F1–F11 (§5.2)
# ---------------------------------------------------------------------------

_UPSERT_WORD = """
INSERT INTO srs_words (lemma, reading, gloss, pos1, pos2, freq_rank, occ, eps, titles,
    title_concentration, iplus1_lines, moment_lines, leverage_crossings, next_watch_hits,
    migaku_status, in_unwatched, standalone_ratio, canonical_of, score, census_at, updated_at)
VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'),datetime('now'))
ON CONFLICT(lemma) DO UPDATE SET
    reading=excluded.reading, gloss=excluded.gloss, pos1=excluded.pos1, pos2=excluded.pos2,
    freq_rank=excluded.freq_rank, occ=excluded.occ, eps=excluded.eps, titles=excluded.titles,
    title_concentration=excluded.title_concentration, iplus1_lines=excluded.iplus1_lines,
    moment_lines=excluded.moment_lines, leverage_crossings=excluded.leverage_crossings,
    next_watch_hits=excluded.next_watch_hits, migaku_status=excluded.migaku_status,
    in_unwatched=excluded.in_unwatched, standalone_ratio=excluded.standalone_ratio,
    canonical_of=excluded.canonical_of, score=excluded.score, census_at=excluded.census_at,
    updated_at=datetime('now')
"""


def _variant_merge(c: _Corpus) -> dict[str, str]:
    """Fold a kana-only unknown form into its kanji spelling when the reading AND
    the first JMdict sense agree (§5.2). Returns `{kana_form: kanji_form}`."""
    ls = _learn()
    by_reading: dict[str, list[str]] = defaultdict(list)
    for form, w in c.words.items():
        if not KANJI_RE.search(form):
            continue
        reading = w["readings"].most_common(1)[0][0] if w["readings"] else ""
        if reading:
            by_reading[reading].append(form)
    merged: dict[str, str] = {}
    for form, w in list(c.words.items()):
        if KANJI_RE.search(form):
            continue
        hira = ls._to_hira(form)
        sense = _first_sense(c.glosses.get(form))
        if not sense:
            continue
        for kanji in by_reading.get(hira, []):
            if kanji == form:
                continue
            if _first_sense(c.glosses.get(kanji)) != sense:
                continue
            k = c.words[kanji]
            k["occ"] += w["occ"]
            k["eps"] |= w["eps"]
            k["lines"].extend(w["lines"])
            k["all_lines"].extend(w["all_lines"])
            k["titles"].update(w["titles"])
            k["standalone"] += w["standalone"]
            k["in_unwatched"] = k["in_unwatched"] or w["in_unwatched"]
            merged[form] = kanji
            break
    return merged


def run(cfg: SrsSettings, *, fresh: bool = True) -> CensusResult:
    """Full census (§5.2): relink, classify lines, variant merge, filters F1–F11,
    upsert `srs_words`; returns the scored candidates (`score DESC`)."""
    from ..db import connect, cursor, kv_set

    t0 = time.time()
    cx = connect()
    try:
        c = _corpus(cfg, cx, fresh=fresh)
        merged = _variant_merge(c)

        rows: list[tuple] = []                 # feature upserts
        verdicts: list[tuple[str, str, Optional[str]]] = []   # (lemma, judge_status, reason)
        candidates: list[dict] = []
        filtered = 0

        for form, w in c.words.items():
            p1, p2 = c.pos_of(form)
            rank = c.ranks.get(form)
            gloss = c.glosses.get(form)
            occ = w["occ"]
            eps = len(w["eps"])
            titles = len(w["titles"])
            conc = (max(w["titles"].values()) / occ) if occ and w["titles"] else None
            standalone_ratio = (w["standalone"] / (w["standalone"] + w["glued"])
                                if (w["standalone"] + w["glued"]) else None)
            reading = w["readings"].most_common(1)[0][0] if w["readings"] else None
            migaku = c.kw_rows.get(form)
            nwh = sum(1 for lid in w["lines"]
                      if c.lines[lid]["episode_id"] in c.next_watch_eps)
            iplus1 = sum(1 for lid in dict.fromkeys(w["lines"])
                         if len([u for u in c.lines[lid]["unknown"] if u[0] != form]) == 0)
            n_moments = len(c.moment_lines(form))

            feat = {
                "occ": occ, "eps": eps, "freq_rank": rank,
                "leverage_crossings": c.leverage.get(form, 0),
                "next_watch_hits": nwh, "migaku_status": migaku,
                "in_unwatched": w["in_unwatched"],
            }
            score = word_score(feat)
            rows.append((
                form, reading, gloss, p1 or None, p2 or None, rank, occ, eps, titles,
                conc, iplus1, n_moments, c.leverage.get(form, 0), nwh, migaku,
                1 if w["in_unwatched"] else 0, standalone_ratio, merged.get(form), score,
            ))

            prev = c.prev_words.get(form, {})
            prev_status = (prev.get("judge_status") or "unjudged")
            user_flag = prev.get("user_flag")

            # --- variant merge: never proposed on its own -----------------
            if form in merged:
                verdicts.append((form, "filtered", f"variant_of:{merged[form]}"))
                filtered += 1
                continue

            reason = _filter_reason(c, form, w, p1, p2, rank, gloss, occ, eps, titles,
                                    conc, standalone_ratio, n_moments)
            if reason is not None:
                status, why = reason
                if user_flag in (None, "unskip") and prev_status not in ("accepted", "rejected"):
                    verdicts.append((form, status, why))
                filtered += 1
                continue

            # --- F3 state -------------------------------------------------
            if user_flag == "skip":
                verdicts.append((form, "filtered", "user_skip"))
                continue
            if form in c.card_lemmas or prev_status in ("accepted", "pending"):
                continue
            if prev_status == "probably_known":
                continue
            if prev_status == "rejected":
                if not _rejudge_eligible(prev, n_moments):
                    continue
                verdicts.append((form, "unjudged", None))
            elif prev_status in ("unjudged", "filtered", "error"):
                verdicts.append((form, "unjudged", None))

            candidates.append({
                "lemma": form, "reading": reading, "gloss": gloss, "pos1": p1 or None,
                "pos2": p2 or None, "freq_rank": rank, "score": score, "occ": occ,
                "eps": eps, "titles": titles, "iplus1_lines": iplus1,
                "moment_lines": n_moments,
                "leverage_crossings": c.leverage.get(form, 0), "next_watch_hits": nwh,
                "migaku_status": migaku, "in_unwatched": w["in_unwatched"],
                "standalone_ratio": standalone_ratio,
                "hints": _hints(c, form, w, p1, p2, rank, gloss, conc, titles),
            })

        candidates.sort(key=lambda d: (-d["score"], d["freq_rank"] or 10 ** 9, d["lemma"]))

        census_at = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
        with cursor() as wx:
            for r in rows:
                wx.execute(_UPSERT_WORD, r)
            for lemma, status, why in verdicts:
                if status == "unjudged":
                    wx.execute(
                        "UPDATE srs_words SET judge_status='unjudged', judge_reason=NULL, "
                        "updated_at=datetime('now') WHERE lemma=? AND judge_status IN "
                        "('filtered','error','unjudged','rejected')", (lemma,))
                else:
                    wx.execute(
                        "UPDATE srs_words SET judge_status=?, judge_reason=?, "
                        "updated_at=datetime('now') WHERE lemma=? AND judge_status NOT IN "
                        "('accepted','rejected','pending')", (status, why, lemma))
        kv_set("srs.candidates.at", census_at)
        log.info("census: %d words scored, %d filtered, %d candidates in %.1fs",
                 len(rows), filtered, len(candidates), time.time() - t0)
        return CensusResult(
            candidates=candidates, words_scored=len(rows), words_filtered=filtered,
            lines_scanned=len(c.lines), episodes=len(c.eps), census_at=census_at,
        )
    finally:
        cx.close()


def _rejudge_eligible(prev: dict, moment_lines: int) -> bool:
    """`no_clear_moment` words come back when the corpus grew (§5.9)."""
    if (prev.get("judge_reason") or "") != "no_clear_moment":
        return False
    judged = int(prev.get("judged_moment_lines") or 0)
    return (moment_lines - judged) >= REJUDGE_NEW_LINES


def _filter_reason(c: _Corpus, form: str, w: dict, p1: str, p2: str, rank: Optional[int],
                   gloss: Optional[str], occ: int, eps: int, titles: int,
                   conc: Optional[float], standalone_ratio: Optional[float],
                   n_moments: int) -> Optional[tuple[str, str]]:
    """F1, F2, F4–F11 in order. Returns `(judge_status, judge_reason)` or None."""
    ls = _learn()

    # F1 corpus — at least one clean unknown occurrence in a downloaded episode
    if occ <= 0:
        # every occurrence was glued inside a compound (教 ← 魔女教) or sat on a
        # junk/lyric line — nothing clean is left to show
        return ("filtered", "fragment" if w["glued"] else "lyrics_only")

    # F2 content
    if not ls._is_content(form) or DIGIT_RE.match(form):
        return ("filtered", "function_word")
    has_kanji = bool(KANJI_RE.search(form))
    if len(form) < 2 and not has_kanji:
        return ("filtered", "fragment")
    if len(form) == 1 and has_kanji and (standalone_ratio or 0.0) < FRAGMENT_MIN_STANDALONE:
        return ("filtered", "fragment")

    # F4 POS (head-final)
    if p2 == "固有名詞":
        return ("filtered", "name")
    if p1 in ls._NON_VOCAB_POS:
        return ("filtered", "function_word")
    if p1 in _FUNC_POS_RANKED and (rank or 10 ** 9) <= 3000:
        return ("filtered", "function_word")
    if p2 in ("数詞", "フィラー"):
        return ("filtered", "function_word")
    if not has_kanji and not gloss and len(form) <= 4 and c.grammar_combo(form):
        return ("filtered", "function_word")

    # F5 kana spelling of a KNOWN word
    if not has_kanji and ls._to_hira(form) in c.known_readings:
        if _kana_of_known_same_sense(c, form, gloss):
            return ("probably_known", "kana_of_known")

    # F6 basic rank with no Migaku row at all
    if rank is not None and rank <= BASIC_RANK and form not in c.kw_rows and eps < 3:
        return ("probably_known", "basic_rank")

    # F7 name heuristic
    if (_is_katakana(form) and (conc or 0.0) >= 0.8 and titles <= NAME_MAX_TITLES
            and (rank or 10 ** 9) > 5000):
        return ("filtered", "name")

    # F8 gloss
    if not has_kanji and not gloss:
        return ("filtered", "kana_no_gloss")
    if rank is None and eps < 2 and not gloss:
        return ("filtered", "rare")

    # F9 frequency
    if rank is not None:
        if rank > MAX_FREQ_RANK:
            return ("filtered", "rare")
    elif not (gloss and eps >= 2):
        return ("filtered", "rare")

    # F10 lyrics-only (all clean lines were lyric texts → occ counted 0 above,
    # but a word whose every remaining line is a lyric text lands here)
    if not w["lines"]:
        return ("filtered", "lyrics_only")

    # F11 moments
    if n_moments <= 0:
        return ("filtered", "no_moment")
    return None


def _kana_of_known_same_sense(c: _Corpus, form: str, gloss: Optional[str]) -> bool:
    """F5: park the kana form only when its first JMdict sense equals that of the
    KNOWN kanji form with the same reading; homophones go to the judge."""
    ls = _learn()
    sense = _first_sense(gloss)
    if not sense:
        return True                     # no gloss to compare: park (F8 would drop it anyway)
    hira = ls._to_hira(form)
    forms = [f for f, st in c.kw_rows.items() if st == "KNOWN" and KANJI_RE.search(f)]
    cand = [f for f in forms if ls._to_hira(f) == hira]
    if not cand:
        try:
            rows = c.cx.execute(
                "SELECT dict_form FROM known_words WHERE status='KNOWN' AND reading=?",
                (hira,)).fetchall()
            cand = [r["dict_form"] for r in rows if KANJI_RE.search(r["dict_form"] or "")]
        except Exception:
            cand = []
    if not cand:
        return False
    try:
        glosses = ls._glosses_for(cand)
    except Exception:
        glosses = {}
    return any(_first_sense(glosses.get(f)) == sense for f in cand)


def _is_katakana(form: str) -> bool:
    return bool(form) and all("゠" <= ch <= "ヿ" or ch == "ー" for ch in form)


def _hints(c: _Corpus, form: str, w: dict, p1: str, p2: str, rank: Optional[int],
           gloss: Optional[str], conc: Optional[float], titles: int) -> list[str]:
    """Judge packet hints (§5.5 "hints"): tokenizer splits, reading matches a
    known word, the English capitalises it, the compound a single kanji sits in."""
    ls = _learn()
    hints: list[str] = []
    if not p1 and c.grammar_combo(form):
        try:
            parts = "+".join(t.surface for t in c._tagger(form))
            hints.append(f"tokenizer: UniDic splits this as {parts}")
        except Exception:
            hints.append("tokenizer: UniDic splits this form")
    if not KANJI_RE.search(form) and ls._to_hira(form) in c.known_readings:
        hira = ls._to_hira(form)
        kanji = next((f for f, st in c.kw_rows.items()
                      if st == "KNOWN" and KANJI_RE.search(f) and ls._to_hira(f) == hira), None)
        if kanji is None:
            try:
                r = c.cx.execute(
                    "SELECT dict_form FROM known_words WHERE status='KNOWN' AND reading=? "
                    "AND dict_form<>? LIMIT 1", (hira, form)).fetchone()
                kanji = r["dict_form"] if r else None
            except Exception:
                kanji = None
        hints.append(f"reading matches KNOWN {kanji or 'word'} (may be a homophone)")
    if _is_katakana(form):
        for lid in w["lines"][:6]:
            tr = c.lines[lid].get("translation") or ""
            if tr and _CAPS_RE.search(tr):
                hints.append("looks like a name in the English")
                break
    if len(form) == 1 and KANJI_RE.match(form) and w["compounds"]:
        comp, n = w["compounds"].most_common(1)[0]
        if comp:
            hints.append(f"single kanji; also appears inside {comp} ({n}×) — "
                         f"accept only if used standalone here")
    if titles == 1 and (conc or 0) >= 0.99 and w["occ"] >= 3:
        hints.append("only appears in one show")
    return hints


# ---------------------------------------------------------------------------
# Moment ranking (§5.4)
# ---------------------------------------------------------------------------

_UPSERT_MOMENT = """
INSERT INTO srs_moments (lemma, line_id, episode_id, idx, start_ms, end_ms, norm_text, text,
    translation, translation_source, translation_shared, target_surface, other_unknowns,
    line_score, run_id, batch_no)
VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
ON CONFLICT(lemma, episode_id, norm_text) DO UPDATE SET
    line_id=excluded.line_id, idx=excluded.idx, start_ms=excluded.start_ms,
    end_ms=excluded.end_ms, text=excluded.text,
    translation=CASE WHEN srs_moments.judged_at IS NULL THEN excluded.translation
                     ELSE srs_moments.translation END,
    translation_source=CASE WHEN srs_moments.judged_at IS NULL THEN excluded.translation_source
                            ELSE srs_moments.translation_source END,
    translation_shared=excluded.translation_shared,
    target_surface=excluded.target_surface,
    other_unknowns=CASE WHEN srs_moments.judged_at IS NULL THEN excluded.other_unknowns
                        ELSE srs_moments.other_unknowns END,
    line_score=excluded.line_score,
    run_id=CASE WHEN srs_moments.judged_at IS NULL THEN excluded.run_id ELSE srs_moments.run_id END,
    batch_no=CASE WHEN srs_moments.judged_at IS NULL THEN excluded.batch_no
                  ELSE srs_moments.batch_no END
"""


def rank_moments(lemma: str, cfg: SrsSettings, *, cx=None, run_id: Optional[int] = None,
                 batch_no: Optional[int] = None, limit: int = MOMENTS_PER_WORD,
                 translate: bool = True, exclude_judged: bool = False) -> list[MomentCandidate]:
    """Rank the word's lines (§5.4) and upsert the top `limit` as pending
    `srs_moments` rows (`ON CONFLICT(lemma, episode_id, norm_text)`).

    `cx` (a live connection, used by `generate.plan_run` inside its
    `BEGIN IMMEDIATE`) keeps everything in one transaction; without it the
    function opens and commits its own.
    """
    from ..db import connect, cursor

    own = cx is None
    conn = cx or connect()
    try:
        c = _corpus(cfg, conn, fresh=False)
        judged: set[str] = set()
        if exclude_judged:
            for r in conn.execute(
                "SELECT norm_text FROM srs_moments WHERE lemma=? AND judged_at IS NOT NULL",
                (lemma,)
            ):
                judged.add(r["norm_text"])
        picks = [p for p in c.pick_moments(lemma, limit=max(limit, MOMENTS_PER_WORD))
                 if p[1]["norm"] not in judged][:limit]
        if translate:
            _fill_translations(c, picks[:2])

        out: list[MomentCandidate] = []
        writer = conn if not own else None
        wx_ctx = None
        if own:
            wx_ctx = cursor()
            writer = wx_ctx.__enter__()
        try:
            for score, line, others, surface in picks:
                writer.execute(_UPSERT_MOMENT, (
                    lemma, line["line_id"], line["episode_id"], line["idx"],
                    line["start_ms"], line["end_ms"], line["norm"], line["text"],
                    line["translation"], line.get("translation_source"),
                    1 if line.get("translation_shared") else 0, surface, others,
                    score, run_id, batch_no,
                ))
                row = writer.execute(
                    "SELECT id FROM srs_moments WHERE lemma=? AND episode_id=? AND norm_text=?",
                    (lemma, line["episode_id"], line["norm"])).fetchone()
                out.append(MomentCandidate(
                    moment_id=row["id"] if row else None, lemma=lemma,
                    line_id=line["line_id"], episode_id=line["episode_id"], idx=line["idx"],
                    start_ms=line["start_ms"], end_ms=line["end_ms"], text=line["text"],
                    norm_text=line["norm"], translation=line["translation"],
                    translation_source=line.get("translation_source"),
                    translation_shared=bool(line.get("translation_shared")),
                    target_surface=surface, other_unknowns=others, line_score=score,
                ))
            if out:
                ids = [m.moment_id for m in out if m.moment_id]
                writer.execute(
                    "UPDATE srs_words SET best_moment_ids_json=?, updated_at=datetime('now') "
                    "WHERE lemma=?", (json.dumps(ids[:12]), lemma))
        finally:
            if wx_ctx is not None:
                wx_ctx.__exit__(None, None, None)
        return out
    finally:
        if own:
            conn.close()


def _fill_translations(c: _Corpus, picks: list) -> None:
    """Machine-translate the top-2 moments that have no translation (§5.4).
    Marks them `translation_source='mt'` (not independent evidence for the
    judge). Never raises; silently does nothing without an API key."""
    from .. import llm as _llm

    if not picks:
        return
    try:
        if not _llm.available():
            return
    except Exception:
        return
    ls = _learn()
    for _score, line, _o, _s in picks:
        if line.get("translation"):
            continue
        try:
            res = ls.translate_line(line["line_id"])
        except Exception as e:
            log.debug("translate_line(%s) failed: %s", line.get("line_id"), e)
            continue
        tr = (res or {}).get("translation")
        if tr:
            line["translation"] = tr
            line["translation_source"] = "mt"


# ---------------------------------------------------------------------------
# Jobs (§9.1)
# ---------------------------------------------------------------------------

def _job_census(payload: dict) -> None:
    """`srs_census {}` — census + scoring only (Up-next refresh). Raises on a DB
    error so the queue retries."""
    from .settings import get_settings
    res = run(get_settings())
    try:
        from ..events import bus
        bus.publish("srs", {"what": "generation", "state": "census"})
    except Exception as e:
        log.debug("bus publish failed: %s", e)
    log.info("srs_census: %d words, %d candidates", res.words_scored, len(res.candidates))


def register_jobs() -> None:
    """Register `srs_census` (priority 70 at every enqueue site, §9.1)."""
    from ..jobs.service import register
    register("srs_census", _job_census)
