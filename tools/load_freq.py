"""Populate the `lemma_freq` table with a real Japanese word-frequency list.

The learn module (`app/learn/service.py::new_words`) ranks unknown words by
`lemma_freq.rank` (lower = more frequent) so the most useful new vocab floats to
the top of the Moments / new-words view. Out of the box `lemma_freq` is empty
(every rank is NULL), which collapses that ordering to a plain occurrence count.

This script downloads a freely-available, anime/fiction/subtitle-weighted
Japanese frequency list packaged as a Yomitan/Yomichan dictionary zip, parses its
`term_meta_bank_*.json` frequency entries, and bulk-loads `(term -> rank)` into
`lemma_freq`. Terms are keyed by surface/dictionary form (Migaku `dictForm`
convention, e.g. 逃げる, 日本語, する), matching how the tokenizer keys words.

Primary source: the **JPDB v2.2** frequency dictionary (a JPDB-corpus-derived,
rank-based Yomitan dict; JPDB's corpus is heavily anime / visual-novel / light-
novel weighted). Fallbacks: other maintained `*-frequency*` Yomitan dicts.

Yomitan `freq` entries come in several shapes; all are handled:
    ["term", "freq", 123]
    ["term", "freq", {"value": 123, "displayValue": "123"}]
    ["term", "freq", {"reading": "...", "frequency": 123}]
    ["term", "freq", {"reading": "...", "frequency": {"value": 123, ...}}]
When a term repeats (homographs / multiple readings) the lowest (best) rank wins.

Idempotent & re-runnable: downloads are cached under app/learn/data/, and rows
are written with INSERT OR REPLACE. Run:

    .venv/bin/python tools/load_freq.py
"""
from __future__ import annotations

import io
import json
import sys
import zipfile
from pathlib import Path
from typing import Iterable, Optional

import httpx

# Import the project's DB layer (schema: lemma_freq(lemma TEXT PK, rank INTEGER)).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.db import cursor, init_db  # noqa: E402

DATA_DIR = Path(__file__).resolve().parents[1] / "app" / "learn" / "data"
CACHE_DIR = DATA_DIR / "freq_cache"

# Candidate Yomitan/Yomichan frequency dictionaries (zip), tried in order.
# Each is a freely-distributed community frequency dict keyed by dictionary form.
FREQ_SOURCES: list[dict] = [
    {
        "name": "JPDB v2.2 Frequency (2024-10-13)",
        # JPDB-corpus-derived rank-based frequency list (~279k entries).
        # JPDB corpus is anime / visual-novel / light-novel weighted.
        "url": "https://raw.githubusercontent.com/Kuuuube/yomitan-dictionaries/main/dictionaries/JPDB_v2.2_Frequency_2024-10-13.zip",
        "license": "JPDB corpus data, redistributed by the Kuuuube/yomitan-dictionaries community repo for Yomitan use.",
    },
    {
        "name": "JPDB v2.1 Frequency (2024-05-26)",
        "url": "https://raw.githubusercontent.com/Kuuuube/yomitan-dictionaries/main/dictionaries/JPDB_v2.1_2024-05-26.zip",
        "license": "JPDB corpus data, redistributed by the Kuuuube/yomitan-dictionaries community repo for Yomitan use.",
    },
    {
        "name": "BCCWJ SUW+LUW combined",
        # Balanced Corpus of Contemporary Written Japanese (broad written corpus).
        "url": "https://raw.githubusercontent.com/Kuuuube/yomitan-dictionaries/main/dictionaries/BCCWJ_SUW_LUW_combined.zip",
        "license": "Derived from BCCWJ; redistributed by Kuuuube/yomitan-dictionaries.",
    },
]

# Spot-check vocab: extremely common words that MUST land at small ranks.
SPOT_CHECK = ["する", "いる", "事", "時", "人", "思う", "言う", "の", "逃げる", "日本語"]

MAX_RANK = 500_000  # safety guard; real lists are well under this


def _download(url: str, timeout: float = 240.0) -> bytes:
    """Download `url` to the cache (keyed by filename) and return its bytes.

    Cached files are reused on re-run so the script is fast and offline-friendly
    after the first successful fetch.
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    fname = url.rsplit("/", 1)[-1] or "freq.zip"
    cached = CACHE_DIR / fname
    if cached.exists() and cached.stat().st_size > 0:
        print(f"[freq] using cached {cached.name} ({cached.stat().st_size:,} bytes)")
        return cached.read_bytes()
    print(f"[freq] downloading {url} …")
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        r = client.get(url)
        r.raise_for_status()
        data = r.content
    cached.write_bytes(data)
    print(f"[freq] cached -> {cached.name} ({len(data):,} bytes)")
    return data


def _coerce_rank(value: object) -> Optional[int]:
    """Pull an integer rank out of any Yomitan freq value shape.

    Handles: int, numeric str, {"value": N}, {"frequency": N},
    {"frequency": {"value": N}}, {"reading": .., "frequency": {"value": N}}.
    Returns None when no usable integer can be extracted.
    """
    if isinstance(value, bool):  # guard: bool is an int subclass
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        s = value.strip().replace(",", "")
        # ranks are sometimes formatted like "12000㋕" or "12000" — take digits
        digits = "".join(ch for ch in s if ch.isdigit())
        return int(digits) if digits else None
    if isinstance(value, dict):
        if "value" in value:
            return _coerce_rank(value["value"])
        if "frequency" in value:
            return _coerce_rank(value["frequency"])
        # last resort: any nested numeric
        for v in value.values():
            r = _coerce_rank(v)
            if r is not None:
                return r
    return None


def _iter_freq_entries(zip_bytes: bytes) -> Iterable[tuple[str, int]]:
    """Yield (term, rank) from every term_meta_bank_*.json in the Yomitan zip.

    Yomitan term-meta entries are `[term, mode, data]`; we only consume the
    `"freq"` mode. `data` is decoded via `_coerce_rank`.
    """
    zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    banks = sorted(
        n for n in zf.namelist()
        if n.rsplit("/", 1)[-1].startswith("term_meta_bank") and n.endswith(".json")
    )
    if not banks:
        raise RuntimeError("no term_meta_bank_*.json found in zip (not a Yomitan freq dict?)")

    # Print dictionary metadata if present (title / attribution / description).
    if "index.json" in zf.namelist():
        try:
            idx = json.loads(zf.read("index.json"))
            meta = {k: idx.get(k) for k in ("title", "revision", "frequencyMode", "author", "url")}
            print(f"[freq] dict index: {json.dumps(meta, ensure_ascii=False)}")
        except Exception:
            pass

    for bank in banks:
        entries = json.loads(zf.read(bank))
        for entry in entries:
            # Expected: [term, "freq", data]
            if not isinstance(entry, list) or len(entry) < 3:
                continue
            term, mode, data = entry[0], entry[1], entry[2]
            if mode != "freq" or not isinstance(term, str) or not term:
                continue
            rank = _coerce_rank(data)
            if rank is None or rank <= 0 or rank > MAX_RANK:
                continue
            yield term, rank


def _best_ranks(zip_bytes: bytes) -> dict[str, int]:
    """Collapse all freq entries to one best (lowest) rank per term."""
    best: dict[str, int] = {}
    n_entries = 0
    for term, rank in _iter_freq_entries(zip_bytes):
        n_entries += 1
        cur = best.get(term)
        if cur is None or rank < cur:
            best[term] = rank
    print(f"[freq] parsed {n_entries:,} freq entries -> {len(best):,} unique terms")
    return best


def _load_rows(ranks: dict[str, int]) -> int:
    """Bulk INSERT OR REPLACE (term -> rank) into lemma_freq. Returns row count."""
    init_db()  # ensure the table exists (no-op if already created)
    rows = list(ranks.items())
    with cursor() as cx:
        cx.executemany(
            "INSERT OR REPLACE INTO lemma_freq(lemma, rank) VALUES (?, ?)",
            rows,
        )
    return len(rows)


def _verify() -> None:
    """Print total rows, spot-check ranks, and confirm join payoff."""
    with cursor() as cx:
        total = cx.execute("SELECT COUNT(*) FROM lemma_freq").fetchone()[0]
        print(f"\n[verify] lemma_freq now holds {total:,} rows")

        print("[verify] spot-check ranks for very common words (lower = more frequent):")
        qmarks = ",".join("?" * len(SPOT_CHECK))
        found = {
            r["lemma"]: r["rank"]
            for r in cx.execute(
                f"SELECT lemma, rank FROM lemma_freq WHERE lemma IN ({qmarks})",
                SPOT_CHECK,
            )
        }
        for w in SPOT_CHECK:
            rank = found.get(w)
            print(f"    {w:<6} -> {rank if rank is not None else 'MISSING'}")

        # Payoff check: join lemma_freq against any ingested subtitle lemmas.
        joined = cx.execute(
            """
            SELECT ll.lemma, lf.rank, COUNT(*) AS hits
            FROM line_lemmas ll
            JOIN lemma_freq lf ON lf.lemma = ll.lemma
            GROUP BY ll.lemma, lf.rank
            ORDER BY lf.rank ASC
            LIMIT 10
            """
        ).fetchall()
        n_lines = cx.execute("SELECT COUNT(*) FROM line_lemmas").fetchone()[0]
        if joined:
            print(
                f"[verify] line_lemmas↔lemma_freq join works "
                f"({n_lines} ingested lemmas); top-ranked sample:"
            )
            for r in joined:
                print(f"    {r['lemma']:<8} rank={r['rank']:<6} (x{r['hits']})")
        else:
            print(
                f"[verify] no ingested subtitle lemmas to join yet "
                f"(line_lemmas={n_lines}); table is populated and ready."
            )


def main() -> int:
    last_err: Optional[Exception] = None
    for src in FREQ_SOURCES:
        try:
            print(f"\n[freq] === trying source: {src['name']} ===")
            zip_bytes = _download(src["url"])
            ranks = _best_ranks(zip_bytes)
            if len(ranks) < 1000:
                raise RuntimeError(f"only {len(ranks)} terms parsed — looks wrong, trying next")
            n = _load_rows(ranks)
            print(f"[freq] loaded {n:,} rows into lemma_freq from: {src['url']}")
            print(f"[freq] SOURCE USED: {src['name']}")
            print(f"[freq] SOURCE URL: {src['url']}")
            print(f"[freq] LICENSE NOTE: {src['license']}")
            _verify()
            print("\n[freq] done.")
            return 0
        except Exception as e:  # try the next candidate on any failure
            print(f"[freq] source failed ({type(e).__name__}: {e}); trying next…", file=sys.stderr)
            last_err = e
            continue

    print(f"[freq] ALL sources failed; lemma_freq left unchanged. last error: {last_err}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
