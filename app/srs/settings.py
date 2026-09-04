"""SRS settings — kv `srs.settings` merged over the defaults (§9.5).

Three genuine product choices; everything else is a constant (constants.py).
`get_settings()` never raises and never writes: a missing/corrupt kv row, an
unknown key or an out-of-range value falls back to the default for that field,
so a hand-edited kv value can never break the scheduler.
"""
from __future__ import annotations

import json
import logging

from ..db import kv_get, kv_set
from ..models import SrsSettings, SrsSettingsPatch

log = logging.getLogger("mimi_lab.srs.settings")

KV_KEY = "srs.settings"

# field -> (lo, hi) for the two integer settings (§9.5)
_RANGES: dict[str, tuple[int, int]] = {
    "new_per_day": (0, 50),
    "demote_after_fails": (0, 5),
}
_MOMENT_SOURCE = ("any", "watched_only")

__all__ = ["SrsSettings", "SrsSettingsPatch", "KV_KEY", "get_settings", "update_settings"]


def _coerce(raw: dict) -> dict:
    """Keep known keys with usable values; drop everything else (unknown keys are
    ignored, out-of-range values fall back to the field default)."""
    out: dict = {}
    for field, (lo, hi) in _RANGES.items():
        if field not in raw:
            continue
        try:
            v = int(raw[field])
        except (TypeError, ValueError):
            continue
        if lo <= v <= hi:
            out[field] = v
    if raw.get("moment_source") in _MOMENT_SOURCE:
        out["moment_source"] = raw["moment_source"]
    return out


def get_settings() -> SrsSettings:
    """Defaults with the kv JSON merged over them."""
    raw = kv_get(KV_KEY)
    if not raw:
        return SrsSettings()
    try:
        doc = json.loads(raw)
    except Exception as e:
        log.warning("kv %s is not valid JSON (%s) — using defaults", KV_KEY, e)
        return SrsSettings()
    if not isinstance(doc, dict):
        return SrsSettings()
    return SrsSettings(**_coerce(doc))


def update_settings(patch: SrsSettingsPatch) -> SrsSettings:
    """Apply a partial update and persist the full merged value. Range violations
    are rejected by the pydantic model (422 at the route)."""
    current = get_settings().model_dump()
    current.update(patch.model_dump(exclude_none=True))
    merged = SrsSettings(**current)
    kv_set(KV_KEY, json.dumps(merged.model_dump(), sort_keys=True))
    return merged
