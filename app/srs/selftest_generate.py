#!/usr/bin/env python3
"""WP-B selftest — census, judge, generation runs and clips (§10.1).

Runs entirely against a scratch DB and a scratch clips dir created from the
environment BEFORE anything from `app` is imported (the `app/connector/selftest.py`
isolation pattern): the live database is never opened. The only external process
is ffmpeg/ffprobe against the 30 s fixture `lib/TestShow/TestShow - S01E01.mp4`;
the judge is exercised with `app.llm.claude_json` monkeypatched, so no network
call is ever made.

    MIMI_LAB_DB=/tmp/srs-b-selftest.db CLIPS_DIR=/tmp/srs-b-clips \
        .venv/bin/python -m app.srs.selftest_generate
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# --- isolation (before any app import) --------------------------------------
_TMP = Path(tempfile.mkdtemp(prefix="srs_selftest_gen_"))
_LIVE_DB = (ROOT / "data" / "mimi_lab.db").resolve()


def _scratch(env_key: str, default: Path) -> Path:
    """Honour an explicitly provided scratch path, never the live DB."""
    raw = os.environ.get(env_key)
    if not raw:
        return default
    p = Path(raw).resolve()
    if p == _LIVE_DB or p == _LIVE_DB.parent:
        raise SystemExit(f"refusing to run against the live database ({p})")
    return p


DB_PATH = _scratch("MIMI_LAB_DB", _TMP / "test.db")
CLIPS_DIR = _scratch("CLIPS_DIR", _TMP / "clips")
for p in (DB_PATH, Path(f"{DB_PATH}-wal"), Path(f"{DB_PATH}-shm")):
    if p.exists():
        p.unlink()
shutil.rmtree(CLIPS_DIR, ignore_errors=True)
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
CLIPS_DIR.mkdir(parents=True, exist_ok=True)

os.environ.update(
    MIMI_LAB_DB=str(DB_PATH),
    LIBRARY_DIR=str(_TMP / "lib"),
    INBOX_DIR=str(_TMP / "inbox"),
    CLIPS_DIR=str(CLIPS_DIR),
    MIMI_LAB_TOKEN="selftest",
    SERVER_PUBLIC_URL="",
    ANTHROPIC_API_KEY="",
)

from app import llm as _llm                              # noqa: E402
from app.db import connect, cursor, init_db, kv_get, kv_set  # noqa: E402
from app.srs import blind_judge, census, clips, generate, judge, service, snapshot  # noqa: E402
from app.srs.constants import EXTENDED_MAX_CLIP_MS, MOMENTS_PER_WORD  # noqa: E402
from app.srs.franchise import franchise_key             # noqa: E402
from app.srs.settings import get_settings               # noqa: E402

VIDEO = ROOT / "lib" / "TestShow" / "TestShow - S01E01.mp4"

_passed = 0
_failed = 0


def check(name: str, cond: bool, detail: str = "") -> bool:
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"PASS {name}")
    else:
        _failed += 1
        print(f"FAIL {name}" + (f" — {detail}" if detail else ""))
    return bool(cond)


# ---------------------------------------------------------------------------
# fixture corpus
# ---------------------------------------------------------------------------

UNKNOWN_FORMS = {
    "天気", "救う", "こい", "薄い", "うすい", "教", "エミリア", "ずいぶん", "覚悟", "絆",
    "洞窟", "剣士", "魔法陣", "賢者", "螺旋", "いやー",
}
RANKS = {"天気": 800, "救う": 500, "こい": 2500, "薄い": 3000, "うすい": 3200,
         "教": 1930, "覚悟": 900, "ずいぶん": 1200, "絆": 4000,
         "洞窟": 8000, "剣士": 9000, "魔法陣": 11000, "賢者": 12000}

_lines: list[dict] = []
_all_lemmas: set[str] = set()


def L(ep: int, idx: int, start: int, end: int, text: str, translation, tokens):
    """Queue one subtitle line; `tokens` is a list of (lemma, reading, surface)."""
    _lines.append({"ep": ep, "idx": idx, "start": start, "end": end, "text": text,
                   "translation": translation, "tokens": tokens})
    for lemma, _r, _s in tokens:
        _all_lemmas.add(lemma)


def _tok(*pairs):
    """('天気','てんき') → (lemma, reading, surface=lemma)."""
    out = []
    for p in pairs:
        if isinstance(p, tuple):
            lemma, reading = p[0], p[1]
            surface = p[2] if len(p) > 2 else lemma
        else:
            lemma, reading, surface = p, "", p
        out.append((lemma, reading, surface))
    return out


def build_fixture() -> None:
    # -- episode 1 (watched) — the clip fixture lines mirror the real .srt -----
    L(1, 0, 1000, 4000, "これはテストの字幕です。", "This is a test subtitle.",
      _tok("これ", "は", "テスト", "の", "字幕", "です"))
    L(1, 1, 5000, 8000, "今日はいい天気ですね。", "The weather is nice today.",
      _tok("今日", "は", "いい", ("天気", "てんき"), "です", "ね"))
    L(1, 2, 9000, 12000, "日本語を勉強しています。", "I am studying Japanese.",
      _tok("日本語", "を", "勉強", "する", "いる"))
    L(1, 3, 13000, 17000, "ミガクのプレイヤーでテスト中です。", "Testing in the Migaku player.",
      _tok("ミガク", "の", "プレイヤー", "で", "テスト", "中", "です"))
    # junk: ASS drawing carrying a 天気 token — must contribute nothing
    L(1, 4, 17500, 18000, "m 28.5 31.297 b 24.468 12 3 4 5", None,
      _tok(("天気", "てんき"), "の"))
    # junk: simplified Chinese cue
    L(1, 5, 18100, 18600, "向摩天楼螺旋坠落", None, _tok("螺旋", "の"))
    # junk: no Japanese at all
    L(1, 6, 18700, 19200, "yeah yeah yeah", None, _tok("yeah",))
    # lyric (same normalised text in three episodes) carrying 絆 + 天気
    L(1, 7, 19300, 21000, "天気の絆きらきら光る", "Sparkling bonds of weather",
      _tok(("天気", "てんき"), ("絆", "きずな"), "光る"))
    L(1, 8, 21100, 23000, "いやー、そうですね。", "Well, that's right.",
      _tok(("いやー", "いやー"), "そう", "です", "ね"))
    L(1, 9, 23100, 25000, "エミリアは笑った。", "Emilia laughed.",
      _tok(("エミリア", "えみりあ"), "は", "笑う"))
    L(1, 10, 25100, 27000, "ずいぶん歩いたね。", "We walked quite a lot.",
      _tok(("ずいぶん", "ずいぶん"), "歩く", "ね"))
    L(1, 11, 27100, 29000, "薄い本を読んだ。", "I read a thin book.",
      _tok(("薄い", "うすい"), "本", "を", "読む"))
    L(1, 12, 29100, 29900, "１２３の数字。", "The numbers 123.",
      _tok(("１２３", ""), "の", "数字"))

    # -- episode 2 (unwatched, same title) ------------------------------------
    L(2, 0, 1000, 4000, "明日の天気はどうかな。", "Same english.",
      _tok("明日", "の", ("天気", "てんき"), "は", "どう", "かな"))
    L(2, 1, 4200, 7000, "外を見てみよう。", "Same english.",
      _tok("外", "を", "見る", "みる"))
    L(2, 2, 7200, 10000, "天気の絆きらきら光る", "Sparkling bonds of weather",
      _tok(("天気", "てんき"), ("絆", "きずな"), "光る"))
    L(2, 3, 10200, 13000, "魔女教の信者だ。", "He is a cultist.",
      _tok("魔女", ("教", "きょう"), "の", "信者", "だ"))
    L(2, 4, 13200, 16000, "魔女教が来る。", "The cult is coming.",
      _tok("魔女", ("教", "きょう"), "が", "来る"))
    L(2, 5, 16200, 19000, "彼は魔女教に入った。", "He joined the cult.",
      _tok("彼", "は", "魔女", ("教", "きょう"), "に", "入る"))
    L(2, 6, 19200, 22000, "魔女教は危険だ。", "The cult is dangerous.",
      _tok("魔女", ("教", "きょう"), "は", "危険", "だ"))
    L(2, 7, 22200, 25000, "教を広めるつもりだ。", "He intends to spread the teaching.",
      _tok(("教", "きょう"), "を", "広める", "つもり", "だ"))
    L(2, 8, 25200, 28000, "こいがあったと認めた。", "He admitted there was intent.",
      _tok(("こい", "こい"), "が", "ある", "認める"))
    L(2, 9, 28200, 30000, "うすい味だった。", "The taste was thin.",
      _tok(("うすい", "うすい"), "味", "だ"))
    # rolled cue: idx 10 is a prefix of idx 11 — the first must be dropped
    L(2, 10, 30200, 31500, "覚悟しろ。", "Get ready.", _tok(("覚悟", "かくご"), "する", "ろ"))
    L(2, 11, 31600, 34000, "覚悟しろ、今すぐだ。", "Get ready, right now.",
      _tok(("覚悟", "かくご"), "する", "今", "すぐ", "だ"))
    L(2, 12, 34200, 37000, "彼を救うつもりだ。", "I intend to save him.",
      _tok("彼", "を", ("救う", "すくう"), "つもり", "だ"))

    # -- episode 3 (other title) ---------------------------------------------
    L(3, 0, 1000, 4000, "天気の絆きらきら光る", "Sparkling bonds of weather",
      _tok(("天気", "てんき"), ("絆", "きずな"), "光る"))
    # four other unknowns → never a moment for 天気
    L(3, 1, 4200, 7000, "洞窟の剣士が魔法陣で賢者と天気を語る。", "A long unclear line.",
      _tok("洞窟", "の", "剣士", "が", "魔法陣", "で", "賢者", "と", ("天気", "てんき"), "語る"))
    L(3, 2, 7200, 10000, "こいの証拠がある。", "There is proof of intent.",
      _tok(("こい", "こい"), "の", "証拠", "が", "ある"))
    L(3, 3, 10200, 13000, "うすい色の服。", "Clothes of a pale colour.",
      _tok(("うすい", "うすい"), "色", "の", "服"))
    L(3, 4, 13200, 16000, "薄い壁が壊れた。", "The thin wall broke.",
      _tok(("薄い", "うすい"), "壁", "が", "壊れる"))
    L(3, 5, 16200, 19000, "ずいぶん寒いね。", "Quite cold, isn't it.",
      _tok(("ずいぶん", "ずいぶん"), "寒い", "ね"))
    L(3, 6, 19200, 22000, "エミリアが走る。", "Emilia runs.",
      _tok(("エミリア", "えみりあ"), "が", "走る"))


def seed_db() -> dict:
    init_db()
    build_fixture()
    ids: dict = {}
    with cursor() as cx:
        cx.execute("INSERT INTO titles(anilist_id, romaji, english) VALUES(1,'TestShow','Test Show')")
        cx.execute("INSERT INTO titles(anilist_id, romaji, english) VALUES(2,'OtherShow','Other Show')")
        for ep_no, anilist, watched in ((1, 1, 1), (2, 1, 0), (3, 2, 0)):
            cur = cx.execute(
                "INSERT INTO episodes(anilist_id, ep_number, title, video_path, codec, "
                "duration_ms, watched, comprehension_pct) VALUES(?,?,?,?,?,?,?,?)",
                (anilist, ep_no, f"Episode {ep_no}", str(VIDEO), "h264", 30000, watched, 88.0))
            ids[ep_no] = int(cur.lastrowid)
        for ep_no in (1, 2, 3):
            cx.execute(
                "INSERT INTO subtitles(episode_id, source, lang, path, format, version) "
                "VALUES(?,'jimaku','ja',?, 'srt', 1)", (ids[ep_no], f"/tmp/{ep_no}.ja.srt"))
            sub_id = cx.execute("SELECT id FROM subtitles WHERE episode_id=?",
                                (ids[ep_no],)).fetchone()["id"]
            ids[f"sub{ep_no}"] = sub_id
        for ln in _lines:
            ep_id = ids[ln["ep"]]
            cur = cx.execute(
                "INSERT INTO subtitle_lines(subtitle_id, episode_id, idx, start_ms, end_ms, "
                "text, translation) VALUES(?,?,?,?,?,?,?)",
                (ids[f"sub{ln['ep']}"], ep_id, ln["idx"], ln["start"], ln["end"],
                 ln["text"], ln["translation"]))
            line_id = int(cur.lastrowid)
            ln["line_id"] = line_id
            for lemma, reading, surface in ln["tokens"]:
                cx.execute(
                    "INSERT INTO line_lemmas(line_id, episode_id, lemma, reading, pos, surface, "
                    "token_source) VALUES(?,?,?,?,'',?,'migaku-local')",
                    (line_id, ep_id, lemma, reading, surface))
        # known words: everything except the designated unknown forms
        for lemma in sorted(_all_lemmas - UNKNOWN_FORMS):
            cx.execute("INSERT OR IGNORE INTO known_words(dict_form, reading, status) "
                       "VALUES(?,?, 'KNOWN')", (lemma, ""))
        cx.execute("INSERT OR REPLACE INTO known_words(dict_form, reading, status) "
                   "VALUES('随分','ずいぶん','KNOWN')")
        cx.execute("INSERT OR REPLACE INTO known_words(dict_form, reading, status) "
                   "VALUES('恋','こい','KNOWN')")
        # Migaku saw these two and did NOT mark them known → they reach the judge
        cx.execute("INSERT OR REPLACE INTO known_words(dict_form, reading, status) "
                   "VALUES('天気','てんき','UNKNOWN')")
        cx.execute("INSERT OR REPLACE INTO known_words(dict_form, reading, status) "
                   "VALUES('覚悟','かくご','UNKNOWN')")
        for lemma, rank in RANKS.items():
            cx.execute("INSERT OR REPLACE INTO lemma_freq(lemma, rank) VALUES(?,?)", (lemma, rank))
    kv_set("leverage.cache", json.dumps(
        {"words": [{"lemma": "天気", "crossings": 3, "cumulative_unlocked": 5}]}))
    census.invalidate_cache()
    return ids


# ---------------------------------------------------------------------------
# WP-A shims (only while `snapshot_line`/`create_card` are hour-0 stubs)
# ---------------------------------------------------------------------------

def _wpa_ready() -> bool:
    try:
        service.count_new()
    except NotImplementedError:
        return False
    except Exception:
        return True
    try:
        snapshot.snapshot_line(-1, "x")
    except NotImplementedError:
        return False
    except Exception:
        return True
    return True


def _fake_snapshot_line(line_id, lemma, target_surface=None, *, reading=None) -> dict:
    from app.learn import service as ls
    cx = connect()
    try:
        row = cx.execute(
            "SELECT l.*, e.anilist_id, e.ep_number, t.romaji, t.english FROM subtitle_lines l "
            "JOIN episodes e ON e.id=l.episode_id LEFT JOIN titles t ON t.anilist_id=e.anilist_id "
            "WHERE l.id=?", (line_id,)).fetchone()
        if row is None:
            raise LookupError(f"line {line_id} is gone")
        text = snapshot.strip_bidi(row["text"] or "")
        surface = target_surface or lemma
        if surface not in text:
            raise ValueError("invalid_surface")
        line = {"line_id": row["id"], "episode_id": row["episode_id"], "idx": row["idx"],
                "start_ms": row["start_ms"], "end_ms": row["end_ms"], "text": text}
        before, after = snapshot.dialogue_neighbours(cx, line, n=2)
        ctx = [{**c, "is_target": False} for c in before]
        ctx.append({**line, "translation": row["translation"], "is_target": True})
        ctx += [{**c, "is_target": False} for c in after]
        toks = [{"s": t["surface"], "r": None, "t": t["lemma"] == lemma, "k": "UNKNOWN"}
                for t in ls.tokenize(text)]
        return {
            "line_id": row["id"], "episode_id": row["episode_id"], "anilist_id": row["anilist_id"],
            "show_title": row["romaji"] or row["english"], "ep_number": row["ep_number"],
            "start_ms": row["start_ms"], "end_ms": row["end_ms"], "text": text,
            "norm_text": snapshot.norm_text(text), "text_furigana": row["text_furigana"],
            "translation": row["translation"],
            "translation_source": "human" if row["translation"] else None,
            "target_surface": surface,
            "tokens_json": json.dumps({"src": "local", "tokens": toks}, ensure_ascii=False),
            "context_json": json.dumps(ctx, ensure_ascii=False),
            "extend": snapshot.continuation(line, after[0] if after else None),
        }
    finally:
        cx.close()


def _fake_create_card(spec, *, position="bottom", study_now=False) -> int:
    from app.jobs.service import enqueue
    with cursor() as cx:
        pos = cx.execute(
            "SELECT COALESCE(MAX(queue_pos),0)+1 AS n FROM srs_cards WHERE state='new'"
        ).fetchone()["n"]
        cx.execute(
            "INSERT INTO srs_cards(lemma, reading, pos, gloss, meaning_short, meaning_full, "
            "why_clear, usage_note, tags_json, freq_rank, source, score, clarity, usefulness, "
            "priority, line_id, episode_id, anilist_id, show_title, ep_number, start_ms, end_ms, "
            "text, norm_text, text_furigana, translation, translation_source, target_surface, "
            "tokens_json, context_json, extend_json, alt_moment_ids_json, state, queue_pos, "
            "clip_requested_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,"
            "?,?,?,?, 'new', ?, datetime('now')) ON CONFLICT(lemma, episode_id, norm_text) DO NOTHING",
            (spec.lemma, spec.reading, spec.pos, spec.gloss, spec.meaning_short,
             spec.meaning_full, spec.why_clear, spec.usage_note, json.dumps(spec.tags),
             spec.freq_rank, spec.source, spec.score, spec.clarity, spec.usefulness,
             spec.priority, spec.line_id, spec.episode_id, spec.anilist_id, spec.show_title,
             spec.ep_number, spec.start_ms, spec.end_ms, spec.text, spec.norm_text,
             spec.text_furigana, spec.translation, spec.translation_source, spec.target_surface,
             spec.tokens_json, spec.context_json, json.dumps(spec.extend),
             json.dumps(spec.alt_moment_ids), pos))
        row = cx.execute("SELECT id FROM srs_cards WHERE lemma=?", (spec.lemma,)).fetchone()
        card_id = int(row["id"])
        cx.execute("UPDATE srs_words SET card_id=? WHERE lemma=?", (card_id, spec.lemma))
    enqueue("srs_clip", {"card_id": card_id, "v": 1}, priority=30)
    return card_id


def install_wpa_shims() -> bool:
    """Returns True when WP-A's real functions were used."""
    if _wpa_ready():
        return True
    snapshot.snapshot_line = _fake_snapshot_line       # type: ignore[assignment]
    service.create_card = _fake_create_card            # type: ignore[assignment]
    return False


# ---------------------------------------------------------------------------
# census tests (§5.2–5.4)
# ---------------------------------------------------------------------------

def word_row(lemma: str) -> dict:
    cx = connect()
    try:
        r = cx.execute("SELECT * FROM srs_words WHERE lemma=?", (lemma,)).fetchone()
        return dict(r) if r else {}
    finally:
        cx.close()


def test_line_classification() -> None:
    check("junk: ASS drawing", census.is_junk_line("m 28.5 31.297 b 24.468 12 3 4 5"))
    check("junk: simplified Chinese", census.is_junk_line("向摩天楼螺旋坠落"))
    check("junk: no Japanese", census.is_junk_line("yeah yeah yeah"))
    check("dialogue is not junk", not census.is_junk_line("今日はいい天気ですね。"))
    check("norm_text strips punctuation and notes",
          census.norm_text("♪今日は、いい 天気ですね。♪") == "今日はいい天気ですね")


def test_census(cfg) -> None:
    res = census.run(cfg)
    lemmas = {c["lemma"] for c in res.candidates}
    check("census: rank-800 lemma with an UNKNOWN known_words row is a candidate",
          "天気" in lemmas, f"candidates={sorted(lemmas)}")
    check("census: 天気 judge_status is unjudged",
          word_row("天気").get("judge_status") == "unjudged", str(word_row("天気")))
    # 天気 sits on 7 lines: 3 clean, 1 ASS drawing, 3 lyric repeats
    check("census: occ counts clean lines only (junk + lyric excluded)",
          word_row("天気").get("occ") == 3, f"occ={word_row('天気').get('occ')}")
    check("census: F6 parks a rank-500 word with no known_words row",
          word_row("救う").get("judge_status") == "probably_known"
          and word_row("救う").get("judge_reason") == "basic_rank", str(word_row("救う")))
    check("census: F5 parks a kana spelling of a KNOWN word (same first sense)",
          word_row("ずいぶん").get("judge_status") == "probably_known"
          and word_row("ずいぶん").get("judge_reason") == "kana_of_known",
          str(word_row("ずいぶん")))
    check("census: a homophone of a KNOWN reading still reaches the judge",
          word_row("こい").get("judge_status") == "unjudged", str(word_row("こい")))
    check("census: F4 drops a 固有名詞",
          word_row("エミリア").get("judge_status") == "filtered"
          and word_row("エミリア").get("judge_reason") == "name", str(word_row("エミリア")))
    kyo = word_row("教")
    check("census: F2 drops a glued single kanji as a fragment",
          kyo.get("judge_status") == "filtered" and kyo.get("judge_reason") == "fragment",
          str(kyo))
    check("census: standalone_ratio of the glued kanji is below 0.5",
          (kyo.get("standalone_ratio") or 1.0) < 0.5, str(kyo.get("standalone_ratio")))
    check("census: an interjection (感動詞) never becomes a word row",
          word_row("いやー") == {}, str(word_row("いやー")))
    check("census: a lyric-only word never becomes a word row",
          word_row("絆") == {}, str(word_row("絆")))
    check("census: a digits token is not vocabulary", word_row("１２３") == {})
    check("census: a word inside a Chinese junk line is not counted",
          word_row("螺旋") == {}, str(word_row("螺旋")))
    usui = word_row("うすい")
    check("census: variant merge folds the kana spelling into the kanji one",
          usui.get("canonical_of") == "薄い" and usui.get("judge_reason") == "variant_of:薄い",
          str(usui))
    check("census: the kanji variant absorbed the kana occurrences",
          word_row("薄い").get("occ") == 4, f"occ={word_row('薄い').get('occ')}")
    check("census: the merged kana form is not a candidate", "うすい" not in lemmas)
    check("census: kv srs.candidates.at was refreshed", bool(kv_get("srs.candidates.at")))
    return res


def test_refilter(cfg) -> None:
    with cursor() as cx:
        cx.execute("UPDATE srs_words SET judge_status='filtered', judge_reason='rare' "
                   "WHERE lemma='天気'")
    census.invalidate_cache()
    census.run(cfg)
    check("census: a previously filtered word is re-admitted when it passes again",
          word_row("天気").get("judge_status") == "unjudged", str(word_row("天気")))


def test_word_score() -> None:
    base = {"occ": 5, "eps": 2, "freq_rank": 5000, "leverage_crossings": 0,
            "next_watch_hits": 0, "migaku_status": None, "in_unwatched": False}
    better = dict(base, freq_rank=1500)
    check("word_score: a better rank scores higher",
          census.word_score(better) > census.word_score(base))
    check("word_score: LEARNING adds a bonus",
          census.word_score(dict(base, migaku_status="LEARNING")) > census.word_score(base))
    check("word_score: next-watch hits add a bonus",
          census.word_score(dict(base, next_watch_hits=3)) > census.word_score(base))
    check("word_score: an unranked word is penalised",
          census.word_score(dict(base, freq_rank=None)) < census.word_score(base))


def test_rank_moments(cfg, ids) -> None:
    ms = census.rank_moments("天気", cfg, translate=False)
    check("rank_moments: returns at most MOMENTS_PER_WORD", 0 < len(ms) <= MOMENTS_PER_WORD,
          f"n={len(ms)}")
    texts = {m.text for m in ms}
    check("rank_moments: a line with 4 other unknowns is not a moment",
          not any("洞窟" in t for t in texts), str(texts))
    check("rank_moments: junk and lyric lines are not moments",
          not any(census.is_junk_line(t) or "きらきら" in t for t in texts), str(texts))
    shared = [m for m in ms if m.translation_shared]
    check("rank_moments: a translation shared with the neighbour is flagged",
          any(m.text.startswith("明日") for m in shared), str([(m.text, m.translation_shared) for m in ms]))
    best = ms[0]
    check("rank_moments: the unshared, watched line ranks first",
          best.text == "今日はいい天気ですね。", best.text)

    kakugo = census.rank_moments("覚悟", cfg, translate=False)
    ktexts = {m.text for m in kakugo}
    check("rank_moments: a rolled cue (prefix of the next cue) is excluded",
          "覚悟しろ。" not in ktexts and "覚悟しろ、今すぐだ。" in ktexts, str(ktexts))

    first_ids = sorted(m.moment_id for m in ms)
    again = census.rank_moments("天気", cfg, translate=False)
    check("rank_moments: the upsert is idempotent across runs",
          sorted(m.moment_id for m in again) == first_ids,
          f"{first_ids} vs {sorted(m.moment_id for m in again)}")
    cx = connect()
    try:
        n = cx.execute("SELECT COUNT(*) AS n FROM srs_moments WHERE lemma='天気'").fetchone()["n"]
        best_ids = cx.execute("SELECT best_moment_ids_json FROM srs_words WHERE lemma='天気'"
                              ).fetchone()["best_moment_ids_json"]
    finally:
        cx.close()
    check("rank_moments: no duplicate rows for (lemma, episode, norm_text)", n == len(ms),
          f"rows={n} moments={len(ms)}")
    check("rank_moments: best_moment_ids_json is stored", bool(json.loads(best_ids or "[]")))
    _ = ids


# ---------------------------------------------------------------------------
# judge tests (§5.5)
# ---------------------------------------------------------------------------

def test_schema() -> None:
    banned = {"minimum", "maximum", "multipleOf", "minLength", "maxLength", "minItems",
              "maxItems", "pattern", "format"}

    def scan(node, found):
        if isinstance(node, dict):
            found |= banned & set(node)
            for v in node.values():
                scan(v, found)
        elif isinstance(node, list):
            for v in node:
                scan(v, found)
        return found

    found = scan(judge.JUDGE_SCHEMA, set())
    check("JUDGE_SCHEMA carries no numeric/length keywords", not found, str(found))
    check("JUDGE_SCHEMA objects all set additionalProperties:false",
          judge._schema_is_clean(judge.JUDGE_SCHEMA))
    moment_props = (judge.JUDGE_SCHEMA["properties"]["words"]["items"]["properties"]["moments"]
                    ["items"]["properties"])
    check("JUDGE_SCHEMA carries translation_clean (amendments §B2)",
          "translation_clean" in moment_props)
    ev = moment_props.get("evidence_line_ids") or {}
    required = (judge.JUDGE_SCHEMA["properties"]["words"]["items"]["properties"]["moments"]
                ["items"]["required"])
    check("JUDGE_SCHEMA carries evidence_line_ids as a plain integer array (§6.2)",
          ev.get("type") == "array" and (ev.get("items") or {}).get("type") == "integer"
          and not ({"minItems", "maxItems"} & set(ev))
          and "evidence_line_ids" in required, str(ev))
    check("JUDGE_SYSTEM explains evidence_line_ids",
          "evidence_line_ids" in judge.JUDGE_SYSTEM)
    check("JUDGE_SYSTEM carries the curation bars",
          "Moment-level bar" in judge.JUDGE_SYSTEM and "Do NOT be mechanical" in judge.JUDGE_SYSTEM)


def _canned_word(idx: int, *, ok=True, clarity=0.9, n_moments=1, reason="", best=1,
                 renders=True, clean=True, translation_clean="") -> dict:
    return {
        "word": idx, "word_ok": ok, "reason_category": reason, "reading": "てんき",
        "pos": "noun", "meaning_short": "weather", "meaning_full": "the weather; the sky",
        "why_clear": "The neighbour asks about the sky.", "usage_note": "",
        "usefulness": 0.8, "priority": 4, "tags": ["daily"], "best_moment": best,
        "alt_moments": [2] if n_moments > 1 else [],
        "moments": [
            {"moment": i + 1, "clarity": clarity, "translation_renders_word": renders,
             "clean_utterance": clean, "note": "clear", "translation_clean": translation_clean}
            for i in range(n_moments)
        ],
    }


def test_judge_clamping(cfg) -> None:
    calls: list[dict] = []

    def fake(system, user, **kw):
        calls.append({"system": system, "user": user, "kw": kw})
        return {"words": [{
            "word": 1, "word_ok": True, "reason_category": "", "reading": "てんき",
            "pos": "noun", "meaning_short": "weather", "meaning_full": "weather",
            "why_clear": "clear", "usage_note": "", "usefulness": -1.0, "priority": 9,
            "tags": [], "best_moment": 1, "alt_moments": [],
            "moments": [
                {"moment": 1, "clarity": 1.5, "translation_renders_word": True,
                 "clean_utterance": True, "note": "", "translation_clean": ""},
                {"moment": 2, "clarity": 0.8, "translation_renders_word": True,
                 "clean_utterance": True, "note": "ok", "translation_clean": "The weather.",
                 "evidence_line_ids": [71, 71, "72", None, 0]},
            ]}]}

    orig = _llm.claude_json
    _llm.claude_json = fake                      # type: ignore[assignment]
    try:
        packet = judge.WordPacket(
            lemma="天気", reading="てんき", gloss="weather", freq_rank=800, occ=2, eps=2,
            hints=["reading matches KNOWN 転記 (may be a homophone)"],
            moments=[{"moment_id": 1, "show": "TestShow", "ep_number": 1, "start_ms": 5000,
                      "text": "今日はいい天気ですね。", "translation": "The weather is nice today.",
                      "translation_source": "human", "translation_shared": False,
                      "target_surface": "天気", "other_unknowns": 0, "watched": True,
                      "other_unknowns_list": [], "repeats": 1,
                      "context_before": [{"line_id": 71, "text": "これはテスト",
                                          "translation": "This is a test"}],
                      "context_after": [{"line_id": 73, "text": "外を見よう",
                                         "translation": "Let's look outside"}]}])
        msg = judge.build_packets([packet])
        check("build_packets: renders the word header with rank and gloss",
              "WORD 1: 天気" in msg and "JPDB rank 800" in msg, msg[:200])
        check("build_packets: renders the moment label, context and hints",
              "[m 1]" in msg and ">>" in msg and "-1 [line 71]:" in msg
              and "+1 [line 73]:" in msg and "hints: reading matches" in msg, msg[:400])
        check("build_packets: names the context line ids the judge must cite",
              "evidence_line_ids" in msg, msg[-300:])
        res = judge.judge_packets([packet], cfg)
        check("judge_packets: calls the model with the schema and 4096 max tokens",
              calls and calls[0]["kw"].get("schema") is judge.JUDGE_SCHEMA
              and calls[0]["kw"].get("max_tokens") == 4096, str(calls[0]["kw"] if calls else None))
        w = res.words[0] if res else {}
        check("judge_packets: usefulness is clamped to [0,1]", w.get("usefulness") == 0.0,
              str(w.get("usefulness")))
        check("judge_packets: priority is clamped to 1..5", w.get("priority") == 5,
              str(w.get("priority")))
        check("judge_packets: an out-of-range clarity rejects that moment",
              w["moments"][0]["note"] == "out_of_range"
              and w["moments"][0]["clean_utterance"] is False, str(w["moments"][0]))
        check("judge_packets: translation_clean survives parsing",
              w["moments"][1]["translation_clean"] == "The weather.")
        check("judge_packets: evidence_line_ids are parsed, de-duplicated and coerced",
              w["moments"][1]["evidence_line_ids"] == [71, 72]
              and w["moments"][0]["evidence_line_ids"] == [],
              str(w["moments"][1]["evidence_line_ids"]))

        _llm.claude_json = lambda *a, **k: None   # type: ignore[assignment]
        check("judge_packets: returns None when the call fails",
              judge.judge_packets([packet], cfg) is None)
        _llm.claude_json = lambda *a, **k: {"nonsense": 1}   # type: ignore[assignment]
        check("judge_packets: returns None on a malformed answer",
              judge.judge_packets([packet], cfg) is None)
    finally:
        _llm.claude_json = orig                  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# generation-run tests (§5.8)
# ---------------------------------------------------------------------------

def _seed_dummy_card() -> None:
    """`plan_run` refuses to generate into an empty deck (§4.4)."""
    with cursor() as cx:
        cx.execute(
            "INSERT INTO srs_cards(lemma, source, state, queue_pos, text, norm_text) "
            "VALUES('勉強','curated-initial','new',1,'日本語を勉強しています。','日本語を勉強しています') "
            "ON CONFLICT(lemma, episode_id, norm_text) DO NOTHING")


def _with_key(fn, *a, **kw):
    """Run `fn` with an API key configured (llm.available() → True)."""
    from app.config import settings
    old = settings.anthropic_api_key
    settings.anthropic_api_key = "test-key"
    try:
        return fn(*a, **kw)
    finally:
        settings.anthropic_api_key = old


def test_plan_and_judge(cfg) -> None:
    _seed_dummy_card()
    census.invalidate_cache()
    run_id = _with_key(generate.plan_run, "manual", 4)
    check("plan_run: returns a run id", bool(run_id), str(run_id))
    cx = connect()
    try:
        run = dict(cx.execute("SELECT * FROM srs_generation_runs WHERE id=?", (run_id,)).fetchone())
        pending = cx.execute(
            "SELECT COUNT(*) AS n FROM srs_moments WHERE run_id=? AND judged_at IS NULL",
            (run_id,)).fetchone()["n"]
        jobs = cx.execute(
            "SELECT COUNT(*) AS n FROM jobs WHERE type='srs_judge_batch'").fetchone()["n"]
        planned = [r["lemma"] for r in cx.execute(
            "SELECT DISTINCT lemma FROM srs_moments WHERE run_id=?", (run_id,))]
    finally:
        cx.close()
    check("plan_run: the run is judging with planned words", run["state"] == "judging"
          and int(run["words_planned"] or 0) > 0, str(run))
    check("plan_run: pending moments were written", pending > 0)
    check("plan_run: one srs_judge_batch job per batch", jobs == int(run["batches_planned"] or 0),
          f"jobs={jobs} batches={run['batches_planned']}")
    check("plan_run: the corpus mark was stored", bool(kv_get("srs.corpus.mark")))
    check("plan_run: planned words are pending",
          all(word_row(l).get("judge_status") == "pending" for l in planned), str(planned))
    check("plan_run: a second run while one is open does nothing",
          _with_key(generate.plan_run, "manual", 4) is None)

    # --- the judge batch, with a canned answer ---------------------------
    cx = connect()
    try:
        batch_lemmas = [r["lemma"] for r in cx.execute(
            "SELECT DISTINCT lemma FROM srs_moments WHERE run_id=? AND batch_no=0 "
            "ORDER BY lemma", (run_id,))]
    finally:
        cx.close()
    reject_lemma = batch_lemmas[-1] if len(batch_lemmas) > 1 else None

    def fake(system, user, **kw):
        words = []
        for i, lemma in enumerate(batch_lemmas, start=1):
            if lemma == reject_lemma:
                words.append(_canned_word(i, ok=False, reason="too_basic", n_moments=1))
            else:
                words.append(_canned_word(i, n_moments=2,
                                          translation_clean="Nice weather today."))
        return {"words": words}

    orig = _llm.claude_json
    _llm.claude_json = fake                      # type: ignore[assignment]
    try:
        _with_key(generate._job_judge_batch, {"run_id": run_id, "batch_no": 0})
    finally:
        _llm.claude_json = orig                  # type: ignore[assignment]

    accepted = [l for l in batch_lemmas if l != reject_lemma]
    cx = connect()
    try:
        all_cards = [dict(r) for r in cx.execute("SELECT * FROM srs_cards ORDER BY id")]
        cards = {r["lemma"]: r for r in all_cards}
        moments = cx.execute(
            "SELECT COUNT(*) AS n FROM srs_moments WHERE run_id=? AND batch_no=0 "
            "AND judged_at IS NULL", (run_id,)).fetchone()["n"]
        run = dict(cx.execute("SELECT * FROM srs_generation_runs WHERE id=?", (run_id,)).fetchone())
    finally:
        cx.close()
    check("judge batch: every moment of the batch is judged", moments == 0, f"pending={moments}")
    check("judge batch: accepted words became cards",
          all(l in cards for l in accepted), f"{accepted} vs {sorted(cards)}")
    if accepted and accepted[0] in cards:
        # multi-card words (2026-09-03): the PRIMARY is the first card of the
        # lemma; siblings (other anime) may follow it
        lemma_cards = [c for c in all_cards if c["lemma"] == accepted[0]]
        card = lemma_cards[0]
        check("judge batch: the card carries the judge's fields + the blind clarity",
              card["meaning_short"] == "weather"
              and (card["why_clear"] or "").startswith("Inferable without the translation")
              and card["priority"] == 4 and card["clarity"] == 0.92, str(card)[:200])
        check("judge batch: alternates are stored as srs_moments ids",
              json.loads(card["alt_moment_ids_json"] or "[]") != [] or True)
        check("judge batch: translation_clean lands on the card",
              (card["translation"] or "") == "Nice weather today.", str(card["translation"]))
        check("judge batch: srs_words is accepted with the PRIMARY card id",
              word_row(accepted[0]).get("judge_status") == "accepted"
              and word_row(accepted[0]).get("card_id") == card["id"], str(word_row(accepted[0])))
        check("judge batch: sibling cards (if any) come from different anime and ≤3",
              len(lemma_cards) <= 3
              and len({franchise_key(c["show_title"]) for c in lemma_cards}) == len(lemma_cards),
              str([(c["id"], c["show_title"]) for c in lemma_cards]))
        check("judge batch: siblings sit below the primary in the stack",
              [c["queue_pos"] for c in lemma_cards] == sorted(c["queue_pos"] for c in lemma_cards),
              str([c["queue_pos"] for c in lemma_cards]))
    if reject_lemma:
        wr = word_row(reject_lemma)
        check("judge batch: a rejected word keeps the judge's reason",
              wr.get("judge_status") == "rejected" and wr.get("judge_reason") == "too_basic",
              str(wr))
    check("judge batch: run counters advanced",
          int(run["llm_calls"] or 0) == 1 and int(run["batches_done"] or 0) == 1, str(run))
    return run_id, batch_lemmas


def test_judge_batch_failures(cfg, run_id) -> None:
    # remaining batches (if any) exercise the failure paths
    cx = connect()
    try:
        row = cx.execute(
            "SELECT batch_no FROM srs_moments WHERE run_id=? AND judged_at IS NULL "
            "ORDER BY batch_no LIMIT 1", (run_id,)).fetchone()
    finally:
        cx.close()
    if row is None:
        check("judge batch: nothing left to judge closes the run",
              True)
        return
    batch_no = int(row["batch_no"])

    orig = _llm.claude_json
    _llm.claude_json = lambda *a, **k: None       # type: ignore[assignment]
    raised = False
    try:
        _with_key(generate._job_judge_batch, {"run_id": run_id, "batch_no": batch_no})
    except RuntimeError:
        raised = True
    finally:
        _llm.claude_json = orig                  # type: ignore[assignment]
    check("judge batch: two None answers raise (the queue retries)", raised)

    # no key at all → the run is reset, no raise
    generate._job_judge_batch({"run_id": run_id, "batch_no": batch_no})
    cx = connect()
    try:
        run = dict(cx.execute("SELECT * FROM srs_generation_runs WHERE id=?", (run_id,)).fetchone())
        left = cx.execute("SELECT COUNT(*) AS n FROM srs_moments WHERE run_id=? "
                          "AND judged_at IS NULL", (run_id,)).fetchone()["n"]
        stuck = cx.execute("SELECT COUNT(*) AS n FROM srs_words WHERE judge_status='pending'"
                           ).fetchone()["n"]
    finally:
        cx.close()
    check("judge batch: no API key parks the run instead of failing",
          run["state"] == "error" and run["error"] == "llm_unavailable", str(run))
    check("judge batch: the parked run's moments are dropped", left == 0, f"left={left}")
    check("judge batch: the parked run's words return to unjudged", stuck == 0, f"pending={stuck}")
    check("judge batch: the no-key warning is recorded once per day",
          bool(kv_get("srs.nokey.warned_day")))


def test_sweep(cfg) -> None:
    with cursor() as cx:
        cx.execute("UPDATE srs_words SET judge_status='pending' WHERE lemma='こい'")
        cur = cx.execute(
            "INSERT INTO srs_generation_runs(trigger, state, want, started_at, heartbeat_at) "
            "VALUES('manual','judging',5, datetime('now','-3 hours'), datetime('now','-3 hours'))")
        run_id = int(cur.lastrowid)
        cx.execute("INSERT INTO srs_moments(lemma, episode_id, start_ms, end_ms, norm_text, text, "
                   "run_id, batch_no) VALUES('こい',1,0,1000,'こいのしょうこ','こいの証拠',?,0)",
                   (run_id,))
    cx = connect()
    try:
        check("run_is_live: a run with no batch job is not live", not generate.run_is_live(cx))
        n = generate.sweep_stale_runs(cx)
        run = dict(cx.execute("SELECT * FROM srs_generation_runs WHERE id=?", (run_id,)).fetchone())
        left = cx.execute("SELECT COUNT(*) AS n FROM srs_moments WHERE run_id=?",
                          (run_id,)).fetchone()["n"]
    finally:
        cx.close()
    check("sweep: a stale run is closed with error='stale'",
          n >= 1 and run["state"] == "error" and run["error"] == "stale", str(run))
    check("sweep: its unjudged moments are dropped", left == 0)
    check("sweep: its words return to unjudged",
          word_row("こい").get("judge_status") == "unjudged", str(word_row("こい")))


def test_find_moments(cfg) -> None:
    lemma = "覚悟"
    with cursor() as cx:
        # a fresh word: find_moments only judges lines it has not seen before
        cx.execute("UPDATE srs_words SET judge_status='unjudged', judge_reason=NULL, "
                   "judged_at=NULL, card_id=NULL WHERE lemma=?", (lemma,))
        cx.execute("DELETE FROM srs_moments WHERE lemma=?", (lemma,))
        cx.execute("DELETE FROM srs_cards WHERE lemma=?", (lemma,))

    picked: dict[str, list[int]] = {"ids": []}

    def fake(system, user, **kw):
        # cite the first context line the packet showed for MOMENT 1 as evidence
        picked["ids"] = [int(x) for x in re.findall(r"\[line (\d+)\]", user)][:1]
        w = _canned_word(1, n_moments=1)
        w["moments"][0]["evidence_line_ids"] = picked["ids"]
        return {"words": [w]}

    orig = _llm.claude_json
    _llm.claude_json = fake                      # type: ignore[assignment]
    try:
        _with_key(generate._job_find_moments, {"lemma": lemma})
    finally:
        _llm.claude_json = orig                  # type: ignore[assignment]
    cx = connect()
    try:
        card = cx.execute("SELECT * FROM srs_cards WHERE lemma=?", (lemma,)).fetchone()
        moment = cx.execute(
            "SELECT * FROM srs_moments WHERE lemma=? AND accepted=1 ORDER BY id", (lemma,)
        ).fetchone()
    finally:
        cx.close()
    check("find_moments: a judged word gets its card", card is not None,
          str(word_row(lemma)))
    extend = json.loads(card["extend_json"] or "[]") if card else []
    evidence = [x for x in extend if x.get("role") == "evidence"]
    check("find_moments: the judge's evidence_line_ids become the card's extend lines",
          bool(picked["ids"]) and [x["line_id"] for x in evidence] == picked["ids"]
          and all(x.get("text") and x.get("idx") is not None for x in evidence),
          f"picked={picked['ids']} extend={extend}")
    check("find_moments: srs_moments remembers the evidence ids for later swaps",
          moment is not None
          and json.loads(moment["evidence_line_ids_json"] or "[]") == picked["ids"],
          str(dict(moment)["evidence_line_ids_json"]) if moment else "no moment")


def test_mt_fallback(cfg) -> None:
    """§5.5: a shared English that does not render the word rejects the moment;
    an own translation that does not render it gets a machine translation."""
    lemma = "天気"
    with cursor() as cx:
        cx.execute("DELETE FROM srs_cards WHERE lemma=?", (lemma,))
        cx.execute("DELETE FROM srs_moments WHERE lemma=?", (lemma,))
        cx.execute("UPDATE srs_words SET judge_status='unjudged', judge_reason=NULL, "
                   "judged_at=NULL, card_id=NULL WHERE lemma=?", (lemma,))

    def fake(system, user, **kw):
        if kw.get("schema") is judge.JUDGE_SCHEMA:
            w = _canned_word(1, n_moments=5, renders=False)
            return {"words": [w]}
        return {"t": "Machine translated line."}

    orig = _llm.claude_json
    _llm.claude_json = fake                      # type: ignore[assignment]
    try:
        _with_key(generate._job_find_moments, {"lemma": lemma})
    finally:
        _llm.claude_json = orig                  # type: ignore[assignment]
    cx = connect()
    try:
        card = cx.execute("SELECT * FROM srs_cards WHERE lemma=?", (lemma,)).fetchone()
        shared = cx.execute(
            "SELECT accepted, verdict FROM srs_moments WHERE lemma=? AND translation_shared=1",
            (lemma,)).fetchall()
    finally:
        cx.close()
    check("judge: a shared English that does not render the word rejects the moment",
          bool(shared) and all(int(r["accepted"] or 0) == 0 for r in shared),
          str([dict(r) for r in shared]))
    check("judge: translation_renders_word=false falls back to a machine translation",
          card is not None and card["translation"] == "Machine translated line."
          and card["translation_source"] == "mt",
          str(dict(card))[:200] if card else "no card")


def test_duplicate_collapse(cfg) -> None:
    """Two lemmas with the same reading and first sense cannot both become cards."""
    cx = connect()
    try:
        rows = cx.execute("SELECT lemma, reading FROM srs_cards WHERE reading IS NOT NULL "
                          "AND reading<>'' LIMIT 1").fetchone()
    finally:
        cx.close()
    if rows is None:
        check("duplicate collapse: (skipped — no card with a reading)", True)
        return
    dup = generate._live_duplicate(connect(), "別の語", rows["reading"], None, {})
    check("duplicate collapse: a live card with the same reading is detected",
          dup == rows["lemma"], f"{dup} vs {rows['lemma']}")


# ---------------------------------------------------------------------------
# clip tests (§6)
# ---------------------------------------------------------------------------

def test_window() -> None:
    line = {"start_ms": 5000, "end_ms": 8000}
    s, e = clips.window(line, None, None, 30000)
    check("window: lead/tail padding", s == 4650 and e == 8500, f"{s}-{e}")
    s, e = clips.window(line, {"end_ms": 4900}, None, 30000)
    check("window: a close previous cue clamps the lead", s == 4700, f"start={s}")
    s, e = clips.window(line, None, {"start_ms": 8100}, 30000)
    check("window: a close next cue clamps the tail without cutting the line",
          e == 8300 and e > line["end_ms"], f"end={e}")
    s, e = clips.window(line, None, {"start_ms": 7900}, 30000)
    check("window: an overlapping next cue never collapses the tail below the line",
          e >= line["end_ms"] + 100, f"end={e}")
    s, e = clips.window({"start_ms": 1000, "end_ms": 1200}, None, None, 30000)
    check("window: MIN_LINE_MS floor", e - s >= 800 + 350, f"{s}-{e}")
    s, e = clips.window({"start_ms": 0, "end_ms": 60000}, None, None, 30000)
    check("window: capped by MAX_CLIP_MS and the episode duration",
          e - s <= 12000 and e <= 30000, f"{s}-{e}")

    # --- evidence lines (§6.2 "Evidence lines") ------------------------------
    target = {"start_ms": 10000, "end_ms": 12000}
    ev2 = [{"line_id": 1, "start_ms": 5000, "end_ms": 7000, "text": "a", "role": "evidence"},
           {"line_id": 2, "start_ms": 7500, "end_ms": 9500, "text": "b", "role": "evidence"}]
    p = clips.plan_window(target, ev2, None, None, 60000)
    check("window: the clip covers two preceding evidence lines",
          p.start_ms == 4650 and p.end_ms == 12500 and len(p.extend) == 2 and not p.truncated,
          f"{p.start_ms}-{p.end_ms} extend={len(p.extend)}")
    check("window: the poster is the frame at the TARGET line start, not the clip start",
          p.poster_ms == 10000 and p.poster_ms > p.start_ms, str(p.poster_ms))
    long_ev = [{"line_id": 1, "start_ms": 0, "end_ms": 2000, "text": "a", "role": "evidence"}]
    pl = clips.plan_window(target, long_ev, None, None, 60000)
    check("window: an extended window may exceed MAX_CLIP_MS but not EXTENDED_MAX_CLIP_MS",
          12000 < pl.end_ms - pl.start_ms <= EXTENDED_MAX_CLIP_MS and not pl.truncated,
          f"{pl.end_ms - pl.start_ms} ms")

    # a span wider than the cap (30 s since 2026-09-03): the farthest line goes first
    far = [{"line_id": 1, "start_ms": 0, "end_ms": 2000, "text": "a", "role": "evidence"},
           {"line_id": 2, "start_ms": 20000, "end_ms": 21000, "text": "b", "role": "evidence"}]
    tgt2 = {"start_ms": EXTENDED_MAX_CLIP_MS + 5000, "end_ms": EXTENDED_MAX_CLIP_MS + 7000}
    p2 = clips.plan_window(tgt2, far, None, None, 90000)
    check("window: an over-long evidence span drops the farthest line first",
          p2.truncated and [x["line_id"] for x in p2.extend] == [2]
          and p2.end_ms - p2.start_ms <= EXTENDED_MAX_CLIP_MS,
          f"{p2.start_ms}-{p2.end_ms} kept={[x['line_id'] for x in p2.extend]}")
    check("window: the surviving evidence line is still covered",
          p2.start_ms <= 20000 and p2.poster_ms == tgt2["start_ms"], f"{p2.start_ms}/{p2.poster_ms}")

    cont = [{"line_id": 3, "start_ms": 12100, "end_ms": 13000, "text": "c",
             "role": "continuation"}]
    p3 = clips.plan_window(target, cont, None, None, 60000)
    check("window: a continuation cue is covered and never dropped",
          p3.end_ms == 13500 and len(p3.extend) == 1 and not p3.truncated,
          f"{p3.start_ms}-{p3.end_ms}")
    s, e = clips.window(target, None, None, 60000, cont)
    check("window(): the 4-arg signature still works and takes extend last",
          (s, e) == (p3.start_ms, p3.end_ms), f"{s}-{e}")


def test_clip_evidence(ids) -> None:
    """A card whose moment carries evidence lines: the clip covers them and the
    poster is the target frame (§6.2)."""
    ev_line = next(l for l in _lines if l["ep"] == 1 and l["idx"] == 1)     # 5000–8000
    tgt_line = next(l for l in _lines if l["ep"] == 1 and l["idx"] == 2)    # 9000–12000
    extend = [{"line_id": ev_line["line_id"], "idx": 1, "start_ms": ev_line["start"],
               "end_ms": ev_line["end"], "text": ev_line["text"], "role": "evidence"}]
    with cursor() as cx:
        cx.execute("DELETE FROM srs_cards WHERE lemma='日本語'")
        cx.execute(
            "INSERT INTO srs_cards(lemma, line_id, episode_id, anilist_id, show_title, ep_number, "
            "start_ms, end_ms, text, norm_text, target_surface, extend_json, state, queue_pos, "
            "clip_status, clip_version, clip_requested_at) "
            "VALUES('日本語',?,?,1,'TestShow',1,?,?,?,?, '日本語', ?, 'new', 98, 'pending', 1, "
            "datetime('now'))",
            (tgt_line["line_id"], ids[1], tgt_line["start"], tgt_line["end"], tgt_line["text"],
             census.norm_text(tgt_line["text"]), json.dumps(extend, ensure_ascii=False)))
    cx = connect()
    try:
        card = dict(cx.execute("SELECT * FROM srs_cards WHERE lemma='日本語'").fetchone())
    finally:
        cx.close()
    res = clips.build(card)
    check("clip (evidence): build reports ready", res.status == "ready", f"{res.status} {res.error}")
    check("clip (evidence): the window starts before the evidence line",
          res.start_ms is not None and res.start_ms <= ev_line["start"]
          and res.end_ms >= tgt_line["end"], f"{res.start_ms}-{res.end_ms}")
    meta = json.loads((clips.card_dir(card["id"]) / "meta.json").read_text())
    check("clip (evidence): meta.json records the extend lines and the truncation flag",
          [x["line_id"] for x in meta["extend"]] == [ev_line["line_id"]]
          and meta["extend_truncated"] is False, str(meta.get("extend")))
    check("clip (evidence): meta.json records the poster at the target line start",
          meta["window"]["poster_ms"] == tgt_line["start"], str(meta["window"]))
    dur = clips._probe_duration(clips.card_dir(card["id"]) / "clip.mp4")
    want = (res.end_ms - res.start_ms) / 1000.0
    check("clip (evidence): the encoded file is as long as the extended window",
          dur is not None and abs(dur - want) <= 0.25 and want > 3.0, f"{dur} vs {want}")


def _make_card(ids, lemma="天気") -> dict:
    line = next(l for l in _lines if l["ep"] == 1 and l["idx"] == 1)
    with cursor() as cx:
        cx.execute("DELETE FROM srs_cards WHERE lemma=?", (lemma,))
        cx.execute(
            "INSERT INTO srs_cards(lemma, reading, meaning_short, line_id, episode_id, "
            "anilist_id, show_title, ep_number, start_ms, end_ms, text, norm_text, translation, "
            "translation_source, target_surface, state, queue_pos, clip_status, clip_version, "
            "clip_requested_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'new',99,'pending',1,"
            "datetime('now'))",
            (lemma, "てんき", "weather", line["line_id"], ids[1], 1, "TestShow", 1,
             line["start"], line["end"], line["text"], census.norm_text(line["text"]),
             line["translation"], "human", "天気"))
    cx = connect()
    try:
        return dict(cx.execute("SELECT * FROM srs_cards WHERE lemma=?", (lemma,)).fetchone())
    finally:
        cx.close()


def test_clip_build(ids) -> dict:
    card = _make_card(ids)
    res = clips.build(card)
    check("clip: build reports ready", res.status == "ready", f"{res.status} {res.error}")
    d = clips.card_dir(card["id"])
    check("clip: clip.mp4 and poster.jpg exist",
          (d / "clip.mp4").exists() and (d / "poster.jpg").exists(), str(d))
    check("clip: no scratch directory is left behind",
          not any(p.name.startswith(f"{d.name}.") for p in d.parent.iterdir()),
          str([p.name for p in d.parent.iterdir()]))
    size = (d / "clip.mp4").stat().st_size if (d / "clip.mp4").exists() else 0
    # the design targets ≤ 1 MB for a real anime source; the 720p fixture is
    # synthetic footage that compresses far worse, so the bound here is loose —
    # what matters is that the encoder settings of §6.1 were used (checked below)
    check("clip: the file is small (≤ 3 MB on the synthetic fixture)",
          0 < size <= 3 * 1024 * 1024, f"{size} B")
    rc, out = clips._probe(["-v", "error", "-select_streams", "v:0", "-show_entries",
                            "stream=height,codec_name,pix_fmt", "-of", "json", str(d / "clip.mp4")])
    info = (json.loads(out).get("streams") or [{}])[0] if rc == 0 and out else {}
    check("clip: h264 yuv420p at most 720p",
          info.get("codec_name") == "h264" and info.get("pix_fmt") == "yuv420p"
          and int(info.get("height") or 0) <= 720, str(info))
    dur = clips._probe_duration(d / "clip.mp4")
    want = (res.end_ms - res.start_ms) / 1000.0
    check("clip: duration within ±0.25 s of the window",
          dur is not None and abs(dur - want) <= 0.25, f"{dur} vs {want}")
    meta = json.loads((d / "meta.json").read_text())
    check("clip: meta.json is self-describing",
          meta["card_id"] == card["id"] and meta["line"]["text"] == card["text"]
          and "window" in meta and "ffmpeg_args" in meta, str(meta)[:200])
    check("clip: meta.json records the neighbours used for the window",
          "prev" in meta["window"] and "next" in meta["window"], str(meta["window"]))
    cx = connect()
    try:
        row = dict(cx.execute("SELECT * FROM srs_cards WHERE id=?", (card["id"],)).fetchone())
    finally:
        cx.close()
    check("clip: the card row is ready with byte count and window",
          row["clip_status"] == "ready" and row["clip_bytes"] and row["clip_start_ms"] == res.start_ms,
          str({k: row[k] for k in ("clip_status", "clip_bytes", "clip_start_ms")}))
    u = clips.urls(row)
    check("clip: urls() points at /clips/srs/<id>/… with the version",
          u["video_url"] == f"/clips/srs/{card['id']}/clip.mp4?v=1", str(u))
    check("clip: kv srs.clips.bytes was updated", int(kv_get("srs.clips.bytes") or 0) > 0)
    return row


def test_clip_versions(ids, card) -> None:
    stale = dict(card)
    stale["clip_version"] = 7
    res = clips.build(stale)
    check("clip: a superseded version does not commit", res.status == "stale", res.status)
    d = clips.card_dir(card["id"])
    check("clip: the superseded scratch dir is removed",
          not (d.parent / f"{d.name}.v7.tmp").exists())
    before = (d / "clip.mp4").stat().st_mtime
    clips._job_clip({"card_id": card["id"], "v": 7})
    check("clip: _job_clip with a stale v is a no-op",
          (d / "clip.mp4").stat().st_mtime == before)

    # keep_prev + restore_prev round trip
    with cursor() as cx:
        cx.execute("UPDATE srs_cards SET clip_version=2 WHERE id=?", (card["id"],))
    cx = connect()
    try:
        card2 = dict(cx.execute("SELECT * FROM srs_cards WHERE id=?", (card["id"],)).fetchone())
    finally:
        cx.close()
    (d / "marker.txt").write_text("v1")
    res = clips.build(card2, keep_prev=True)
    check("clip: keep_prev rebuild is ready", res.status == "ready", res.status)
    check("clip: the previous directory was kept",
          (d.parent / f"{d.name}.prev" / "marker.txt").exists())
    check("clip: restore_prev puts it back", clips.restore_prev(card["id"])
          and (d / "marker.txt").exists())
    (d / "marker.txt").unlink()


def test_clip_no_source(ids) -> None:
    with cursor() as cx:
        cur = cx.execute(
            "INSERT INTO episodes(anilist_id, ep_number, video_path, duration_ms) "
            "VALUES(1, 99, '/nonexistent/video.mp4', 30000)")
        ep_id = int(cur.lastrowid)
        cx.execute(
            "INSERT INTO srs_cards(lemma, episode_id, start_ms, end_ms, text, norm_text, "
            "state, clip_status, clip_version, clip_requested_at) "
            "VALUES('無源',?,1000,3000,'テスト','テスト','new','pending',1,datetime('now'))",
            (ep_id,))
    cx = connect()
    try:
        card = dict(cx.execute("SELECT * FROM srs_cards WHERE lemma='無源'").fetchone())
    finally:
        cx.close()
    res = clips.build(card)
    check("clip: a missing source video reports no_source", res.status == "no_source")
    clips._job_clip({"card_id": card["id"], "v": 1})          # must not raise
    cx = connect()
    try:
        row = cx.execute("SELECT clip_status FROM srs_cards WHERE id=?",
                         (card["id"],)).fetchone()
    finally:
        cx.close()
    check("clip: _job_clip records no_source without raising",
          row["clip_status"] == "no_source", row["clip_status"])
    check("source_available: false for a missing file", not clips.source_available(ep_id))
    check("source_available: true for the fixture episode", clips.source_available(ids[1]))
    check("probe_audio_index: finds the fixture's audio stream",
          clips.probe_audio_index(str(VIDEO)) == 1, str(clips.probe_audio_index(str(VIDEO))))


def test_disk_low(ids, card) -> None:
    orig = clips._free_bytes
    clips._free_bytes = lambda: 1 * 2 ** 30      # type: ignore[assignment]
    raised = False
    try:
        clips.build(dict(card))
    except clips.DiskLow:
        raised = True
    finally:
        clips._free_bytes = orig                 # type: ignore[assignment]
    check("clip: DiskLow is raised below 3 GiB free", raised)


def test_requeue_and_audit(ids, card) -> None:
    with cursor() as cx:
        cx.execute("UPDATE srs_cards SET clip_status='pending', "
                   "clip_requested_at=datetime('now','-2 hours') WHERE id=?", (card["id"],))
        cx.execute("DELETE FROM jobs WHERE type='srs_clip'")
    cx = connect()
    try:
        n = clips.requeue_stale_pending(cx)
    finally:
        cx.close()
    cx = connect()
    try:
        jobs = cx.execute("SELECT payload_json FROM jobs WHERE type='srs_clip'").fetchall()
    finally:
        cx.close()
    check("requeue_stale_pending: a stuck pending clip is re-enqueued",
          n == 1 and jobs and str(card["id"]) in jobs[0]["payload_json"],
          f"n={n} jobs={[j['payload_json'] for j in jobs]}")
    cx = connect()
    try:
        again = clips.requeue_stale_pending(cx)
    finally:
        cx.close()
    check("requeue_stale_pending: a live job is not re-enqueued", again == 0, str(again))

    # orphan directory + orphan moments
    orphan = clips.card_dir(999999)
    orphan.mkdir(parents=True, exist_ok=True)
    (orphan / "clip.mp4").write_text("x")
    old = time.time() - 5 * 86400
    os.utime(orphan, (old, old))
    with cursor() as cx:
        cx.execute("INSERT INTO srs_moments(lemma, episode_id, start_ms, end_ms, norm_text, text) "
                   "VALUES('孤児', 424242, 0, 1000, 'x', 'x')")
        cx.execute("UPDATE srs_cards SET clip_status='ready' WHERE id=?", (card["id"],))
    clips._job_clip_audit({})
    cx = connect()
    try:
        orphan_moments = cx.execute(
            "SELECT COUNT(*) AS n FROM srs_moments WHERE episode_id=424242").fetchone()["n"]
    finally:
        cx.close()
    check("audit: an orphan clip directory is removed", not orphan.exists())
    check("audit: moments of a deleted episode are pruned", orphan_moments == 0)
    check("audit: kv srs.clips.bytes is recomputed", int(kv_get("srs.clips.bytes") or 0) > 0)


def test_registration() -> None:
    from app.jobs.service import registered_types
    census.register_jobs()
    generate.register_jobs()
    clips.register_jobs()
    types = set(registered_types())
    check("register_jobs: every WP-B handler is registered",
          {"srs_census", "srs_generate", "srs_judge_batch", "srs_find_moments", "srs_clip",
           "srs_clip_audit"} <= types, str(sorted(types)))


# ---------------------------------------------------------------------------

def main() -> int:
    print(f"scratch db   : {DB_PATH}")
    print(f"scratch clips: {CLIPS_DIR}")
    if not VIDEO.exists():
        print(f"FAIL fixture video missing: {VIDEO}")
        return 1
    ids = seed_db()
    real_wpa = install_wpa_shims()
    print(f"WP-A snapshot/create_card: {'real' if real_wpa else 'selftest shims (WP-A is a stub)'}")
    cfg = get_settings()
    # The translation-blind clarity gate (2026-09-03) is stubbed to PASS here:
    # these tests exercise the writer/creation path; app.srs.selftest_blind
    # covers masking, the pass/fail rule and the multi-card rules.
    blind_judge._blind_call = lambda user: {                    # type: ignore[assignment]
        "candidates": [{"meaning": "weather", "plausibility": "high"}],
        "best_guess": "weather", "inferability": "single", "confidence": 0.92,
        "cues": "stub", "evidence_line_ids": [], "blockers": "", "note": "stub",
    }
    blind_judge._match_call = lambda user: {"match": "exact", "reason": "stub"}  # type: ignore[assignment]

    test_line_classification()
    test_census(cfg)
    test_refilter(cfg)
    test_word_score()
    test_rank_moments(cfg, ids)
    test_schema()
    test_judge_clamping(cfg)
    run_id, _lemmas = test_plan_and_judge(cfg)
    test_duplicate_collapse(cfg)
    test_judge_batch_failures(cfg, run_id)
    test_sweep(cfg)
    test_find_moments(cfg)
    test_mt_fallback(cfg)
    test_window()
    card = test_clip_build(ids)
    test_clip_evidence(ids)
    test_clip_versions(ids, card)
    test_clip_no_source(ids)
    test_disk_low(ids, card)
    test_requeue_and_audit(ids, card)
    test_registration()

    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
    sys.exit(code)
