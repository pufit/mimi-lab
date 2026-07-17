"""Client for the migaku-tokenizer sidecar (tools/migaku-tokenizer/server.mjs).

The sidecar runs Migaku's OWN analyzer in Node and returns Migaku-exact tokens
(surface, dictForm, reading, pos, pitch). Using these instead of fugashi makes
our comprehension match Migaku's own number (≈ within rounding). If the sidecar
is down, callers fall back to the local fugashi tokenizer.
"""
from __future__ import annotations

import logging
import os

import httpx

log = logging.getLogger("mimi_lab.learn.migaku_tok")

_URL = os.environ.get("MIGAKU_TOK_URL", "http://127.0.0.1:8788").rstrip("/")


def available(timeout: float = 2.0) -> bool:
    try:
        r = httpx.get(f"{_URL}/health", timeout=timeout)
        return r.status_code == 200 and bool(r.json().get("ok"))
    except Exception:
        return False


def health(timeout: float = 2.0) -> dict:
    """Raw sidecar /health: {ok, ext, lang} where `ext` is the resolved Migaku
    extension version. Returns {ok: False} if the sidecar is unreachable."""
    try:
        r = httpx.get(f"{_URL}/health", timeout=timeout)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        log.debug("migaku tokenizer health check failed: %s", e)
    return {"ok": False}


def tokenize_lines(texts: list[str], lang: str = "ja", timeout: float = 180.0):
    """POST lines to the sidecar. Returns a list aligned 1:1 with `texts`, each a
    list of tokens {surface, dictForm, reading, pos, pitches}; or None on any
    failure (caller should fall back to the local tokenizer)."""
    if not texts:
        return []
    try:
        r = httpx.post(f"{_URL}/tokenize", json={"lines": list(texts), "lang": lang}, timeout=timeout)
        r.raise_for_status()
        toks = r.json().get("tokens")
        if isinstance(toks, list) and len(toks) == len(texts):
            return toks
        log.warning("migaku tokenizer row mismatch: %s rows for %s lines",
                    len(toks) if isinstance(toks, list) else "?", len(texts))
        return None
    except Exception as e:
        log.info("migaku tokenizer unavailable (%s); falling back to fugashi", e)
        return None
