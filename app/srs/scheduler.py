"""FSRS-6 scheduler — pure functions, injectable clock and RNG (§3).

Ported from py-fsrs 6.3.2 (no dependency: its `Card` state machine has no
demotion hook and we want day-granular review due dates). Every function here is
side-effect free; `service.py` owns the transaction.

The one deliberate divergence from a naive port is the elapsed time `t` fed to
`R(t, S)`: it is `srs_day(now) - srs_day(last_review_at)` in whole SRS days
(Anki-style), never a timestamp delta — review due dates are floored to the
04:00 cutoff, so a card graduated at 23:00 with I=1 is due 5 h later and a
timestamp delta would report `.days == 0` forever (§3.1).
"""
from __future__ import annotations

import math
import random
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional

from .constants import (            # noqa: F401  (re-exported for the service)
    DAY_CUTOFF_HOUR,
    DESIRED_RETENTION,
    LEARNING_STEPS_MIN,
    MAX_INTERVAL_DAYS,
    RELEARNING_STEPS_MIN,
)

# py-fsrs 6.3.2 defaults (21 values, §3.1)
DEFAULT_PARAMETERS: tuple[float, ...] = (
    0.212, 1.2931, 2.3065, 8.2956, 6.4133, 0.8334, 3.0194, 0.001, 1.8722, 0.1666,
    0.796, 1.4835, 0.0614, 0.2629, 1.6483, 0.6014, 1.8729, 0.5425, 0.0912, 0.0658, 0.1542,
)
STABILITY_MIN = 0.001
STABILITY_MAX = 36500.0
DIFFICULTY_MIN = 1.0
DIFFICULTY_MAX = 10.0

W = DEFAULT_PARAMETERS
DECAY = -W[20]                                  # -0.1542
FACTOR = 0.9 ** (1.0 / DECAY) - 1.0

# States the scheduler itself can produce.
LEARNING_STATES = ("learning", "relearning")


# ---------------------------------------------------------------------------
# Day boundary (§3.4) — never reuse a captured tzinfo (DST)
# ---------------------------------------------------------------------------

def srs_day(ts_utc: datetime) -> date:
    """The SRS day (local date, 04:00 cutoff) an aware UTC instant belongs to.

    Uses `.astimezone()` with no argument so the OS resolves the offset for THAT
    instant — a captured `tzinfo` is a fixed offset and breaks across DST (§3.4).
    """
    if ts_utc.tzinfo is None:
        ts_utc = ts_utc.replace(tzinfo=timezone.utc)
    return (ts_utc.astimezone() - timedelta(hours=DAY_CUTOFF_HOUR)).date()


def day_start(d: date) -> datetime:
    """Aware UTC instant at which SRS day `d` begins (local 04:00, §3.4)."""
    naive_local = datetime.combine(d, time(DAY_CUTOFF_HOUR))
    return naive_local.astimezone(timezone.utc)


def elapsed_days(last_review_at: Optional[datetime], now: datetime) -> int:
    """`t` for the FSRS formulas: whole SRS days between the last review and now
    (Anki-style, not a timestamp delta — §3.1). 0 when there is no last review."""
    if last_review_at is None:
        return 0
    return max(0, (srs_day(now) - srs_day(last_review_at)).days)


# ---------------------------------------------------------------------------
# The FSRS-6 formulas (§3.1)
# ---------------------------------------------------------------------------

def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def retrievability(t: float, stability: float) -> float:
    """R(t, S) — probability of recall after `t` SRS days at stability S."""
    s = max(float(stability), STABILITY_MIN)
    return (1.0 + FACTOR * t / s) ** DECAY


def interval_days(stability: float, *, desired_retention: float = DESIRED_RETENTION) -> int:
    """I(S) — the review interval in whole days (== round(S) at r = 0.90)."""
    raw = (stability / FACTOR) * (desired_retention ** (1.0 / DECAY) - 1.0)
    return int(_clamp(round(raw), 1, MAX_INTERVAL_DAYS))


def _interval_raw(stability: float, *, desired_retention: float = DESIRED_RETENTION) -> float:
    return (stability / FACTOR) * (desired_retention ** (1.0 / DECAY) - 1.0)


def initial_stability(rating: int) -> float:
    return _clamp(W[rating - 1], STABILITY_MIN, STABILITY_MAX)


def initial_difficulty(rating: int) -> float:
    return _clamp(_initial_difficulty_raw(rating), DIFFICULTY_MIN, DIFFICULTY_MAX)


def _initial_difficulty_raw(rating: int) -> float:
    """D0 without the [1, 10] clamp — the Easy value is the mean-reversion target."""
    return W[4] - math.e ** (W[5] * (rating - 1)) + 1.0


def next_difficulty(difficulty: float, rating: int) -> float:
    """D'(D, G) — linear damping + mean reversion toward the unclamped D0(Easy)."""
    delta = -(W[6] * (rating - 3)) * (10.0 - difficulty) / 9.0
    return _clamp(
        W[7] * _initial_difficulty_raw(4) + (1.0 - W[7]) * (difficulty + delta),
        DIFFICULTY_MIN,
        DIFFICULTY_MAX,
    )


def stability_after_recall(stability: float, difficulty: float, r: float, rating: int) -> float:
    hard_penalty = W[15] if rating == 2 else 1.0
    easy_bonus = W[16] if rating == 4 else 1.0
    return stability * (
        1.0
        + math.e ** W[8]
        * (11.0 - difficulty)
        * stability ** (-W[9])
        * (math.e ** ((1.0 - r) * W[10]) - 1.0)
        * hard_penalty
        * easy_bonus
    )


def stability_after_forget(stability: float, difficulty: float, r: float) -> float:
    long_term = (
        W[11]
        * difficulty ** (-W[12])
        * ((stability + 1.0) ** W[13] - 1.0)
        * math.e ** ((1.0 - r) * W[14])
    )
    short_term = stability / math.e ** (W[17] * W[18])
    return min(long_term, short_term)


def stability_short_term(stability: float, rating: int) -> float:
    """Same-SRS-day update (`t == 0`). A passing grade never lowers S."""
    factor = math.e ** (W[17] * (rating - 3 + W[18])) * stability ** (-W[19])
    if rating in (2, 3, 4):
        factor = max(factor, 1.0)
    return stability * factor


def _fuzz(interval: float, rng: random.Random) -> int:
    """Anki-style interval fuzz; only applied to review intervals >= 2.5 d."""
    i = interval
    delta = 1.0
    delta += 0.15 * max(min(i, 7.0) - 2.5, 0.0)
    delta += 0.10 * max(min(i, 20.0) - 7.0, 0.0)
    delta += 0.05 * max(i - 20.0, 0.0)
    min_ivl = max(2, round(i - delta))
    max_ivl = min(MAX_INTERVAL_DAYS, round(i + delta))
    min_ivl = min(min_ivl, max_ivl)
    return int(min(round(rng.random() * (max_ivl - min_ivl + 1) + min_ivl), MAX_INTERVAL_DAYS))


# ---------------------------------------------------------------------------
# The state machine (§3.2)
# ---------------------------------------------------------------------------

def _as_dt(value) -> Optional[datetime]:
    """Accept a datetime, a sqlite 'YYYY-MM-DD HH:MM:SS' UTC string or None."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip().replace("T", " ").removesuffix("Z")
    try:
        return datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def schedule(
    card: dict,
    rating: int,
    now: datetime,
    *,
    rng: Optional[random.Random] = None,
) -> dict:
    """Apply one rating to a card snapshot and return the changed scheduler
    fields (`state, step, stability, difficulty, due_at, scheduled_days,
    last_review_at, reps, lapses`). `rng=None` disables fuzz (previews, tests).

    `card` carries the current `state, step, stability, difficulty,
    last_review_at, scheduled_days, reps, lapses`. Nothing is written here; the
    demotion rule (§3.7) is applied by the service afterwards.
    """
    if rating not in (1, 2, 3, 4):
        raise ValueError(f"bad rating {rating!r}")
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    state = (card.get("state") or "new") or "new"
    step = card.get("step")
    stability = card.get("stability")
    difficulty = card.get("difficulty")
    last_review_at = _as_dt(card.get("last_review_at"))
    reps = int(card.get("reps") or 0)
    lapses = int(card.get("lapses") or 0)

    # --- memory state ------------------------------------------------------
    if stability is None or difficulty is None:
        # first rating of the current introduction
        new_s = initial_stability(rating)
        new_d = initial_difficulty(rating)
        t = 0
    else:
        stability = float(stability)
        difficulty = float(difficulty)
        t = elapsed_days(last_review_at, now)
        if t <= 0:
            new_s = stability_short_term(stability, rating)
        else:
            r = retrievability(t, stability)
            new_s = (
                stability_after_forget(stability, difficulty, r)
                if rating == 1
                else stability_after_recall(stability, difficulty, r, rating)
            )
        new_d = next_difficulty(difficulty, rating)
    new_s = _clamp(new_s, STABILITY_MIN, STABILITY_MAX)

    # --- transition --------------------------------------------------------
    if state in ("review", "known"):
        if rating == 1:
            out_state, out_step = "relearning", 0
            due = now + timedelta(minutes=RELEARNING_STEPS_MIN[0])
            scheduled = 0
            lapses += 1
        else:
            out_state, out_step, due, scheduled = _to_review(new_s, now, rng)
    else:
        # new / learning / relearning all run the step machine
        relearning = state == "relearning"
        steps = RELEARNING_STEPS_MIN if relearning else LEARNING_STEPS_MIN
        base_state = "relearning" if relearning else "learning"
        idx = int(step) if step is not None else 0
        idx = max(0, min(idx, len(steps) - 1))

        if rating == 1:
            out_state, out_step = base_state, 0
            due = now + timedelta(minutes=steps[0])
            scheduled = 0
        elif rating == 2:
            out_state = base_state
            if idx == 0:
                minutes = steps[0] * 1.5 if len(steps) == 1 else (steps[0] + steps[1]) / 2.0
            else:
                minutes = float(steps[idx])
            out_step = idx
            due = now + timedelta(minutes=minutes)
            scheduled = 0
        elif rating == 3:
            if idx + 1 >= len(steps):
                out_state, out_step, due, scheduled = _to_review(new_s, now, rng)
            else:
                out_state, out_step = base_state, idx + 1
                due = now + timedelta(minutes=steps[idx + 1])
                scheduled = 0
        else:  # Easy
            out_state, out_step, due, scheduled = _to_review(new_s, now, rng)

    return {
        "state": out_state,
        "step": out_step,
        "stability": new_s,
        "difficulty": new_d,
        "due_at": due,
        "scheduled_days": scheduled,
        "last_review_at": now,
        "reps": reps + 1,
        "lapses": lapses,
        "elapsed_days": t,
    }


def _to_review(stability: float, now: datetime, rng: Optional[random.Random]):
    """Enter/stay in review: day-granular due date at the 04:00 cutoff (§3.4)."""
    raw = _interval_raw(stability)
    ivl = int(_clamp(round(raw), 1, MAX_INTERVAL_DAYS))
    if rng is not None and raw >= 2.5:
        ivl = _fuzz(raw, rng)
    due = day_start(srs_day(now) + timedelta(days=ivl))
    return "review", None, due, ivl


# ---------------------------------------------------------------------------
# Interval previews (§3.9)
# ---------------------------------------------------------------------------

def format_interval(seconds: float) -> str:
    """`<60 min → Nm`, `<24 h → Nh`, `<30 d → Nd`, `<365 d → N.Nmo`, else `N.Ny`."""
    seconds = max(0.0, float(seconds))
    minutes = seconds / 60.0
    if minutes < 60.0:
        return f"{max(1, int(minutes))}m"
    hours = minutes / 60.0
    if hours < 24.0:
        return f"{int(hours)}h"
    days = hours / 24.0
    if days < 30.0:
        return f"{max(1, int(round(days)))}d"
    if days < 365.0:
        return f"{days / 30.44:.1f}mo"
    return f"{days / 365.0:.1f}y"


def preview(card: dict, now: datetime) -> dict:
    """`{"again": "1m", "hard": "5m", "good": "10m", "easy": "8d"}` — the four
    ratings simulated with fuzz off (§3.9). Never writes."""
    out: dict[str, str] = {}
    for name, rating in (("again", 1), ("hard", 2), ("good", 3), ("easy", 4)):
        res = schedule(card, rating, now, rng=None)
        if res["state"] == "review":
            out[name] = format_interval(res["scheduled_days"] * 86400)
        else:
            out[name] = format_interval((res["due_at"] - now).total_seconds())
    return out
