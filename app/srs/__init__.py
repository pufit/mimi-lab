"""Spaced-repetition study deck (notes/SRS_DESIGN.md).

Layout (§11 file ownership):

  constants.py  the product constants (§9.5) — not knobs
  settings.py   SrsSettings + get_settings() (kv `srs.settings` over defaults)
  scheduler.py  FSRS-6 pure functions (§3)
  snapshot.py   norm_text / dialogue_neighbours / continuation / CardSpec / snapshot_line (§5.7)
  service.py    cards, stack, queue, review, undo, reconcile, relink, summary, stats (§4, §7)
  importer.py   the curated initial deck (§5.11)
  census.py     candidate extraction and scoring (§5.2–5.4)
  judge.py      the LLM moment judge (§5.5)
  generate.py   generation runs and their jobs (§5.8)
  clips.py      ffmpeg clip extraction (§6)
  router.py     every §7.2 endpoint, mounted at /api/srs

Word knowledge state lives in the `srs_*` tables only — never in `known_words`,
which is a full-replace mirror of Migaku's WordList (§2.5).
"""
