"""Self-test for app.subs (+ the learn pipeline it drives).

Run:  .venv/bin/python -m app.subs.selftest

Does an end-to-end ingest of the TestShow sub against a throwaway fake
title/episode, then exercises comprehension_aligned, moments_search, and
extract_clip. An optional live jimaku lookup runs only when both
MIMI_TEST_JIMAKU_ANILIST_ID and MIMI_TEST_JIMAKU_EPISODE are set. Cleans
up all fake rows at the end. Prints PASS/FAIL per check + a final summary.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from app.config import settings
from app.db import connect, cursor, init_db
from app.learn import service as L
from app.subs import service as S

FAKE_ANILIST = 999000001  # high fake id, unlikely to collide
LIB = Path(__file__).resolve().parent.parent.parent / "lib" / "TestShow"
VIDEO = LIB / "TestShow - S01E01.mp4"
SUB = LIB / "TestShow - S01E01.ja.srt"

_fails: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        _fails.append(name)


def _cleanup(episode_id: int | None) -> None:
    with cursor() as cx:
        if episode_id is not None:
            line_ids = [r["id"] for r in cx.execute(
                "SELECT id FROM subtitle_lines WHERE episode_id=?", (episode_id,)).fetchall()]
            if line_ids:
                q = ",".join("?" * len(line_ids))
                cx.execute(f"DELETE FROM subtitle_fts WHERE line_id IN ({q})", line_ids)
            cx.execute("DELETE FROM line_lemmas WHERE episode_id=?", (episode_id,))
            cx.execute("DELETE FROM subtitle_lines WHERE episode_id=?", (episode_id,))
            cx.execute("DELETE FROM subtitles WHERE episode_id=?", (episode_id,))
        cx.execute("DELETE FROM episodes WHERE anilist_id=?", (FAKE_ANILIST,))
        cx.execute("DELETE FROM titles WHERE anilist_id=?", (FAKE_ANILIST,))
        cx.execute("DELETE FROM known_words WHERE source='selftest'", ())
    # clean any clip files we made
    for p in settings.clips_dir.glob("line_*"):
        try:
            p.unlink()
        except Exception:
            pass
    # clean the throwaway sub copy the ingest test worked off
    import tempfile
    try:
        (Path(tempfile.gettempdir()) / f"migaku_selftest_{FAKE_ANILIST}.ja.srt").unlink(missing_ok=True)
    except Exception:
        pass


def _check_caption_cleaner() -> None:
    """Unit checks for the Japanese broadcast-caption cleaner (general, not
    show-specific): annotations stripped, real Japanese + quotes preserved."""
    print("--- clean_ja_caption ---")
    cases = [
        # (raw, expected)
        ("（話者）\\N《僕は　頭がおかしい》", "僕は頭がおかしい"),          # speaker + narration + fw-space
        ("《この均衡は\\N危ういという事を→", "この均衡は危ういという事を"),      # narration open + continuation →
        ("今は　誰も知らない》", "今は誰も知らない"),                        # narration close
        ("（カッターナイフで切り裂く音）", ""),                                  # pure SFX caption -> dropped
        ("（登場人物(とうじょうじんぶつ)）", ""),                              # speaker label w/ inner furigana
        ("私(わたし)は学生(がくせい)です", "私は学生です"),                     # inline furigana in dialogue
        ("次回 「例の物語 特別編」", "次回「例の物語特別編」"),                 # 「」 quotes KEPT
        ("『例の物語』を読む", "『例の物語』を読む"),                          # 『』 quotes KEPT
        ("♪～", ""),                                                            # instrumental sting -> dropped
        ("♪ 明日への扉を開けて ♪", "明日への扉を開けて"),                       # song lyric kept, notes gone
        ("（２人）バ●ス", "バ●ス"),                                            # ● censoring KEPT
        ("〈僕らはインターン先を探す〉", "僕らはインターン先を探す"),           # single-angle narration
    ]
    for raw, want in cases:
        got = S.clean_ja_caption(raw)
        check(f"clean {raw[:22]!r}", got == want, f"got {got!r} want {want!r}")
    # never strips ordinary punctuation / quotes / censoring chars
    kept = S.clean_ja_caption("「ええ」と 学習者(がくしゅうしゃ)は言った…")
    check("quotes+ellipsis preserved, furigana stripped",
          kept == "「ええ」と学習者は言った…", kept)


def _check_alignment_scoring() -> None:
    """The objective aligner picks by cue-onset agreement with a trusted
    reference — verify it scores an aligned sub high and a shifted one low (this
    is what lets alignment be self-verifying instead of blindly trusted)."""
    import tempfile
    import pysubs2
    print("--- alignment scoring ---")
    tmp = Path(tempfile.gettempdir())
    ref = pysubs2.SSAFile()
    for t in (1000, 5000, 9000, 13000):
        ref.append(pysubs2.SSAEvent(start=t, end=t + 2000, text="ref"))
    refp = tmp / "_mig_ref.srt"; ref.save(str(refp))

    aligned = pysubs2.SSAFile()          # same onsets ± small jitter
    for t in (1200, 5100, 8800, 13200):
        aligned.append(pysubs2.SSAEvent(start=t, end=t + 2000, text="ja"))
    okp = tmp / "_mig_ja_ok.srt"; aligned.save(str(okp))

    shifted = pysubs2.SSAFile()           # everything +7s (mistimed)
    for t in (8000, 12000, 16000, 20000):
        shifted.append(pysubs2.SSAEvent(start=t, end=t + 2000, text="ja"))
    badp = tmp / "_mig_ja_bad.srt"; shifted.save(str(badp))

    good = S._onset_agreement(okp, refp)
    bad = S._onset_agreement(badp, refp)
    check("onset-agreement: aligned scores high", good is not None and good >= 0.99, f"{good}")
    check("onset-agreement: mistimed scores low", bad is not None and bad <= 0.25, f"{bad}")
    check("onset-agreement discriminates aligned vs mistimed",
          good is not None and bad is not None and good > bad, f"{good} > {bad}")
    for p in (refp, okp, badp):
        p.unlink(missing_ok=True)


def _check_reference_hygiene() -> None:
    """A typeset fansub track (karaoke frames, vector-drawing commands, per-glyph
    credit animations) must reduce to its dialogue cues before it is scored
    against or aligned to — otherwise onset agreement rewards piling JA cues
    onto OP/ED storms (the Re:Zero S3E01 mangling, 2026-07-21)."""
    import tempfile
    import pysubs2
    print("--- reference hygiene ---")
    tmp = Path(tempfile.gettempdir())

    ref = pysubs2.SSAFile()
    dialogue = [(1000, "Hello there."), (5000, "Are you okay?"),
                (60000, "Line three."), (65000, "Line four."), (70000, "Line five."),
                (96500, "Zone-adjacent line.")]
    for t, txt in dialogue:
        ref.append(pysubs2.SSAEvent(start=t, end=t + 2000, text=txt))
    # rolling same-text re-emission (one dialogue line as 3 frames)
    for i in range(3):
        ref.append(pysubs2.SSAEvent(start=9000 + i * 100, end=9100 + i * 100, text="Rolling line."))
    # karaoke frames: sub-300ms, growing text
    for i in range(40):
        ref.append(pysubs2.SSAEvent(start=20000 + i * 90, end=20090 + i * 90, text="mirai wo"[: 1 + i % 8]))
    # vector-drawing typesetting
    for i in range(30):
        ref.append(pysubs2.SSAEvent(start=30000 + i * 40, end=30500 + i * 40, text="m 83.21 43.75 l 79 63.45 58.67"))
    # per-glyph credit animation: healthy durations, single-char texts, huge density
    for i in range(200):
        ref.append(pysubs2.SSAEvent(start=40000 + (i % 4) * 250, end=40500 + (i % 4) * 250, text=str(i % 10)))
    # multi-char storm: passes every per-cue filter, caught only by onset density
    for i in range(120):
        ref.append(pysubs2.SSAEvent(start=50000 + i * 30, end=50600 + i * 30, text=f"credit {i}"))
    # lyric line rolled as many frames: merges into a healthy-looking 2s cue,
    # only the fragment count gives it away
    for i in range(20):
        ref.append(pysubs2.SSAEvent(start=80000 + i * 100, end=80100 + i * 100, text="lyric line rolling out."))
    # OP-style typeset zone: song translations (healthy per-cue shape) inside a
    # region saturated with drawing frames — only the junk zone catches them
    for i in range(36):
        ref.append(pysubs2.SSAEvent(start=90000 + i * 80, end=90080 + i * 80, text="m 14.87 18.48 l 31.56 19.2 34.5"))
    ref.append(pysubs2.SSAEvent(start=90500, end=92000, text="kotoba ga hito wo yuitsukeru you ni"))
    ref.append(pysubs2.SSAEvent(start=92100, end=93600, text="As their words tie people to each other,"))
    refp = tmp / "_mig_ref_typeset.srt"; ref.save(str(refp))

    ev = S._dialogue_events(refp)
    starts = sorted(s for s, _e, _t in ev)
    dlg = [t for t, _ in dialogue]
    check("hygiene keeps every dialogue cue (incl. zone-adjacent)",
          all(any(abs(s - t) <= 1 for s in starts) for t in dlg), f"{starts}")
    check("hygiene merges rolling same-text run to one cue",
          sum(1 for s in starts if 8900 <= s <= 9300) == 1)
    check("hygiene drops many-fragment rolled lyric line",
          not any(79000 <= s <= 83000 for s in starts))
    check("hygiene drops song translations inside a typeset junk zone",
          not any(89000 <= s <= 94000 for s in starts))
    check("hygiene drops karaoke/drawing/glyph/density storms",
          len(ev) <= len(dlg) + 1, f"{len(ev)} cues survive")

    # a perfectly-timed JA sub must now outscore one shoved onto the storms
    ja_ok = pysubs2.SSAFile()
    for t, _ in dialogue:
        ja_ok.append(pysubs2.SSAEvent(start=t + 150, end=t + 2000, text="日本語"))
    okp = tmp / "_mig_ja_dlg.srt"; ja_ok.save(str(okp))
    ja_storm = pysubs2.SSAFile()
    for t in (20000, 30000, 40000, 50000, 52000):
        ja_storm.append(pysubs2.SSAEvent(start=t, end=t + 2000, text="日本語"))
    stormp = tmp / "_mig_ja_storm.srt"; ja_storm.save(str(stormp))
    sc_ok = S._onset_agreement(okp, refp)
    sc_storm = S._onset_agreement(stormp, refp)
    check("hygiene: correct sub outscores storm-piled sub",
          sc_ok is not None and sc_storm is not None and sc_ok > sc_storm,
          f"{sc_ok} > {sc_storm}")

    # too-few-dialogue reference is not trusted at all
    tiny = pysubs2.SSAFile()
    for i in range(10):
        tiny.append(pysubs2.SSAEvent(start=1000 + i * 2000, end=2500 + i * 2000, text=f"line {i}"))
    tinyp = tmp / "_mig_ref_tiny.srt"; tiny.save(str(tinyp))
    check("hygiene: sparse track below anchor minimum",
          len(S._dialogue_events(tinyp)) < S._REF_MIN_DIALOGUE_CUES)
    check("hygiene: dialogue-ref writer refuses sparse track",
          S._write_dialogue_ref(tinyp, tmp / "_mig_ref_tiny_out.srt") is None)

    for p in (refp, okp, stormp, tinyp, tmp / "_mig_ref_tiny_out.srt"):
        p.unlink(missing_ok=True)


def _check_track_sanity() -> None:
    """The display-sanity gate must accept normal tracks (incl. occasional
    dual-speaker overlap) and reject a structurally-broken one — the same
    dialogue present twice at conflicting timings (two lines on screen at once,
    one mistimed). This is the guard that keeps a doubled/folded track from ever
    being served or trusted as an alignment reference."""
    import tempfile
    import pysubs2
    print("--- track sanity (overlap gate) ---")
    tmp = Path(tempfile.gettempdir())

    def _write(name: str, cues: list[tuple[int, int]]) -> Path:
        f = pysubs2.SSAFile()
        for i, (s, e) in enumerate(cues):
            f.append(pysubs2.SSAEvent(start=s, end=e, text=f"line {i}"))
        p = tmp / name
        f.save(str(p))
        return p

    clean = _write("_mig_sane_ok.srt", [(i * 3000, i * 3000 + 2000) for i in range(20)])
    # one dual-speaker overlap in 20 cues — normal, must pass
    dual = _write("_mig_sane_dual.srt",
                  [(i * 3000, i * 3000 + 2000) for i in range(20)] + [(3200, 4800)])
    # the broken shape: every cue also present shifted +1.5s (original ∪ copy)
    doubled = _write("_mig_sane_doubled.srt",
                     [(i * 3000, i * 3000 + 2000) for i in range(20)]
                     + [(i * 3000 + 1500, i * 3000 + 3500) for i in range(20)])

    f_clean = S.sub_overlap_fraction(clean)
    f_dual = S.sub_overlap_fraction(dual)
    f_doubled = S.sub_overlap_fraction(doubled)
    check("overlap: clean track ~0", f_clean is not None and f_clean < 0.05, f"{f_clean}")
    check("overlap: occasional dual-speaker stays under the bar",
          f_dual is not None and f_dual <= S._MAX_OVERLAP_FRACTION, f"{f_dual}")
    check("overlap: doubled track over the bar",
          f_doubled is not None and f_doubled > S._MAX_OVERLAP_FRACTION, f"{f_doubled}")
    ok1, _ = S.sub_display_sane(clean, None)
    ok2, _ = S.sub_display_sane(dual, None)
    ok3, why = S.sub_display_sane(doubled, None)
    check("gate accepts clean + dual-speaker", ok1 and ok2)
    check("gate rejects doubled track", not ok3, f"{why}")
    for p in (clean, dual, doubled):
        p.unlink(missing_ok=True)

    # runtime fit is judged by the track's BODY (p95 onset), so a tiny
    # over-shifted tail (aligners do this to OP/ED/preview blocks) passes while
    # a mostly-out-of-range (wrong-cut / double-length) track still fails.
    if VIDEO.exists():
        dur = S._video_duration_ms(VIDEO) or 0
        body = [(i * 1000, i * 1000 + 800) for i in range(max(1, dur // 1000))]
        tail = _write("_mig_sane_tail.srt", body + [(dur + 200_000, dur + 202_000)])
        wrong = _write("_mig_sane_wrong.srt",
                       [(dur + i * 3000, dur + i * 3000 + 2000) for i in range(40)])
        okt, _ = S.sub_display_sane(tail, VIDEO)
        okw, whyw = S.sub_display_sane(wrong, VIDEO)
        check("fit: stray over-shifted tail tolerated", okt)
        check("fit: mostly-out-of-range track rejected", not okw, f"{whyw}")
        for p in (tail, wrong):
            p.unlink(missing_ok=True)

    # bilingual twin-drop: same-window non-JA copies go, everything else stays
    cues = [
        (0, 1000, "こんにちは"), (0, 1000, "你好啊朋友"),      # JP + CN twin -> CN dropped
        (2000, 3000, "げんきですか"), (2000, 3000, "元气吗"),  # JP + CN twin -> CN dropped
        (4000, 5000, "海の物語"),                              # lone JP -> kept
        (6000, 7000, "no japanese here"),                      # lone non-JP -> kept
        (8000, 9000, "はい"), (8000, 9000, "ええそうです"),     # JP + JP twin -> both kept
    ]
    out = S._drop_non_ja_twins(cues)
    texts = [t for (_, _, t) in out]
    check("twin-drop removes same-window non-JA copies",
          "你好啊朋友" not in texts and "元气吗" not in texts, f"{texts}")
    check("twin-drop keeps JP, lone non-JP and simultaneous JP",
          {"こんにちは", "げんきですか", "海の物語", "no japanese here",
           "はい", "ええそうです"} == set(texts), f"{texts}")


def main() -> int:
    print("=== app.subs selftest ===\n")
    init_db()
    _check_caption_cleaner()
    print()
    _check_alignment_scoring()
    print()
    _check_reference_hygiene()
    print()
    _check_track_sanity()
    print()

    if not VIDEO.exists() or not SUB.exists():
        print(f"[FAIL] TestShow assets missing under {LIB}")
        return 1

    episode_id = None
    try:
        # --- fake title + episode pointing at the TestShow mp4 ---
        with cursor() as cx:
            cx.execute(
                "INSERT INTO titles(anilist_id,romaji,english,format,total_episodes) "
                "VALUES(?,?,?,?,?) ON CONFLICT(anilist_id) DO NOTHING",
                (FAKE_ANILIST, "TestShow", "Test Show", "TV", 1),
            )
            cx.execute(
                "INSERT INTO episodes(anilist_id,ep_number,title,video_path) VALUES(?,?,?,?) "
                "ON CONFLICT(anilist_id,ep_number) DO UPDATE SET video_path=excluded.video_path",
                (FAKE_ANILIST, 1, "Episode 1", str(VIDEO)),
            )
            episode_id = cx.execute(
                "SELECT id FROM episodes WHERE anilist_id=? AND ep_number=1", (FAKE_ANILIST,)
            ).fetchone()["id"]
        print(f"fake episode_id = {episode_id}\n")

        # --- ingest the TestShow sub ---
        # ingest_subtitle rewrites its input .srt with the cleaned cues, so work
        # off a throwaway copy — never mutate the tracked fixture.
        import shutil, tempfile
        sub_copy = Path(tempfile.gettempdir()) / f"migaku_selftest_{FAKE_ANILIST}.ja.srt"
        shutil.copy(SUB, sub_copy)
        sub_id = S.ingest_subtitle(episode_id, sub_copy, source="jimaku", jimaku_file_id=12345)
        with connect() as cx:
            n_lines = cx.execute("SELECT COUNT(*) n FROM subtitle_lines WHERE episode_id=?", (episode_id,)).fetchone()["n"]
            n_lemmas = cx.execute("SELECT COUNT(*) n FROM line_lemmas WHERE episode_id=?", (episode_id,)).fetchone()["n"]
            n_fts = cx.execute("SELECT COUNT(*) n FROM subtitle_fts WHERE episode_id=?", (episode_id,)).fetchone()["n"]
            sample = cx.execute(
                "SELECT text, text_furigana FROM subtitle_lines WHERE episode_id=? ORDER BY idx LIMIT 1",
                (episode_id,)).fetchone()
        print(f"ingest: subtitle_id={sub_id} lines={n_lines} lemmas={n_lemmas} fts={n_fts}")
        print(f"  line[0] text     = {sample['text']}")
        print(f"  line[0] furigana = {sample['text_furigana']}\n")
        check("subtitle_lines populated", n_lines >= 4, f"{n_lines} lines")
        check("line_lemmas populated", n_lemmas > 0, f"{n_lemmas} lemmas")
        check("subtitle_fts populated", n_fts == n_lines)
        check("furigana stored", bool(sample["text_furigana"]) and "<ruby>" in (sample["text_furigana"] or ""))

        # --- idempotency: re-ingest must not duplicate ---
        S.ingest_subtitle(episode_id, sub_copy, source="jimaku", jimaku_file_id=12345)
        with connect() as cx:
            n_lines2 = cx.execute("SELECT COUNT(*) n FROM subtitle_lines WHERE episode_id=?", (episode_id,)).fetchone()["n"]
        check("ingest idempotent (no dupes)", n_lines2 == n_lines, f"{n_lines2} after re-ingest")

        # --- seed a fake KNOWN set (dict-forms that actually appear) ---
        # TestShow vocab tokens (per UniDic): これ 日本語 天気 テスト 今日 いい 勉強 ...
        known_forms = ["これ", "日本語", "天気", "今日", "です", "の", "は", "テスト", "いい", "ね", "を"]
        with cursor() as cx:
            for f in known_forms:
                cx.execute(
                    "INSERT INTO known_words(dict_form,reading,status,source) VALUES(?,?,?, 'selftest') "
                    "ON CONFLICT(dict_form,reading) DO UPDATE SET status=excluded.status, source='selftest'",
                    (f, "", "KNOWN"),
                )
            # mark one appearing word IGNORED to exercise the denominator path
            cx.execute(
                "INSERT INTO known_words(dict_form,reading,status,source) VALUES('ます','','IGNORED','selftest') "
                "ON CONFLICT(dict_form,reading) DO UPDATE SET status='IGNORED', source='selftest'", ())

        comp = L.comprehension_aligned(episode_id)
        print(f"\ncomprehension_aligned: pct={comp.comprehension_pct} rating={comp.rating!r} "
              f"source={comp.source} total={comp.total_tokens} known={comp.known_tokens} "
              f"unknown_unique={comp.unknown_unique}")
        print("  new_words (top 8):")
        for w in comp.new_words[:8]:
            print(f"    {w.lemma!s:8} reading={w.reading!s:6} rank={w.freq_rank} gloss={w.gloss!s:.40} x{w.count}")
        check("comprehension computed", 0.0 <= comp.comprehension_pct <= 100.0)
        check("comprehension stored on episode",
              _episode_pct(episode_id) == comp.comprehension_pct)
        check("rating label present", bool(comp.rating))
        check("new_words returned", comp.unknown_unique >= 1)

        # --- moments search via line_lemmas ---
        moments = L.moments_search("日本語")
        print(f"\nmoments_search('日本語'): {len(moments)} hit(s)")
        for m in moments[:3]:
            print(f"    line {m.line_id} ep{m.ep_number} [{m.start_ms}-{m.end_ms}] {m.text}")
            print(f"        image_url={m.image_url} audio_url={m.audio_url}")
        check("moments returns the line", len(moments) >= 1)
        check("moment carries media anchors", bool(moments and moments[0].image_url and moments[0].video_path))
        # free-text fallback (>=3 chars) for a substring that isn't a lemma
        ft = L.moments_search("プレイヤー")
        check("moments fts fallback works", len(ft) >= 1, f"{len(ft)} hit(s) for プレイヤー")

        # --- extract clip ---
        target_line = moments[0].line_id if moments else None
        if target_line is not None:
            res = L.extract_clip(target_line)
            img = settings.clips_dir / f"line_{target_line}.jpg"
            aud = settings.clips_dir / f"line_{target_line}.m4a"
            print(f"\nextract_clip(line {target_line}): {res}")
            print(f"    img exists={img.exists()} ({img.stat().st_size if img.exists() else 0} B) "
                  f"aud exists={aud.exists()} ({aud.stat().st_size if aud.exists() else 0} B)")
            check("clip screenshot produced", img.exists() and img.stat().st_size > 0)
            check("clip audio produced", aud.exists() and aud.stat().st_size > 0)
        else:
            check("clip screenshot produced", False, "no line to clip")

        # --- optional jimaku LIVE fixture ---
        raw_live_id = os.getenv("MIMI_TEST_JIMAKU_ANILIST_ID", "").strip()
        raw_live_ep = os.getenv("MIMI_TEST_JIMAKU_EPISODE", "").strip()
        prefer = os.getenv("MIMI_TEST_JIMAKU_PREFER", "").strip() or None
        if not (raw_live_id and raw_live_ep):
            print("\n--- SKIP jimaku LIVE (set MIMI_TEST_JIMAKU_ANILIST_ID "
                  "and MIMI_TEST_JIMAKU_EPISODE) ---")
        else:
          try:
            live_id = int(raw_live_id)
            live_ep = int(raw_live_ep)
            print("\n--- jimaku LIVE (configured fixture) ---")
            entries = S.jimaku_search(live_id)
            check("jimaku_search returns entries", len(entries) >= 1, f"{len(entries)} entries")
            if entries:
                entry = entries[0]
                eid = entry.get("id")
                print(f"    entry id={eid} name={entry.get('name') or entry.get('english_name')!r}")
                files = S.jimaku_files(eid, live_ep)
                check("jimaku_files returns files for configured episode",
                      len(files) >= 1, f"{len(files)} files")
                if files:
                    best = S.pick_best_file(files, prefer=prefer)
                    from urllib.parse import urlparse
                    host = urlparse(S._file_url(best) or "").netloc
                    print(f"    picked file = {S._file_name(best)!r}")
                    print(f"    url host    = {host}")
                    check("pick_best_file + url host", bool(host))
          except ValueError:
            check("jimaku live fixture values are integers", False)
          except RuntimeError as e:
            if "429" in str(e) or "rate" in str(e).lower():
                print(f"    [SKIP] jimaku rate-limited: {e}")
            else:
                check("jimaku live", False, str(e))
          except Exception as e:
            check("jimaku live", False, f"{type(e).__name__}: {e}")

    finally:
        _cleanup(episode_id)
        print("\n(cleaned up fake rows + clip files)")

    print()
    if _fails:
        print(f"=== RESULT: FAIL ({len(_fails)} failed: {', '.join(_fails)}) ===")
        return 1
    print("=== RESULT: PASS ===")
    return 0


def _episode_pct(episode_id: int):
    with connect() as cx:
        return cx.execute("SELECT comprehension_pct FROM episodes WHERE id=?", (episode_id,)).fetchone()["comprehension_pct"]


if __name__ == "__main__":
    sys.exit(main())
