"""Aggregated system health.

`/api/health` used to be a constant `{ok: true}` while every real probe
already existed elsewhere (tokenizer sidecar, Transmission, Connector
registry, MAL token expiry, job-queue error counts). This module aggregates
them for the System page. Probes are best-effort with short timeouts; a
failing probe never raises out of `full_report()`.
"""
from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path

from .config import settings
from .db import connect, kv_get

log = logging.getLogger("mimi_lab.health")


def _db_report() -> dict:
    try:
        db = Path(settings.mimi_lab_db)
        size = db.stat().st_size if db.exists() else 0
        wal = db.with_name(db.name + "-wal")
        wal_size = wal.stat().st_size if wal.exists() else 0
        return {"ok": True, "size_bytes": size, "wal_bytes": wal_size}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _disk_report() -> dict:
    try:
        usage = shutil.disk_usage(str(settings.library_dir))
        free_gb = usage.free / 1e9
        return {"ok": free_gb > 5.0, "free_gb": round(free_gb, 1),
                "total_gb": round(usage.total / 1e9, 1)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _tokenizer_report() -> dict:
    try:
        from .learn import migaku_tok
        h = migaku_tok.health(timeout=2.0)
        return {"ok": bool(h.get("ok")), "ext_version": h.get("ext"), "lang": h.get("lang")}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _transmission_report() -> dict:
    try:
        from .acquire import service as acquire
        return {"ok": acquire.qbt_available()}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _connector_report() -> dict:
    try:
        from .connector.service import registry
        st = registry.status()
        return {
            "ok": bool(st.get("connected")),
            "connected": bool(st.get("connected")),
            "chrome": st.get("chrome"),
            "migaku": st.get("migaku"),
            "version": st.get("version"),
            "last_seen": st.get("last_seen"),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _mal_report() -> dict:
    try:
        configured = bool(settings.mal_client_id and settings.mal_client_secret)
        expires_at = kv_get("mal_expires_at")
        last_sync = kv_get("mal_last_sync")
        authed = bool(kv_get("mal_refresh"))
        expires_in_days = None
        if expires_at:
            expires_in_days = round((int(expires_at) - time.time()) / 86400, 1)
        return {
            # authed-and-configured is healthy; unconfigured is "n/a", not broken
            "ok": authed or not configured,
            "configured": configured,
            "authed": authed,
            "expires_in_days": expires_in_days,
            "last_sync": int(last_sync) if last_sync else None,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _jobs_report() -> dict:
    try:
        from .jobs.service import job_stats
        st = job_stats()
        states = st.get("states", {})
        return {
            "ok": (states.get("error", 0) == 0),
            "queued": states.get("queued", 0),
            "running": states.get("running", 0),
            "error": states.get("error", 0),
            "parked": states.get("parked", 0),
            "oldest_queued": st.get("oldest_queued"),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _jimaku_report() -> dict:
    return {"ok": bool(settings.jimaku_token), "configured": bool(settings.jimaku_token)}


def _anthropic_report() -> dict:
    try:
        from . import llm
        usage = None
        try:
            usage = llm.usage_summary()
        except Exception:
            pass
        return {"ok": True, "configured": llm.available(), "usage": usage}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def full_report() -> dict:
    """Aggregate every component probe. ~2s worst case (network probes have
    short timeouts). `ok` = every *required* component healthy; optional
    integrations (MAL/jimaku/anthropic unconfigured) don't fail the aggregate.
    """
    components = {
        "db": _db_report(),
        "disk": _disk_report(),
        "jobs": _jobs_report(),
        "tokenizer": _tokenizer_report(),
        "transmission": _transmission_report(),
        "connector": _connector_report(),
        "mal": _mal_report(),
        "jimaku": _jimaku_report(),
        "anthropic": _anthropic_report(),
    }
    required = ("db", "disk", "tokenizer", "transmission")
    ok = all(components[c].get("ok") for c in required)
    return {"ok": ok, "service": "mimi-lab", "version": "0.2.0", "components": components}
