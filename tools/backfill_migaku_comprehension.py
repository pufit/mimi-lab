"""Re-tokenize episodes with Migaku's own tokenizer (via the migaku-tokenizer
sidecar) and recompute comprehension over those Migaku-exact tokens.

  .venv/bin/python tools/backfill_migaku_comprehension.py --episode <episode-id>
  .venv/bin/python tools/backfill_migaku_comprehension.py --anilist <anilist-id>
  .venv/bin/python tools/backfill_migaku_comprehension.py --all [--force]

Skips episodes whose comprehension_source='exact' (the Player-scraped value) unless
--force. Requires the sidecar (tools/migaku-tokenizer/server.mjs) running.
"""
import argparse

from app.db import connect, cursor
from app.learn import service as learn, migaku_tok


def retokenize(episode_id: int):
    cx = connect()
    rows = cx.execute(
        "SELECT id, text FROM subtitle_lines WHERE episode_id=? ORDER BY idx", (episode_id,)
    ).fetchall()
    cx.close()
    if not rows:
        return ("no-lines", None)
    texts = [r["text"] for r in rows]
    mig = migaku_tok.tokenize_lines(texts)
    if mig is None:
        return ("sidecar-down", None)
    with cursor() as cx:
        cx.execute("DELETE FROM line_lemmas WHERE episode_id=?", (episode_id,))
        for r, line in zip(rows, mig):
            # Drop symbol-only tokens (…, →, ♬ …) Migaku emits with empty pos;
            # _PUNCT_RE misses those Unicode blocks, so gate on linguistic
            # content (kana/kanji/alnum) — else they inflate the denominator.
            toks = [
                (r["id"], episode_id,
                 (t.get("dictForm") or t.get("surface") or ""), t.get("reading") or "",
                 t.get("pos") or "", t.get("surface") or "", "migaku-local")
                for t in (line or [])
                if learn._is_content(t.get("dictForm"), t.get("surface"))
            ]
            if toks:
                cx.executemany(
                    "INSERT INTO line_lemmas(line_id,episode_id,lemma,reading,pos,surface,token_source) "
                    "VALUES(?,?,?,?,?,?,?)", toks,
                )
    res = learn.comprehension_aligned(episode_id)
    return ("ok", res.comprehension_pct)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--episode", type=int)
    ap.add_argument("--anilist", type=int)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--force", action="store_true", help="also re-tokenize source='exact' episodes")
    a = ap.parse_args()

    if not migaku_tok.available():
        print("✗ migaku-tokenizer sidecar not reachable (set MIGAKU_TOK_URL / start server.mjs)")
        return

    cx = connect()
    if a.episode:
        eps = cx.execute("SELECT id, comprehension_pct, comprehension_source FROM episodes WHERE id=?", (a.episode,)).fetchall()
    elif a.anilist:
        eps = cx.execute("SELECT id, comprehension_pct, comprehension_source FROM episodes WHERE anilist_id=? ORDER BY ep_number", (a.anilist,)).fetchall()
    elif a.all:
        eps = cx.execute(
            "SELECT id, comprehension_pct, comprehension_source FROM episodes "
            "WHERE id IN (SELECT DISTINCT episode_id FROM subtitle_lines) ORDER BY id"
        ).fetchall()
    else:
        print("specify --episode N | --anilist N | --all")
        return
    cx.close()

    done = skipped = 0
    for e in eps:
        if (e["comprehension_source"] == "exact") and not a.force:
            print(f"  ep {e['id']}: skip (source=exact, pct={e['comprehension_pct']})")
            skipped += 1
            continue
        old = e["comprehension_pct"]
        status, pct = retokenize(e["id"])
        if status == "ok":
            print(f"  ep {e['id']}: {old} -> {pct}  (migaku-local)")
            done += 1
        else:
            print(f"  ep {e['id']}: {status}")
    print(f"done={done} skipped={skipped}")


if __name__ == "__main__":
    main()
