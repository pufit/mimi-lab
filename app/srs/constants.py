"""SRS constants (notes/SRS_DESIGN.md §9.5).

These are the product, not knobs: nothing here is exposed as a setting. The only
learner-facing choices are the three fields of `SrsSettings` (settings.py).
"""
from __future__ import annotations

# --- scheduler (§3) --------------------------------------------------------
DESIRED_RETENTION = 0.90
MAX_INTERVAL_DAYS = 365
LEARNING_STEPS_MIN = [1, 10]
RELEARNING_STEPS_MIN = [10]
DAY_CUTOFF_HOUR = 4
KNOWN_INTERVAL_DAYS = 21
MAX_DEMOTIONS = 3
MAX_REVIEWS_PER_DAY = 200
LEARN_AHEAD_MIN = 20
FAIL_MIN_GAP_HOURS = 8
DEMOTE_SWAP_MOMENT = True

# --- stack & generation (§4.4, §5) -----------------------------------------
STACK_TARGET = 60
STACK_MIN = 40
MAX_WANT_PER_RUN = 40
WORDS_PER_JUDGE_CALL = 4
MOMENTS_PER_WORD = 5

# --- judge (§5.5) ----------------------------------------------------------
JUDGE_MODEL_SETTING = "translation_model"
JUDGE_MAX_TOKENS = 4096
JUDGE_TIMEOUT_S = 90.0
JUDGE_MIN_CLARITY = 0.75

# --- census filters (§5.2) -------------------------------------------------
MAX_OTHER_UNKNOWNS = 2
MAX_FREQ_RANK = 20000
BASIC_RANK = 1000
REJUDGE_NEW_LINES = 3
FRAGMENT_MIN_STANDALONE = 0.5
NAME_MAX_TITLES = 2
LYRIC_REPEATS = 3
LYRIC_PAIR_MIN_CHARS = 10

# --- housekeeping (§5.8, §6.6, §2.7) ---------------------------------------
STALE_RUN_HOURS = 2
STALE_CLIP_MIN = 30
RELINK_TOLERANCE_MS = 250

# --- clips (§6) ------------------------------------------------------------
CLIP_HEIGHT = 720
CLIP_LEAD_MS = 350
CLIP_TAIL_MS = 500
MIN_LINE_MS = 800
BLEED_MS = 200
MAX_CLIP_MS = 12000
# A clip that carries evidence lines (§6.2 "Evidence lines") may run longer than
# a single cue, but never past this: the farthest evidence lines are dropped
# until the window fits and `extend_truncated` is recorded in meta.json.
# (20 s → 30 s on 2026-09-03: the judge may lean on lines farther back.)
EXTENDED_MAX_CLIP_MS = 30000
# How many evidence lines one moment may carry (hard clamp after parsing; the
# clip cap below drops the farthest ones anyway).
MAX_EVIDENCE_LINES = 6
# An evidence line must be a neighbour of the target: same episode and within
# this many subtitle indices — sized to the blind judge's widest window
# (2026-09-03; was 2 = the ±2 writer window).
MAX_EVIDENCE_IDX_DISTANCE = 16
# A cue whose span CONTAINS the target and runs this much longer than it is a
# long-running sign, not a neighbour. Rows of one two-line cue share identical
# timings (the ingest stores each line as its own row) and must stay neighbours.
SIGN_EXTRA_MS = 1500
DISK_LOW_BYTES = 3 * 2**30
CLIP_BYTES_WARN = 4 * 2**30

# --- multi-card words (2026-09-03, the user: "allow multiple cards for the same
# word (not more than 3 in total), but from different animes only") ----------
# Independent sibling cards, each with its own FSRS state; no sibling rules
# beyond the count and the franchise (`franchise.franchise_key` decides what
# counts as the same anime — seasons of one show are one anime).
MAX_CARDS_PER_LEMMA = 3

# --- translation-blind clarity gate (2026-09-03, `blind_judge.py`) -----------
# "The translation is a check, not a cue": a moment is card-worthy only when the
# masked target can be inferred from the Japanese context alone AND the guess
# matches the actual meaning. Opus 5 infers; the translation model (Sonnet)
# grades guess-vs-meaning.
BLIND_JUDGE_MODEL = "claude-opus-5"
# The categorical verdict (single/narrow + the guess matches) is the robust
# signal; the model's confidence hedges on learner behaviour and sat at
# 0.5–0.72 for moments a human calls clear (measured 2026-09-03: 救う after
# 助けてやりたい 0.55; 命を救ってやんなきゃ 0.72). 0.5 is a floor, not the bar.
BLIND_MIN_CONFIDENCE = 0.5
# Dialogue lines shown around the target: what a viewer has in mind (the user,
# 2026-09-03: "LLM should adjust context themself") — the judge may flag
# `need_more_context`, which re-runs it once with the *_MAX window.
BLIND_CONTEXT_BEFORE = 8
BLIND_CONTEXT_AFTER = 4
BLIND_CONTEXT_BEFORE_MAX = 16
BLIND_CONTEXT_AFTER_MAX = 8
BLIND_MAX_TOKENS = 4000                        # adaptive thinking counts against max_tokens
BLIND_TIMEOUT_S = 180.0
BLIND_MATCH_MAX_TOKENS = 400
BLIND_MATCH_TIMEOUT_S = 60.0
BLIND_WORKERS = 6                              # parallel blind calls inside one judge job
