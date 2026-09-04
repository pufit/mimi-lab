"""Tiny Anthropic (Claude) helper — raw httpx, no SDK dependency.

The project pins its venv (SPEC: no `pip install`), so rather than add the
`anthropic` SDK we call the Messages API directly with httpx (the same client
every other integration here uses). Used for *optional* smart matching (Claude
Haiku ranks nyaa releases for an episode). Everything degrades gracefully: with
no API key, a timeout, or any error this returns None and the caller falls back
to its deterministic heuristic.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Optional

import httpx

from .config import settings

log = logging.getLogger("mimi_lab.llm")

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"

# transient statuses worth one retry (the old behavior fell straight back to
# the heuristic on a blip 429/529)
_RETRYABLE = {429, 500, 502, 503, 529}

# Why the last call returned None. Callers that must RAISE on failure (the SRS
# judge jobs) put this in their error message — otherwise a schema regression or
# an expired key looks like "judge unavailable" with no cause anywhere.
_LAST_ERROR: Optional[str] = None


def last_error() -> Optional[str]:
    """Reason the most recent `claude_json` call failed (None after a success)."""
    return _LAST_ERROR


def _fail(reason: str, *, level: int = logging.INFO) -> None:
    global _LAST_ERROR
    _LAST_ERROR = reason
    log.log(level, "Claude: %s", reason)


def available() -> bool:
    """True if an Anthropic API key is configured."""
    return bool(settings.anthropic_api_key)


def _record_usage(model: str, usage: dict) -> None:
    """Accumulate per-model token counters in kv — the system was previously
    blind to its own LLM spend. Cheap (two kv upserts per call), surfaced via
    usage_summary() on /api/health?full=1 and the System page."""
    try:
        from .db import cursor
        in_tok = int(usage.get("input_tokens") or 0)
        out_tok = int(usage.get("output_tokens") or 0)
        with cursor() as cx:
            for suffix, inc in (("in", in_tok), ("out", out_tok), ("calls", 1)):
                cx.execute(
                    "INSERT INTO kv(key, value) VALUES(?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET "
                    "value = CAST(CAST(value AS INTEGER) + ? AS TEXT), "
                    "updated_at = datetime('now')",
                    (f"llm.usage.{model}.{suffix}", str(inc), inc),
                )
    except Exception as e:  # never let accounting break a call
        log.debug("llm usage accounting failed: %s", e)


def usage_summary() -> dict:
    """Lifetime token counters per model, from kv."""
    out: dict = {}
    try:
        from .db import connect
        with connect() as cx:
            for r in cx.execute("SELECT key, value FROM kv WHERE key LIKE 'llm.usage.%'"):
                _, _, rest = r["key"].partition("llm.usage.")
                model, _, suffix = rest.rpartition(".")
                out.setdefault(model, {})[suffix] = int(r["value"] or 0)
    except Exception:
        pass
    return out


def claude_json(
    system: str,
    user: str,
    *,
    model: Optional[str] = None,
    schema: Optional[dict] = None,
    max_tokens: int = 512,
    timeout: float = 20.0,
    effort: Optional[str] = None,
) -> Optional[dict]:
    """Call a Claude model and return a parsed JSON object, or None on any failure.

    `model` defaults to `settings.anthropic_model` (Haiku). Pass e.g.
    `settings.translation_model` (Sonnet) for heavier work like translation.

    When `schema` (a JSON Schema) is given, the response is constrained to it via
    `output_config.format` so the reply is guaranteed parseable. `thinking` is
    never sent (Haiku rejects it; Opus/Sonnet 4.6+ think adaptively by default);
    `effort` (low|medium|high|xhigh|max) is forwarded via `output_config` only
    when given — Haiku rejects it. Never raises — callers treat None as "LLM
    unavailable, fall back".
    """
    global _LAST_ERROR
    if not settings.anthropic_api_key:
        _fail("no ANTHROPIC_API_KEY configured")
        return None

    body: dict = {
        "model": model or settings.anthropic_model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }
    if schema is not None:
        body["output_config"] = {"format": {"type": "json_schema", "schema": schema}}
    if effort:
        body.setdefault("output_config", {})["effort"] = effort

    headers = {
        "x-api-key": settings.anthropic_api_key,
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }

    data = None
    for attempt in (1, 2):
        try:
            r = httpx.post(ANTHROPIC_URL, json=body, headers=headers, timeout=timeout)
            if r.status_code in _RETRYABLE and attempt == 1:
                retry_after = min(10.0, float(r.headers.get("retry-after") or 2.0))
                log.info("Claude %s — retrying once in %.1fs", r.status_code, retry_after)
                time.sleep(retry_after)
                continue
            if r.status_code >= 400:
                # A non-retryable 4xx is a real defect (bad key, bad model id, a
                # schema the API rejects) and used to vanish into a one-line INFO
                # with no body — log status + body so the cause is on record.
                _fail(f"HTTP {r.status_code} from {body['model']}: {r.text[:500]}",
                      level=logging.WARNING)
                return None
            r.raise_for_status()
            data = r.json()
            break
        except httpx.TimeoutException as e:
            _fail(f"timeout after {timeout}s ({e})")
            return None
        except Exception as e:  # network, auth, rate limit, etc. — degrade
            _fail(f"call failed ({type(e).__name__}: {e})")
            return None
    if data is None:
        _fail(f"no response from {body['model']} after retry")
        return None
    _record_usage(body["model"], data.get("usage") or {})

    # refusal / non-text stop — bail
    if data.get("stop_reason") == "refusal":
        _fail("model refused the request")
        return None

    text = ""
    for block in data.get("content", []):
        if block.get("type") == "text":
            text += block.get("text", "")
    text = text.strip()
    if not text:
        _fail(f"empty response (stop_reason={data.get('stop_reason')})")
        return None

    try:
        out = json.loads(text)
        _LAST_ERROR = None
        return out
    except json.JSONDecodeError:
        # tolerate a fenced ```json block if the model added one
        cleaned = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        try:
            out = json.loads(cleaned)
            _LAST_ERROR = None
            return out
        except json.JSONDecodeError:
            _fail(f"non-JSON output ({text[:200]!r})")
            return None


def haiku_json(
    system: str,
    user: str,
    *,
    schema: Optional[dict] = None,
    max_tokens: int = 512,
    timeout: float = 20.0,
) -> Optional[dict]:
    """Backwards-compatible wrapper: `claude_json` on the default (Haiku) model."""
    return claude_json(
        system, user, model=settings.anthropic_model,
        schema=schema, max_tokens=max_tokens, timeout=timeout,
    )
