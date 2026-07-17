"""Best-effort builder for the gloss + frequency data the learn module uses.

Strategy (compact + fast, degrades gracefully):
  * GLOSSES — download the small jmdict-simplified "eng-common" release
    (~1.4 MB tgz, ~22.6k common entries) and build
    app/learn/data/glosses.sqlite  → glosses(form TEXT PRIMARY KEY, gloss TEXT).
    Covers the everyday vocabulary that "new words" are drawn from.
  * FREQUENCY — try a compact frequency wordlist; if none is reachable, leave
    lemma_freq empty (rank stays null). The JMdict common subset only carries a
    boolean `common` flag (no usable rank), so it is NOT used as a freq proxy.

The learn module treats missing gloss/freq as empty (gloss=None, rank=null), so
a DEGRADED run never blocks the core. Idempotent.
Run:  .venv/bin/python -m app.learn.data.build_data
"""
from __future__ import annotations

import io
import json
import sqlite3
import sys
import tarfile
from pathlib import Path

import httpx

from app.db import cursor

DATA_DIR = Path(__file__).resolve().parent
GLOSS_DB = DATA_DIR / "glosses.sqlite"
RELEASE_API = "https://api.github.com/repos/scriptin/jmdict-simplified/releases/latest"

# Candidate compact frequency lists (word<TAB or space>rank/count). Best-effort.
FREQ_SOURCES = [
    # Anime/JDrama subtitle frequency (one word per line, ranked by position).
    "https://raw.githubusercontent.com/Matchin-Games/japanese_frequency/master/japanese_frequency_list.txt",
    "https://raw.githubusercontent.com/ttsuiki/japanese-word-frequency/master/frequency.txt",
]


def _pick_gloss_asset() -> tuple[str, str]:
    r = httpx.get(RELEASE_API, timeout=20)
    r.raise_for_status()
    rel = r.json()
    for a in rel.get("assets", []):
        nm = a["name"].lower()
        if "eng-common" in nm and nm.endswith(".tgz"):
            return a["name"], a["browser_download_url"]
    raise RuntimeError("no jmdict eng-common tgz in latest release")


def build_glosses() -> int:
    name, url = _pick_gloss_asset()
    print(f"[data] downloading {name} …")
    r = httpx.get(url, timeout=180, follow_redirects=True)
    r.raise_for_status()
    tf = tarfile.open(fileobj=io.BytesIO(r.content), mode="r:gz")
    member = next((m for m in tf.getmembers() if m.name.endswith(".json")), None)
    if not member:
        raise RuntimeError("no .json inside jmdict tgz")
    data = json.loads(tf.extractfile(member).read().decode("utf-8"))
    words = data.get("words", [])
    print(f"[data] parsing {len(words)} JMdict common entries …")

    gloss_rows: dict[str, str] = {}
    for w in words:
        gl: list[str] = []
        for s in w.get("sense", [])[:3]:
            for g in s.get("gloss", [])[:4]:
                t = g.get("text")
                if t:
                    gl.append(t)
        gloss_text = "; ".join(dict.fromkeys(gl))[:400]
        if not gloss_text:
            continue
        for grp in ("kanji", "kana"):
            for k in w.get(grp, []):
                form = k.get("text")
                if form and form not in gloss_rows:
                    gloss_rows[form] = gloss_text

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    gx = sqlite3.connect(str(GLOSS_DB))
    gx.execute("DROP TABLE IF EXISTS glosses")
    gx.execute("CREATE TABLE glosses (form TEXT PRIMARY KEY, gloss TEXT)")
    gx.executemany("INSERT OR IGNORE INTO glosses(form,gloss) VALUES(?,?)", list(gloss_rows.items()))
    gx.commit()
    gx.close()
    print(f"[data] wrote {len(gloss_rows)} glosses -> {GLOSS_DB.name}")
    return len(gloss_rows)


def build_freq() -> int:
    text = None
    for url in FREQ_SOURCES:
        try:
            r = httpx.get(url, timeout=60, follow_redirects=True)
            if r.status_code == 200 and r.text.strip():
                text = r.text
                print(f"[data] frequency list from {url.split('/')[-1]}")
                break
        except Exception:
            continue
    if not text:
        print("[data] no frequency list reachable — lemma_freq left empty (rank=null)")
        return 0

    rows: list[tuple[str, int]] = []
    rank = 0
    seen: set[str] = set()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        word = line.split("\t")[0].split()[0] if (" " in line or "\t" in line) else line
        word = word.strip()
        if not word or word in seen:
            continue
        seen.add(word)
        rank += 1
        rows.append((word, rank))
        if rank >= 50000:
            break
    with cursor() as cx:
        cx.executemany(
            "INSERT INTO lemma_freq(lemma,rank) VALUES(?,?) "
            "ON CONFLICT(lemma) DO UPDATE SET rank=excluded.rank",
            rows,
        )
    print(f"[data] wrote {len(rows)} lemma_freq ranks")
    return len(rows)


def build() -> dict:
    glosses = 0
    freq = 0
    try:
        glosses = build_glosses()
    except Exception as e:
        print(f"[data] gloss build DEGRADED: {type(e).__name__}: {e}", file=sys.stderr)
    try:
        freq = build_freq()
    except Exception as e:
        print(f"[data] freq build DEGRADED: {type(e).__name__}: {e}", file=sys.stderr)
    return {"glosses": glosses, "freq": freq}


if __name__ == "__main__":
    res = build()
    print("[data] done", res)
    # never fail hard — the core works without this data
    sys.exit(0)
