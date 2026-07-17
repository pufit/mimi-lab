"""Self-test for app.learn — run:  .venv/bin/python -m app.learn.selftest

Covers tokenize, furigana, and the Migaku comprehension formula primitives.
Prints PASS/FAIL per check and a final summary line.
"""
from __future__ import annotations

import sys

from app.learn import service as L

_fails: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        _fails.append(name)


def main() -> int:
    print("=== app.learn selftest ===\n")

    # --- tokenize ---
    toks = L.tokenize("これは日本語のテストです")
    print("tokenize('これは日本語のテストです'):")
    for t in toks:
        print(f"    surface={t['surface']!s:6} lemma={t['lemma']!s:6} reading={t['reading']!s:6} pos={t['pos']}")
    surfaces = [t["surface"] for t in toks]
    check("tokenize splits content words", len(toks) >= 5, f"{len(toks)} tokens")
    check("tokenize keeps 日本/語/テスト", all(x in "".join(surfaces) for x in ("日本", "語", "テスト")))
    check("readings are hiragana", all(all(ch <= "ヿ" for ch in t["reading"]) for t in toks if t["reading"]))

    # a casual line from the TestShow sub
    line = "ミガクのプレイヤーで自動再生のテスト中です。"
    toks2 = L.tokenize(line)
    print(f"\ntokenize('{line}'):")
    for t in toks2:
        print(f"    surface={t['surface']!s:6} lemma={t['lemma']!s:6} reading={t['reading']!s:6} pos={t['pos']}")
    check("punctuation stripped", all(t["surface"] != "。" for t in toks2))

    # --- furigana ---
    fg = L.furigana("今日はいい天気ですね。")
    print(f"\nfurigana: {fg}")
    check("furigana rubies kanji", "<ruby>" in fg and "<rt>" in fg)
    check("furigana keeps kana bare", "はいい" in fg)

    # --- content-token filter (regression: Migaku emits symbol tokens with an
    # empty POS; the old _PUNCT_RE missed …/→/♬/⚟ and they were miscounted as
    # unknown words, tanking comprehension on punctuation-heavy subs) ---
    print("\ncontent-token filter:")
    symbol_only = ["…》", "…。", "…", "→", "♬～", "⚟（", "《", "》", "・", "ー", "♬"]
    words = ["学習者", "日本語", "ラーメン", "コーヒー", "えーっ", "2人", "ABC"]
    drop = [s for s in symbol_only if L._is_content(s)]
    keep = [w for w in words if not L._is_content(w)]
    print(f"    symbol-only kept (should be []): {drop}")
    print(f"    words dropped (should be []):    {keep}")
    check("symbol-only tokens are dropped (incl. lone ・ ー)", not drop, f"leaked {drop}")
    check("real words (incl. ー-bearing) are kept", not keep, f"dropped {keep}")

    # --- comprehension formula primitives ---
    print("\nformula primitives:")
    c_all = L._c4e(5, 0, 5, 0, 0)
    c_one = L._c4e(5, 1, 4, 0, 1)
    c_ign = L._c4e(4, 1, 2, 1, 1)  # 1 ignored removed from denom -> known/(total-ignored)
    print(f"    C4e(all known)={c_all}  C4e(1/5 unknown)={c_one}  C4e(w/ignored)={c_ign}")
    check("C4e all-known == 100", c_all == 100.0)
    check("C4e 1-unknown-of-5 == 80", c_one == 80.0)
    check("C4e drops ignored from denom", abs(c_ign - (2 / 3) * 100) < 1e-6, f"{c_ign:.2f}")
    e_perfect = L._e4e([(100.0, 4), (100.0, 3)], 0, 0)
    e_mixed = L._e4e([(100.0, 4), (80.0, 5)], 1, 0)
    print(f"    E4e(all 100)={e_perfect}  E4e(mixed)={e_mixed}")
    check("E4e all-100 == 100", e_perfect == 100.0)
    check("E4e mixed < weighted-mean", e_mixed < 100.0)
    check("S4e thresholds", L._s4e(66) == "Challenging" and L._s4e(95) == "Excellent" and L._s4e(72) == "Approachable")

    print()
    if _fails:
        print(f"=== RESULT: FAIL ({len(_fails)} failed: {', '.join(_fails)}) ===")
        return 1
    print("=== RESULT: PASS ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
