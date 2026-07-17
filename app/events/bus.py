"""Tiny in-process pub/sub for Server-Sent Events (browser live updates).

The UI used to be entirely poll-based (4–15s intervals) with
`refetchOnWindowFocus` off, so after a background job finished — most visibly a
comprehension score — the relevant screen showed stale data until the user
navigated away and back. This bus lets the server *push* a nudge to connected
browsers the instant something changes; the client turns each nudge into a
TanStack Query invalidation.

Design: one asyncio.Queue per connected browser. `publish()` is **thread-safe**
(the job worker + APScheduler call it from non-async threads) — it hops onto the
captured event loop via `call_soon_threadsafe`. Messages are best-effort: if a
client's queue is full we drop the nudge (the client still has its slow poll as a
backstop). Zero external deps, fits the single-user scale.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

log = logging.getLogger("mimi_lab.events.bus")

_subscribers: set[asyncio.Queue] = set()
_loop: Optional[asyncio.AbstractEventLoop] = None

# Coalescing: during a bulk pass (e.g. analyze-library queues many comprehension
# jobs) every completion used to emit an identical nudge, and each nudge made
# every client refetch the whole titles list. Identical payloads inside
# COALESCE_WINDOW are now collapsed to one
# leading send + one trailing send (the trailing one matters: the client must
# refetch the *final* state after the burst).
COALESCE_WINDOW = 1.5  # seconds
_last_sent: dict[str, float] = {}   # payload -> monotonic time of last send
_trailing: set[str] = set()         # payloads with a trailing send scheduled


def publish(kind: str, data: Optional[dict] = None) -> None:
    """Fan out a small message to every connected browser. Safe to call from any
    thread; a no-op when nobody is listening. Identical payloads are coalesced
    within COALESCE_WINDOW (leading + trailing edge)."""
    if not _subscribers or _loop is None:
        return
    try:
        payload = json.dumps({"type": kind, **(data or {})}, ensure_ascii=False)
    except Exception:
        return

    def _fan_out(p: str) -> None:
        _last_sent[p] = _loop.time() if _loop else 0.0
        for q in list(_subscribers):
            try:
                q.put_nowait(p)
            except asyncio.QueueFull:
                pass  # slow/stuck client — drop; its poll fallback will catch up
            except Exception:
                pass
        # bound the dedup map (payload variety is tiny in practice)
        if len(_last_sent) > 256:
            _last_sent.clear()

    def _maybe_send() -> None:
        now = _loop.time() if _loop else 0.0
        last = _last_sent.get(payload)
        if last is None or (now - last) >= COALESCE_WINDOW:
            _fan_out(payload)
            return
        if payload in _trailing:
            return  # a trailing send is already scheduled for this burst

        def _trail() -> None:
            _trailing.discard(payload)
            _fan_out(payload)

        _trailing.add(payload)
        try:
            _loop.call_later(COALESCE_WINDOW, _trail)
        except Exception:
            _trailing.discard(payload)

    try:
        _loop.call_soon_threadsafe(_maybe_send)
    except Exception:
        pass


async def subscribe():
    """Async generator of SSE-ready frames for one browser connection.

    Yields formatted `data:`/comment frames; sends a keepalive comment every 25s
    so idle connections aren't reaped by proxies.
    """
    global _loop
    _loop = asyncio.get_running_loop()
    q: asyncio.Queue = asyncio.Queue(maxsize=100)
    _subscribers.add(q)
    try:
        yield ": connected\n\n"
        while True:
            try:
                msg = await asyncio.wait_for(q.get(), timeout=25.0)
                yield f"data: {msg}\n\n"
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
    finally:
        _subscribers.discard(q)


def subscriber_count() -> int:
    return len(_subscribers)
