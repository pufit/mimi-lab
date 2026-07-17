"""Connector relay registry — the server side of the WebSocket the user-side
Connector dials out to (notes/REMOTE_PLAYBACK_DESIGN.md §3).

Multi-device: every watching machine runs its own Connector and identifies
itself with a stable `device_id` (+ a human-readable `device_name`) in its
register/heartbeat frames. The relay:
  * tracks one live WebSocket per device (+ each device's {chrome, migaku}),
  * routes commands (play / sync-known / selfcheck) to an explicit device —
    or, when no device is named, to a sensible default (with a single device
    connected this behaves exactly like the old single-slot registry),
  * lets a reconnect from the SAME device supersede its old half-open socket,
    while DIFFERENT devices coexist,
  * exposes a status snapshot: the legacy aggregate fields (UI chip, health)
    plus a per-device list.

Back-compat: a Connector that never sends a device_id (a pre-multi-device
bundle) is tracked under a generated per-connection id and participates in
default routing like any other device.

Everything runs on the app's asyncio loop (the WS endpoint + the async routes
that call `send_command` share it), so the pending-request maps need no locking.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Optional

log = logging.getLogger("mimi_lab.connector")

# Consider a device "present" only if we've heard from it within this window
# (it heartbeats well inside this). Guards against a half-open socket.
STALE_AFTER = 90.0
# Forget a device entirely after this long without a frame — it re-registers on
# reconnect anyway; this just keeps ghosts out of the device list.
PRUNE_AFTER = 3600.0


class ConnectorOffline(RuntimeError):
    """No (matching) Connector is connected (UI should say 'Start the Connector')."""


class ConnectorTimeout(RuntimeError):
    """The Connector is connected but didn't ack a command in time."""


class ConnectorDevice:
    """One watching machine's live Connector connection + reported state."""

    def __init__(self, ws, device_id: str, provisional: bool) -> None:
        self.ws = ws
        self.device_id = device_id
        self.device_name: Optional[str] = None
        # True until the Connector names itself (old bundles never do).
        self.provisional = provisional
        self.chrome = False
        self.migaku = False
        self.version: Optional[str] = None
        self.ext_version: Optional[str] = None
        now = time.time()
        self.attached_at = now
        self.last_seen = now
        # Last *meaningful* activity (register/ack/telemetry, not heartbeats) —
        # the tie-break for default routing: "the device you actually use".
        self.last_active = now
        self.pending: dict[str, asyncio.Future] = {}

    @property
    def fresh(self) -> bool:
        return (time.time() - self.last_seen) < STALE_AFTER

    @property
    def label(self) -> str:
        return self.device_name or self.device_id

    def fail_pending(self, exc: Exception) -> None:
        for fut in list(self.pending.values()):
            if not fut.done():
                fut.set_exception(exc)
        self.pending.clear()

    def snapshot(self) -> dict:
        on = self.fresh
        return {
            "device_id": self.device_id,
            "device_name": self.device_name,
            "connected": on,
            "chrome": self.chrome if on else False,
            "migaku": self.migaku if on else False,
            "last_seen": self.last_seen or None,
            "version": self.version,
            "ext_version": self.ext_version,
            "provisional": self.provisional,
        }


class ConnectorRegistry:
    def __init__(self) -> None:
        self._devices: dict[str, ConnectorDevice] = {}   # device_id -> device
        self._by_ws: dict[object, ConnectorDevice] = {}  # live socket -> device
        self._seq = 0        # command rid counter (unique across devices)
        self._conn_seq = 0   # provisional-id counter
        # Migaku extension versions: last seen per device_id (survives that
        # device's reconnects) + last seen anywhere (status fallback).
        self._ext_by_device: dict[str, str] = {}
        self._ext_version: Optional[str] = None

    # ----------------------------------------------------------------- lifecycle
    async def attach(self, ws) -> ConnectorDevice:
        """Register a freshly-accepted Connector socket.

        The device identity arrives in its first register frame; until then the
        connection is tracked under a provisional id. Other devices' sockets are
        NOT touched — only a register with the same device_id supersedes.
        """
        self._prune()
        self._conn_seq += 1
        dev = ConnectorDevice(ws, f"conn-{self._conn_seq}", provisional=True)
        self._by_ws[ws] = dev
        self._devices[dev.device_id] = dev
        log.info("connector attached (awaiting register; provisional id=%s)", dev.device_id)
        return dev

    async def detach(self, ws) -> None:
        """Drop a Connector socket (on disconnect). No-op if it was already
        superseded by a newer connection from the same device."""
        dev = self._by_ws.pop(ws, None)
        if dev is None:
            return
        if self._devices.get(dev.device_id) is dev:
            del self._devices[dev.device_id]
        dev.fail_pending(ConnectorOffline(f"connector '{dev.label}' disconnected"))
        log.info("connector detached (%s)", dev.label)

    def _prune(self) -> None:
        """Forget devices we haven't heard from in PRUNE_AFTER (ghost sockets)."""
        now = time.time()
        for did, dev in list(self._devices.items()):
            if now - dev.last_seen > PRUNE_AFTER:
                del self._devices[did]
                self._by_ws.pop(dev.ws, None)
                dev.fail_pending(ConnectorOffline(f"connector '{dev.label}' went away"))
                log.info("connector pruned after %ds silence (%s)", int(PRUNE_AFTER), dev.label)

    async def _identify(self, dev: ConnectorDevice, device_id, device_name) -> None:
        """Adopt the device identity from a register/heartbeat frame.

        A live entry already holding that device_id (the same machine
        reconnecting over a half-open socket) is superseded — its socket is
        closed with 4000 and its pending commands fail, exactly the old
        single-slot semantics, but scoped to that one device.
        """
        if device_name:
            dev.device_name = str(device_name)[:80]
        if not device_id:
            return
        device_id = str(device_id)[:80]
        if device_id == dev.device_id:
            dev.provisional = False
            return
        existing = self._devices.get(device_id)
        if existing is not None and existing is not dev:
            self._by_ws.pop(existing.ws, None)
            existing.fail_pending(
                ConnectorOffline("connector replaced by a new connection from the same device")
            )
            try:
                await existing.ws.close(code=4000)
            except Exception:
                pass
            log.info("connector '%s' superseded by a new connection", device_id)
        if self._devices.get(dev.device_id) is dev:
            del self._devices[dev.device_id]
        dev.device_id = device_id
        dev.provisional = False
        self._devices[device_id] = dev

    # ----------------------------------------------------------- inbound dispatch
    async def handle_message(self, ws, msg: dict) -> None:
        """Process a frame from a Connector: state updates, command acks,
        playback telemetry, session lifecycle, and download-progress relays.
        Frames are attributed to the device that owns `ws`."""
        dev = self._by_ws.get(ws)
        if dev is None:  # frame raced a detach/supersede — drop it
            return
        t = msg.get("type")
        dev.last_seen = time.time()
        if t in ("register", "heartbeat", "status"):
            await self._identify(dev, msg.get("device_id"), msg.get("device_name"))
            if "chrome" in msg:
                dev.chrome = bool(msg.get("chrome"))
            if "migaku" in msg:
                dev.migaku = bool(msg.get("migaku"))
            if msg.get("version"):
                dev.version = str(msg.get("version"))
            if msg.get("ext_version"):
                self._note_ext_version(dev, str(msg.get("ext_version")))
            if t == "register":
                dev.last_active = time.time()
                log.info(
                    "connector registered: %s (chrome=%s migaku=%s v=%s)",
                    dev.label, dev.chrome, dev.migaku, dev.version,
                )
        elif t == "ack":
            dev.last_active = time.time()
            fut = dev.pending.pop(msg.get("id"), None)
            if fut is not None and not fut.done():
                fut.set_result(msg)
        elif t == "telemetry":
            dev.last_active = time.time()
            try:
                from ..watch.service import record_telemetry
                record_telemetry({**msg, "device_id": dev.device_id, "device_name": dev.device_name})
            except Exception as e:
                log.debug("telemetry handling failed: %s", e)
        elif t == "session-end":
            dev.last_active = time.time()
            try:
                from ..watch.service import record_session_end
                record_session_end({**msg, "device_id": dev.device_id})
            except Exception as e:
                log.debug("session-end handling failed: %s", e)
        elif t == "progress":
            # media download progress for the current play — relay to the UI
            try:
                from ..events import bus
                bus.publish("play-progress", {
                    "episode_id": msg.get("episode_id"),
                    "pct": msg.get("pct"),
                    "phase": msg.get("phase"),
                    "device_id": dev.device_id,
                })
            except Exception:
                pass
        else:
            log.debug("connector: ignoring frame type=%r", t)

    def _note_ext_version(self, dev: ConnectorDevice, ver: str) -> None:
        """Track the Migaku extension version; record an event when it changes
        ON THE SAME DEVICE (an extension update is the #1 breakage source for
        the whole reverse-engineered integration). Compared per device — two
        devices legitimately running different versions must not ping-pong
        warnings at each other."""
        dev.ext_version = ver
        self._ext_version = ver
        prev = self._ext_by_device.get(dev.device_id)
        self._ext_by_device[dev.device_id] = ver
        if prev and prev != ver:
            log.warning(
                "Migaku extension updated on %s: %s -> %s", dev.label, prev, ver
            )
            try:
                from ..events import service as events
                events.record(
                    "system",
                    f"Migaku extension updated on {dev.label}: {prev} → {ver}",
                    "warning",
                    detail="Run the self-check (Settings → System) and, if tokenization "
                           "drifted, restart the tokenizer sidecar + re-run the backfill.",
                )
            except Exception:
                pass
        else:
            # persist the last-seen version across restarts for the drift check
            try:
                from ..db import kv_set
                kv_set("migaku.ext_version.connector", ver)
            except Exception:
                pass

    # ------------------------------------------------------------------- routing
    def resolve_device(self, device_id: Optional[str] = None) -> ConnectorDevice:
        """Pick the device a command should go to.

        Explicit `device_id` → that device (ConnectorOffline if absent/stale).
        None → the default: the only connected device when there's one (the
        old single-device behavior), otherwise prefer Migaku-ready devices,
        tie-broken by most recent real activity (last play/ack/telemetry).
        """
        self._prune()
        if device_id:
            dev = self._devices.get(device_id)
            if dev is None or not dev.fresh:
                raise ConnectorOffline(f"Connector device '{device_id}' is not connected")
            return dev
        live = [d for d in self._devices.values() if d.fresh]
        if not live:
            raise ConnectorOffline("no Connector is connected")
        if len(live) == 1:
            return live[0]
        live.sort(key=lambda d: (d.migaku, d.last_active, d.last_seen), reverse=True)
        return live[0]

    # ---------------------------------------------------------- outbound command
    async def send_command(
        self, cmd: dict, timeout: float = 45.0, device_id: Optional[str] = None
    ) -> dict:
        """Push a command to a Connector and await its ack.

        `device_id` targets a specific device; None routes to the default (see
        resolve_device). Raises ConnectorOffline if no matching Connector is
        connected / the send fails, or ConnectorTimeout if no ack arrives. On a
        Connector-reported failure (`ok:false`) raises RuntimeError with its
        error string.
        """
        dev = self.resolve_device(device_id)
        self._seq += 1
        rid = f"r{self._seq}-{int(time.time() * 1000)}"
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        dev.pending[rid] = fut
        try:
            await dev.ws.send_text(json.dumps({"type": "command", "id": rid, **cmd}))
        except Exception as e:
            dev.pending.pop(rid, None)
            raise ConnectorOffline(f"connector send failed ({dev.label}): {e}")
        try:
            ack = await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            dev.pending.pop(rid, None)
            raise ConnectorTimeout(
                f"connector '{dev.label}' did not ack '{cmd.get('cmd')}' within {timeout:.0f}s"
            )
        if not ack.get("ok", False):
            raise RuntimeError(ack.get("error") or "connector reported failure")
        return ack

    # -------------------------------------------------------------------- status
    @property
    def connected(self) -> bool:
        return any(d.fresh for d in self._devices.values())

    def status(self) -> dict:
        """Legacy aggregate shape (UI chip / health) + the per-device list.

        Aggregate semantics with multiple devices: connected/chrome/migaku are
        true if ANY connected device has them; version/ext_version come from
        the default device (where an untargeted Play would go).
        """
        self._prune()
        devices = sorted(
            self._devices.values(),
            key=lambda d: (d.fresh, d.last_active, d.last_seen),
            reverse=True,
        )
        live = [d for d in devices if d.fresh]
        default: Optional[ConnectorDevice] = None
        try:
            default = self.resolve_device(None)
        except ConnectorOffline:
            pass
        pick = default or (devices[0] if devices else None)
        return {
            "connected": bool(live),
            "chrome": any(d.chrome for d in live),
            "migaku": any(d.migaku for d in live),
            "last_seen": max((d.last_seen for d in devices), default=None),
            "version": pick.version if pick else None,
            "ext_version": (pick.ext_version if pick else None) or self._ext_version,
            "device_count": len(live),
            "default_device_id": default.device_id if default else None,
            "devices": [d.snapshot() for d in devices],
        }


# module-level singleton (one per server; many Connector devices attach to it)
registry = ConnectorRegistry()
