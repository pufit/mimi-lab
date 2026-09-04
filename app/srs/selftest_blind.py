#!/usr/bin/env python3
"""Self-test for the translation-blind clarity gate and multi-card words
(2026-09-03) — run:

    .venv/bin/python -m app.srs.selftest_blind

Scratch DB only (the environment is rewritten BEFORE any `app.*` import, as in
app/srs/selftest.py); the two network seams of `blind_judge` are replaced, so no
API call is made. Covers: `mask_surface`/`mask_text`, `franchise_key`, the
srs_cards UNIQUE→multi rebuild migration (rows, reviews and indexes survive),
`judge_moment` end to end on a fixture (masking of the target AND its repeat in
the previous line, pass/fail rule, evidence validation), `create_card`'s
sibling rules (≤3, different anime, idempotent per moment, manual bypass) and
the `known`/`reject` action semantics with siblings.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
TMP = tempfile.mkdtemp(prefix="srs_blind_selftest_")

os.environ.update(
    MIMI_LAB_DB=f"{TMP}/test.db",
    LIBRARY_DIR=f"{TMP}/lib",
    INBOX_DIR=f"{TMP}/inbox",
    CLIPS_DIR=f"{TMP}/clips",
    MIMI_LAB_TOKEN="selftest",
    SERVER_PUBLIC_URL="",
    ANTHROPIC_API_KEY="",
    MIGAKU_TOK_URL="http://127.0.0.1:9",
)

from app import db as _db                                   # noqa: E402
from app.db import connect, init_db                         # noqa: E402
from app.srs import blind_judge, service                    # noqa: E402
from app.srs.constants import MAX_CARDS_PER_LEMMA           # noqa: E402
from app.srs.franchise import franchise_key, same_franchise  # noqa: E402
from app.srs.snapshot import CardSpec, norm_text            # noqa: E402

_passed = 0
_failed = 0


def check(name: str, cond: bool, detail: str = "") -> bool:
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  ok   {name}")
    else:
        _failed += 1
        print(f"  FAIL {name}" + (f" — {detail}" if detail else ""))
    return bool(cond)


# ---------------------------------------------------------------------------
# pure functions
# ---------------------------------------------------------------------------

def test_masking() -> None:
    print("masking")
    M = blind_judge.MASK
    check("kanji-only surface fully masked", blind_judge.mask_surface("攻撃") == M)
    check("okurigana kept", blind_judge.mask_surface("救って") == M + "って")
    check("compound verb keeps trailing kana only", blind_judge.mask_surface("落ち着いて") == M + "いて")
    check("all-kana surface masked whole", blind_judge.mask_surface("せめて") == M)

    toks = [("なら", "なら"), ("中", "中"), ("に", "に"), ("入る", "入った"), ("俺", "俺"),
            ("たち", "たち"), ("に", "に"), ("は", "は"), ("もう", "もう"), ("攻撃", "攻撃"),
            ("する", "する"), ("必要", "必要"), ("は", "は"), ("ない", "ない")]
    text = "なら中に入った俺たちにはもう攻撃する必要はない"
    masked, n = blind_judge.mask_text(text, toks, "攻撃")
    check("token-exact rebuild masks the target", masked == f"なら中に入った俺たちにはもう{M}する必要はない" and n == 1, masked)
    masked2, n2 = blind_judge.mask_text("お師様の命令は“塔に近づくやつを攻撃しろ”だろ？",
                                        [("攻撃", "攻撃")], "攻撃")
    check("fallback replace masks the repeat in another line",
          masked2 == f"お師様の命令は“塔に近づくやつを{M}しろ”だろ？" and n2 == 1, masked2)
    masked3, n3 = blind_judge.mask_text("彼を救ってくれ", [("救う", "救って")], "救う")
    check("verb surface masked with okurigana kept", masked3 == f"彼を{M}ってくれ" and n3 == 1, masked3)
    masked4, n4 = blind_judge.mask_text("全然関係ない", [], "攻撃", target_surface="攻撃")
    check("no occurrence → n=0, text untouched", n4 == 0 and masked4 == "全然関係ない")
    masked5, n5 = blind_judge.mask_text("彼を救ってくれ", [], "救う", target_surface="救って")
    check("target_surface literal fallback", masked5 == f"彼を{M}ってくれ" and n5 == 1, masked5)


def test_franchise() -> None:
    print("franchise key")
    rz = franchise_key("Re:Example kara Hajimeru Monogatari")
    check("Re:Example keeps its colon", rz == "re:example kara hajimeru monogatari", rz)
    check("4th Season collapses", franchise_key("Re:Example kara Hajimeru Monogatari 4th Season") == rz)
    check("3rd Season collapses", franchise_key("Re:Example kara Hajimeru Monogatari 3rd Season") == rz)
    check("EXAMPLE subtitle dropped",
          franchise_key("EXAMPLE: Kami no Shiken-hen") == franchise_key("EXAMPLE") == "example")
    check("Example Slime 4th Season", franchise_key("Example Slime Nikki 4th Season") == "example slime nikki")
    check("Example 2nd Season", franchise_key("Example na Kimi to Boku 2nd Season") == "example na kimi to boku")
    check("plain titles unchanged", franchise_key("Example no Jouheki") == "example no jouheki")
    check("Rock ≠ Dream", not same_franchise("Example the Rock!", "Example Dream! It's Demo!!!!!"))
    check("empty title never matches", not same_franchise("", ""))


# ---------------------------------------------------------------------------
# migration
# ---------------------------------------------------------------------------

def test_migration() -> None:
    print("srs_cards UNIQUE(lemma) → multi-card rebuild")
    init_db()
    cx = connect()
    try:
        ddl = cx.execute("SELECT sql FROM sqlite_master WHERE name='srs_cards'").fetchone()["sql"]
        check("fresh schema has no UNIQUE on lemma", "NOT NULL UNIQUE" not in ddl)
        check("fresh schema: nothing to rebuild", _db._rebuild_srs_cards_multi(cx) is False)

        # simulate a pre-2026-09-03 database: recreate the table WITH the constraint
        m = re.search(r"CREATE TABLE IF NOT EXISTS srs_cards \((.*?)\n\);", _db.SCHEMA, re.S)
        body = re.sub(r"lemma(\s+)TEXT NOT NULL,", r"lemma\1TEXT NOT NULL UNIQUE,", m.group(1), count=1)
        cx.commit()
        cx.isolation_level = None
        cx.execute("PRAGMA foreign_keys=OFF")
        cx.execute("DROP TABLE srs_cards")
        cx.execute(f"CREATE TABLE srs_cards ({body}\n)")
        cx.execute("INSERT INTO srs_cards(id, lemma, text, norm_text, state, queue_pos) VALUES(1,'天気','今日はいい天気','今日はいい天気','new',1)")
        cx.execute("INSERT INTO srs_cards(id, lemma, text, norm_text, state, queue_pos) VALUES(2,'攻撃','攻撃する','攻撃する','new',2)")
        cx.execute("INSERT INTO srs_reviews(card_id, review_day, rating, state_before, state_after, before_json) "
                   "VALUES(1, '2026-09-03', 3, 'new', 'learning', '{}')")
        cx.execute("PRAGMA foreign_keys=ON")
        cx.isolation_level = ""
        ddl = cx.execute("SELECT sql FROM sqlite_master WHERE name='srs_cards'").fetchone()["sql"]
        check("fixture table carries UNIQUE", "NOT NULL UNIQUE" in ddl)

        did = _db._rebuild_srs_cards_multi(cx)
        ddl = cx.execute("SELECT sql FROM sqlite_master WHERE name='srs_cards'").fetchone()["sql"]
        check("rebuild ran", did is True)
        check("UNIQUE gone", "NOT NULL UNIQUE" not in ddl, ddl[:200])
        rows = cx.execute("SELECT id, lemma FROM srs_cards ORDER BY id").fetchall()
        check("rows survive with ids", [(r["id"], r["lemma"]) for r in rows] == [(1, "天気"), (2, "攻撃")])
        check("review history survives",
              cx.execute("SELECT COUNT(*) FROM srs_reviews").fetchone()[0] == 1)
        idx = {r["name"] for r in cx.execute("PRAGMA index_list(srs_cards)")}
        check("indexes recreated", {"idx_srs_cards_stack", "idx_srs_cards_lemma",
                                    "idx_srs_cards_lemma_moment"} <= idx, str(idx))
        check("foreign keys re-enabled", cx.execute("PRAGMA foreign_keys").fetchone()[0] == 1)
        check("second run is a no-op", _db._rebuild_srs_cards_multi(cx) is False)
        cx.execute("INSERT INTO srs_cards(id, lemma, text, norm_text, state, queue_pos, episode_id) "
                   "VALUES(3,'攻撃','別の攻撃','別の攻撃','new',3, 7)")
        check("a second card of the same lemma is now allowed",
              cx.execute("SELECT COUNT(*) FROM srs_cards WHERE lemma='攻撃'").fetchone()[0] == 2)
        cx.execute("DELETE FROM srs_cards")
        cx.execute("DELETE FROM srs_reviews")
        cx.commit()
    finally:
        cx.close()


# ---------------------------------------------------------------------------
# fixture for judge_moment + service rules
# ---------------------------------------------------------------------------

def seed_fixture() -> dict:
    cx = connect()
    try:
        cx.commit()
        cx.isolation_level = None
        cx.execute("PRAGMA foreign_keys=OFF")          # subtitle_id → no subtitles row needed
        cx.execute("INSERT OR IGNORE INTO titles(anilist_id, romaji) VALUES(900046, 'Re:Example kara Hajimeru Monogatari 4th Season')")
        cx.execute("INSERT OR IGNORE INTO titles(anilist_id, romaji) VALUES(900355, 'Re:Example kara Hajimeru Monogatari')")
        cx.execute("INSERT OR IGNORE INTO titles(anilist_id, romaji) VALUES(900801, 'EXAMPLE')")
        cx.execute("INSERT OR IGNORE INTO titles(anilist_id, romaji) VALUES(900003, 'Example the Rock!')")
        for eid, aid in ((1, 900046), (2, 900355), (3, 900801), (4, 900003)):
            cx.execute("INSERT OR IGNORE INTO episodes(id, anilist_id, ep_number, duration_ms) VALUES(?,?,4,1440000)", (eid, aid))
        lines = [
            (101, 1, 10, 740000, 742000, "お師様の命令は“塔に近づくやつを攻撃しろ”だろ？", "Master's order was 'attack anyone approaching the tower', right?"),
            (102, 1, 11, 752504, 756466, "なら中に入った俺たちにはもう攻撃する必要はない", "So now that we're inside, there's no need to attack us."),
            (103, 1, 12, 757000, 759000, "そういうことっス", "That's right."),
            # a two-line cue: rows 104/105 share identical timings (the ingest
            # stores each line of a cue as its own row); 106 is a long sign
            (104, 1, 13, 760000, 763000, "なるほどね。魔法術式が重なって", "I get it. The spells layered on it"),
            (105, 1, 14, 760000, 763000, "より攻撃に見えてるんだ。", "make it look even more like an attack."),
            (106, 1, 15, 700000, 800000, "王都の門", "The Capital Gate"),
        ]
        for lid, ep, idx, s, e, text, tr in lines:
            cx.execute("INSERT OR IGNORE INTO subtitle_lines(id, subtitle_id, episode_id, idx, start_ms, end_ms, text, translation) "
                       "VALUES(?,1,?,?,?,?,?,?)", (lid, ep, idx, s, e, text, tr))
        toks = {
            101: [("お", "お"), ("師", "師"), ("様", "様"), ("の", "の"), ("命令", "命令"), ("は", "は"),
                  ("塔", "塔"), ("に", "に"), ("近づく", "近づく"), ("やつ", "やつ"), ("を", "を"),
                  ("攻撃", "攻撃"), ("する", "しろ"), ("だろ", "だろ")],
            102: [("なら", "なら"), ("中", "中"), ("に", "に"), ("入る", "入った"), ("俺", "俺"),
                  ("たち", "たち"), ("に", "に"), ("は", "は"), ("もう", "もう"), ("攻撃", "攻撃"),
                  ("する", "する"), ("必要", "必要"), ("は", "は"), ("ない", "ない")],
            103: [("そういう", "そういう"), ("こと", "こと"), ("っス", "っス")],
            104: [("なるほど", "なるほど"), ("ね", "ね"), ("。", "。"), ("魔法", "魔法"), ("術式", "術式"),
                  ("が", "が"), ("重なる", "重なって")],
            105: [("より", "より"), ("攻撃", "攻撃"), ("に", "に"), ("見える", "見えてる"), ("ん", "ん"),
                  ("だ", "だ"), ("。", "。")],
            106: [("王都", "王都"), ("の", "の"), ("門", "門")],
        }
        cx.execute("DELETE FROM line_lemmas WHERE line_id IN (101,102,103,104,105,106)")
        for lid, pairs in toks.items():
            for lemma, surface in pairs:
                cx.execute("INSERT INTO line_lemmas(line_id, episode_id, lemma, reading, pos, surface, token_source) "
                           "VALUES(?,1,?,?,?,?,'migaku-local')", (lid, lemma, lemma, "", surface))
        cx.execute("PRAGMA foreign_keys=ON")
        cx.isolation_level = ""
        cx.commit()
    finally:
        cx.close()
    return {"target": 102, "prev": 101, "cue_a": 104, "cue_b": 105, "sign": 106}


def test_neighbours_same_cue(ids: dict) -> None:
    """2026-09-03: the other half of a two-line cue is a neighbour (and part of
    the moment), a long-running sign is not; overlapping cues keep cue order."""
    print("dialogue_neighbours / same-cue lines")
    from app.srs.snapshot import build_extend, dialogue_neighbours, same_cue_lines
    cx = connect()
    try:
        line_b = {"line_id": ids["cue_b"], "episode_id": 1, "idx": 14, "start_ms": 760000, "end_ms": 763000,
                  "text": "より攻撃に見えてるんだ。"}
        before, after = dialogue_neighbours(cx, line_b, 3)
        b_ids = [c["line_id"] for c in before]
        a_ids = [c["line_id"] for c in after]
        check("same-span sibling row is the nearest 'before' neighbour", b_ids[-1:] == [ids["cue_a"]], str(b_ids))
        check("earlier cues still precede it in order", b_ids == [102, 103, 104], str(b_ids))
        check("a long sign spanning the target is not a neighbour", ids["sign"] not in b_ids + a_ids, str(a_ids))
        line_a = {"line_id": ids["cue_a"], "episode_id": 1, "idx": 13, "start_ms": 760000, "end_ms": 763000,
                  "text": "なるほどね。魔法術式が重なって"}
        before_a, after_a = dialogue_neighbours(cx, line_a, 3)
        check("from the first half, the second half is the nearest 'after'",
              [c["line_id"] for c in after_a][:1] == [ids["cue_b"]], str([c["line_id"] for c in after_a]))
        # a dict without idx (generate._context_for style) still resolves row order
        before_noidx, _a = dialogue_neighbours(cx, {"line_id": ids["cue_b"], "episode_id": 1,
                                                    "start_ms": 760000, "end_ms": 763000}, 2)
        check("idx is looked up when the caller omits it", [c["line_id"] for c in before_noidx] == [103, 104],
              str([c["line_id"] for c in before_noidx]))
        check("same_cue_lines finds the sibling", same_cue_lines(cx, line_b) == [ids["cue_a"]])
        entries, truncated = build_extend(cx, line_b, [])
        check("build_extend always attaches the same-cue sibling as evidence",
              [(e["line_id"], e["role"]) for e in entries] == [(ids["cue_a"], "evidence")] and not truncated,
              str(entries))
        entries2, _t = build_extend(cx, line_b, [ids["cue_a"], 102])
        check("…without duplicating it when the judge also cites it",
              [e["line_id"] for e in entries2] == [102, ids["cue_a"]] or [e["line_id"] for e in entries2] == [ids["cue_a"], 102],
              str([e["line_id"] for e in entries2]))
    finally:
        cx.close()


def test_expand_context(ids: dict) -> None:
    """The judge may ask for a wider window once (need_more_context)."""
    print("adaptive context window")
    calls: list[str] = []

    def blind(user: str):
        calls.append(user)
        if len(calls) == 1:
            return {"candidates": [{"meaning": "attack", "plausibility": "medium"}], "best_guess": "attack",
                    "inferability": "broad", "confidence": 0.4, "cues": "", "evidence_line_ids": [],
                    "blockers": "", "need_more_context": True, "note": ""}
        return {"candidates": [{"meaning": "attack", "plausibility": "high"}], "best_guess": "attack",
                "inferability": "single", "confidence": 0.9, "cues": "x", "evidence_line_ids": [ids["cue_a"]],
                "blockers": "", "need_more_context": False, "note": ""}

    blind_judge._blind_call = blind                     # type: ignore[assignment]
    blind_judge._match_call = lambda user: {"match": "exact", "reason": "stub"}  # type: ignore[assignment]
    cx = connect()
    try:
        v = blind_judge.judge_moment(cx, lemma="攻撃", line_id=ids["cue_b"], reference="attack")
        check("second pass ran with the wider window", v.get("context_passes") == 2 and len(calls) == 2, str(v.get("window")))
        check("wider verdict is returned and passes", v["passed"] is True and v["first_pass"]["inferability"] == "broad")
        check("first-pass prompt is shorter than the second", len(calls[0]) <= len(calls[1]))
        check("same-cue half is shown to the judge", "なるほどね" in calls[0])
        calls.clear()
        v2 = blind_judge.judge_moment(cx, lemma="攻撃", line_id=ids["cue_b"], reference="attack", allow_expand=False)
        check("allow_expand=False stays at one pass", v2.get("context_passes") == 1 and len(calls) == 1 and v2["passed"] is False)
    finally:
        cx.close()


def test_judge_moment(ids: dict) -> None:
    print("judge_moment (stubbed network)")
    seen: dict = {}

    def blind_broad(user: str):
        seen["user"] = user
        return {"candidates": [{"meaning": "attack", "plausibility": "medium"},
                               {"meaning": "kill", "plausibility": "medium"},
                               {"meaning": "stop", "plausibility": "medium"}],
                "best_guess": "attack", "inferability": "broad", "confidence": 0.45,
                "cues": "an order concerning people approaching the tower", "evidence_line_ids": [101, 999],
                "blockers": "塔", "note": ""}

    blind_judge._blind_call = blind_broad              # type: ignore[assignment]
    blind_judge._match_call = lambda user: {"match": "exact", "reason": "stub"}  # type: ignore[assignment]
    cx = connect()
    try:
        v = blind_judge.judge_moment(cx, lemma="攻撃", line_id=ids["target"], target_surface="攻撃",
                                     pos="noun", reference="attack; assault", translation="…no need to attack us.")
        M = blind_judge.MASK
        check("target masked in the target line", v["masked_text"] == f"なら中に入った俺たちにはもう{M}する必要はない", v["masked_text"])
        prev = next((c for c in v["context"] if c["line_id"] == ids["prev"]), None)
        check("repeat masked in the previous line", prev is not None and prev["masked"] == f"お師様の命令は“塔に近づくやつを{M}しろ”だろ？", str(prev))
        check("prompt carries no translation", "attack" not in seen["user"].lower() and "no need" not in seen["user"].lower())
        check("prompt shows the masked lines", M in seen["user"] and "▶" in seen["user"])
        check("broad inference fails even when the guess matches", v["passed"] is False and v["inferability"] == "broad")
        check("evidence ids validated against shown lines", v["evidence_line_ids"] == [101], str(v["evidence_line_ids"]))
        check("confidence clamped/rounded", v["confidence"] == 0.45)

        blind_judge._blind_call = lambda user: {           # type: ignore[assignment]
            "candidates": [{"meaning": "attack", "plausibility": "high"}], "best_guess": "attack",
            "inferability": "single", "confidence": 0.93, "cues": "x", "evidence_line_ids": [],
            "blockers": "", "note": ""}
        v2 = blind_judge.judge_moment(cx, lemma="攻撃", line_id=ids["target"], reference="attack")
        check("single + exact match passes", v2["passed"] is True and v2["match"] == "exact")
        blind_judge._match_call = lambda user: {"match": "wrong", "reason": "stub"}  # type: ignore[assignment]
        v3 = blind_judge.judge_moment(cx, lemma="攻撃", line_id=ids["target"], reference="attack")
        check("confident but wrong guess fails", v3["passed"] is False and v3["match"] == "wrong")
        blind_judge._match_call = lambda user: {"match": "exact", "reason": "stub"}  # type: ignore[assignment]
        blind_judge._blind_call = lambda user: {           # type: ignore[assignment]
            "candidates": [], "best_guess": "attack", "inferability": "single", "confidence": 0.4,
            "cues": "", "evidence_line_ids": [], "blockers": "", "note": ""}
        v4 = blind_judge.judge_moment(cx, lemma="攻撃", line_id=ids["target"], reference="attack")
        check("confidence below the floor fails", v4["passed"] is False)
        check("passes() recomputes from fields (threshold change safe)",
              blind_judge.passes({"inferability": "narrow", "confidence": 0.55, "match": "exact"}) is True
              and blind_judge.passes({"inferability": "broad", "confidence": 0.9, "match": "exact"}) is False
              and blind_judge.passes({"inferability": "single", "confidence": 0.9, "match": "wrong"}) is False
              and blind_judge.passes({"error": "no_answer", "inferability": "single", "confidence": 0.9, "match": "exact"}) is False)
        v5 = blind_judge.judge_moment(cx, lemma="存在しない", line_id=ids["target"], reference="x")
        check("lemma absent from the line → target_not_found", v5["error"] == "target_not_found")
        v6 = blind_judge.judge_moment(cx, lemma="攻撃", line_id=987654, reference="x")
        check("unknown line → line_gone", v6["error"] == "line_gone")
        blind_judge._blind_call = lambda user: None        # type: ignore[assignment]
        v7 = blind_judge.judge_moment(cx, lemma="攻撃", line_id=ids["target"], reference="x")
        check("LLM failure → no_answer, not passed", v7["error"] == "no_answer" and v7["passed"] is False)
        check("why_clear text names cues, not the translation",
              blind_judge.why_clear_text({"inferability": "single", "confidence": 0.9, "cues": "「攻撃しろ」 is the order"}).startswith("Inferable without the translation (single, 0.90): 「攻撃しろ」"))
    finally:
        cx.close()


def _spec(lemma: str, episode_id: int, show: str, text: str, anilist_id: int = 0) -> CardSpec:
    return CardSpec(lemma=lemma, reading="こうげき", pos="noun", meaning_short="attack",
                    source="curated-initial", line_id=None, episode_id=episode_id, anilist_id=anilist_id,
                    show_title=show, ep_number=1, start_ms=1000, end_ms=2000, text=text,
                    norm_text=norm_text(text), target_surface=lemma)


def test_multi_card_rules() -> None:
    print("multi-card service rules")
    lemma = "攻撃"
    a = service.create_card(_spec(lemma, 1, "Re:Example kara Hajimeru Monogatari 4th Season", "攻撃する必要はない"))
    a_again = service.create_card(_spec(lemma, 1, "Re:Example kara Hajimeru Monogatari 4th Season", "攻撃する必要はない"))
    check("same moment is idempotent", a == a_again)
    try:
        service.create_card(_spec(lemma, 2, "Re:Example kara Hajimeru Monogatari", "攻撃だ！"))
        check("same franchise (other season) rejected", False, "no raise")
    except service.SrsConflict as e:
        check("same franchise (other season) rejected", "anime" in str(e))
    b = service.create_card(_spec(lemma, 3, "EXAMPLE", "魔法攻撃！"))
    c = service.create_card(_spec(lemma, 4, "Example the Rock!", "攻撃的なギター"))
    check("three cards from three anime", len({a, b, c}) == 3)
    try:
        service.create_card(_spec(lemma, 2, "Example no Jouheki", "攻撃！！"))
        check("4th card rejected", False, "no raise")
    except service.SrsConflict as e:
        check("4th card rejected", "3 cards" in str(e))
    manual = service.create_card(_spec("別語", 1, "EXAMPLE", "別語だ"))
    try:
        service.create_card(_spec("別語", 3, "EXAMPLE: Kami no Shiken-hen", "別語ですね"),
                            allow_same_franchise=True)
        check("allow_same_franchise bypasses the anime rule", True)
    except service.SrsConflict as e:
        check("allow_same_franchise bypasses the anime rule", False, str(e))
    cx = connect()
    try:
        w = cx.execute("SELECT card_id, judge_status FROM srs_words WHERE lemma=?", (lemma,)).fetchone()
        check("srs_words.card_id stays the primary", int(w["card_id"]) == a and w["judge_status"] == "accepted")
        live = service.live_cards_of(cx, lemma)
        check("live_cards_of counts the three", [x["id"] for x in live] == [a, b, c])
        positions = [r["queue_pos"] for r in cx.execute(
            "SELECT queue_pos FROM srs_cards WHERE lemma=? ORDER BY id", (lemma,))]
        check("siblings appended in stack order", positions == sorted(positions), str(positions))
    finally:
        cx.close()

    # the card detail lists the word's other cards; a moment another card shows
    # is flagged and cannot be swapped onto (2026-09-03)
    detail = service.card_detail(a)
    check("card_detail.siblings lists the other cards of the word",
          [s.id for s in detail.siblings] == [b, c], str([s.id for s in detail.siblings]))
    check("sibling rows carry show/state/text",
          detail.siblings[0].show_title == "EXAMPLE" and detail.siblings[0].state == "new"
          and detail.siblings[0].text == "魔法攻撃！")
    with service._txn() as cx:
        mid = service._upsert_moment(
            cx, lemma, {"line_id": None, "episode_id": 3, "idx": None, "start_ms": 1000, "end_ms": 2000,
                        "norm_text": norm_text("魔法攻撃！"), "text": "魔法攻撃！", "translation": None,
                        "translation_source": None, "target_surface": "攻撃"},
            verdict="user", accepted=1, clarity=None, now=service._now())
    detail = service.card_detail(a)
    mm = next((m for m in detail.moments if m.id == mid), None)
    check("a moment shown by another card is marked used_by that card",
          mm is not None and mm.used_by_card_id == b and mm.used_by_state == "new",
          str(mm and (mm.used_by_card_id, mm.used_by_state)))
    try:
        with service._txn() as cx:
            service.swap_moment(cx, a, mid)
        check("swap onto another card's moment is refused (409)", False, "no raise")
    except service.SrsConflict as e:
        check("swap onto another card's moment is refused (409)",
              "already card" in str(e) and (e.payload or {}).get("card_id") == b, str(e))

    # reject one sibling: the word stays accepted while others are live
    service.card_action(c, "reject")
    cx = connect()
    try:
        w = cx.execute("SELECT judge_status FROM srs_words WHERE lemma=?", (lemma,)).fetchone()
        check("rejecting one sibling keeps the word accepted", w["judge_status"] == "accepted", str(dict(w)))
        st = cx.execute("SELECT state FROM srs_cards WHERE id=?", (c,)).fetchone()["state"]
        check("the rejected card itself is rejected", st == "rejected")
    finally:
        cx.close()
    # known on one card → siblings follow
    service.card_action(a, "known")
    cx = connect()
    try:
        rows = {r["id"]: (r["state"], r["known_source"]) for r in cx.execute(
            "SELECT id, state, known_source FROM srs_cards WHERE lemma=?", (lemma,))}
        check("known propagates to the active sibling", rows[b] == ("known", "sibling"), str(rows))
        check("known card keeps known_source=user", rows[a] == ("known", "user"))
        check("rejected sibling untouched by known", rows[c][0] == "rejected")
        check("stack invariant: no queue_pos outside new",
              cx.execute("SELECT COUNT(*) FROM srs_cards WHERE state<>'new' AND queue_pos IS NOT NULL").fetchone()[0] == 0)
    finally:
        cx.close()
    # reject the last live card of a word → the word is rejected
    d = service.create_card(_spec("単独", 3, "EXAMPLE", "単独行動"))
    service.card_action(d, "reject")
    cx = connect()
    try:
        w = cx.execute("SELECT judge_status FROM srs_words WHERE lemma='単独'").fetchone()
        check("rejecting the only card rejects the word", w["judge_status"] == "rejected")
    finally:
        cx.close()


def main() -> int:
    try:
        test_masking()
        test_franchise()
        test_migration()
        ids = seed_fixture()
        test_neighbours_same_cue(ids)
        test_judge_moment(ids)
        test_expand_context(ids)
        test_multi_card_rules()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
