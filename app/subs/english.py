"""English secondary-subtitle pipeline.

English here is a native-language **reference / translation track** shown alongside
the Japanese study track in Migaku (you toggle "secondary subtitles" in Migaku's
own settings). It is **never tokenized** — Japanese stays the sole
comprehension / known-words / Moments source. This module does two things:

  1. Puts an aligned ``<base>.en.srt`` next to the video, so the Connector can hand
     it to Migaku as the secondary track and the media resolver can serve it.
  2. Fills ``subtitle_lines.translation`` (per Japanese line) so Moments + the line
     view show the English meaning — by timestamp-overlapping the English cues onto
     the existing Japanese lines.

Source ladder (``settings.english_source``):
  * ``"human"`` (default): the embedded English softsub extracted from the release
    at media post-process (``media.service.extract_embedded_english``).
  * ``"llm"``: machine-translate the Japanese lines with Claude
    (``settings.translation_model``, e.g. Sonnet).

Registered as the ``english_subtitle_fetch`` + ``english_sub_sweep`` job handlers;
chained off the Japanese ``subtitle_fetch`` so the merge has Japanese lines to
attach to. Best-effort + idempotent (exactly one ``lang='en'`` subtitles row/episode).
"""
from __future__ import annotations

import json
import logging
import lzma
import re
import shutil
from pathlib import Path
from typing import Optional

import anitopy
import httpx
import pysubs2

from .. import llm
from ..config import settings
from ..db import connect, cursor, kv_get, kv_set
from .service import _clean_text, _episode_row, _record_event, align, _load_subs, sub_display_sane

log = logging.getLogger("mimi_lab.subs.english")

# Runtime-flippable source toggle. The .env value (settings.english_source) is the
# default; a kv override lets the Settings UI switch human<->llm without a restart.
_KV_ENGLISH_SOURCE = "english_source"
_VALID_SOURCES = ("human", "llm")


def effective_english_source() -> str:
    """The active source — kv override (set from the UI) else the .env default."""
    v = (kv_get(_KV_ENGLISH_SOURCE) or "").lower()
    return v if v in _VALID_SOURCES else (settings.english_source or "human").lower()


def set_english_source(value: str) -> str:
    """Persist the source toggle (kv). Raises ValueError on a bad value."""
    v = (value or "").lower()
    if v not in _VALID_SOURCES:
        raise ValueError(f"english_source must be one of {_VALID_SOURCES}")
    kv_set(_KV_ENGLISH_SOURCE, v)
    return v


def english_config() -> dict:
    """Current English-subtitle configuration for the Settings UI."""
    return {
        "enabled": settings.english_subs_enabled,
        "source": effective_english_source(),
        "default_source": (settings.english_source or "human").lower(),
        "options": list(_VALID_SOURCES),
        "translation_model": settings.translation_model,
        "anthropic_configured": llm.available(),
    }


# --------------------------------------------------------------------------
# paths / lookups
# --------------------------------------------------------------------------
def _en_dest(ep: dict) -> Path:
    """Where the .en.srt lives: next to the video (Migaku smart-pairs by name),
    else under library/_subs keyed by episode id."""
    vp = ep.get("video_path")
    if vp:
        return Path(vp).with_suffix("").with_suffix(".en.srt")
    return settings.library_dir / "_subs" / f"episode_{ep['id']}.en.srt"


def _embedded_sidecar(ep: dict) -> Optional[Path]:
    """The embedded-English artifact the media pipeline extracted from this
    episode's release, if present (``media.service.embedded_sub_artifact``).

    That path is written ONLY by the extraction, so its existence alone proves
    provenance — unlike the served ``<base>.en.srt``, which mt/AnimeTosho runs
    legitimately rewrite. (The old heuristic — 'no subtitles row yet = the file
    is embedded' — silently discarded fresh extractions whenever an EN row
    predated the video, e.g. subs fetched at title-add time; the MT rebuild
    then overwrote the release's own track. Mashle E01-E12, Aug 2026.)
    Structure is still gate-validated on every acceptance, never trusted."""
    vp = ep.get("video_path")
    if not vp:
        return None
    try:
        from ..media.service import embedded_sub_artifact
        p = embedded_sub_artifact(vp)
    except Exception:  # pragma: no cover — defensive cross-module import
        return None
    if not (p.exists() and p.stat().st_size > 0):
        return None
    return p


def _ja_lines(episode_id: int) -> list[dict]:
    """The Japanese study lines for an episode (idx/start/end/text), ordered.

    Scoped to non-English subtitles so an English row can never be its own
    translation target."""
    with connect() as cx:
        rows = cx.execute(
            "SELECT sl.id AS id, sl.idx AS idx, sl.start_ms AS start_ms, "
            "       sl.end_ms AS end_ms, sl.text AS text "
            "FROM subtitle_lines sl JOIN subtitles s ON s.id = sl.subtitle_id "
            "WHERE sl.episode_id=? AND COALESCE(s.lang,'ja') <> 'en' "
            "ORDER BY sl.idx",
            (episode_id,),
        ).fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------
# machine translation (english_source == "llm")
# --------------------------------------------------------------------------
_MT_BATCH = 50  # lines per Claude call


def _mt_translate(texts: list[str]) -> Optional[list[str]]:
    """Translate Japanese subtitle lines to English with Claude
    (settings.translation_model). Returns a list aligned 1:1 to `texts` (blanks
    for any line the model dropped), or None when the LLM is unavailable / wholly
    failed. Never raises.

    A failed batch is retried once with smaller sub-batches (a 50-line batch can
    blow max_tokens and silently blank all 50 — the dominant source of
    translation holes before).
    """
    if not llm.available() or not texts:
        return None
    system = (
        "You are a professional Japanese-to-English anime subtitle translator. "
        "Translate each Japanese subtitle line into natural, concise English. "
        "Return exactly one translation per input line, in the same order; never "
        "merge, split, drop, or add lines. Keep honorifics natural."
    )

    def _call(batch: list[str]) -> Optional[list[str]]:
        user = (
            f"Translate these {len(batch)} Japanese subtitle lines to English. "
            f'Return ONLY JSON of the form {{"t": [ ... exactly {len(batch)} english '
            f"strings, same order ... ]}}.\n\n"
            + json.dumps({"lines": batch}, ensure_ascii=False)
        )
        res = llm.claude_json(
            system, user, model=settings.translation_model,
            max_tokens=4000, timeout=90.0,
        )
        arr = res.get("t") if isinstance(res, dict) else None
        if not isinstance(arr, list) or not any(str(x or "").strip() for x in arr):
            return None
        return [str(x) if x is not None else "" for x in (arr + [""] * len(batch))[:len(batch)]]

    out: list[str] = []
    failed_batches = 0
    for i in range(0, len(texts), _MT_BATCH):
        batch = texts[i:i + _MT_BATCH]
        arr = _call(batch)
        if arr is None:
            # retry in halves — smaller batches fit max_tokens and isolate a
            # single poison line to 25 blanks instead of 50
            arr = []
            half = max(1, len(batch) // 2)
            for j in range(0, len(batch), half):
                sub = batch[j:j + half]
                sub_arr = _call(sub)
                if sub_arr is None:
                    sub_arr = [""] * len(sub)
                    failed_batches += 1
                arr.extend(sub_arr)
        out.extend(arr)
    if failed_batches:
        log.warning("_mt_translate: %d sub-batch(es) failed — holes will be "
                    "re-driven on the next english pass", failed_batches)
    return out if any(s.strip() for s in out) else None


def mt_fill_holes(episode_id: int) -> dict:
    """Translate ONLY the lines still missing a translation for an episode.

    The full `_build_mt_srt` pass wipes and re-translates everything (fine for
    first build, wasteful for repair). This targets holes — the per-Moment
    on-demand translate endpoint and the english sweep both use it.
    """
    if not llm.available():
        return {"ok": False, "reason": "LLM not configured"}
    with connect() as cx:
        rows = cx.execute(
            "SELECT id, text FROM subtitle_lines WHERE episode_id=? "
            "AND (translation IS NULL OR translation='') AND text != '' ORDER BY idx",
            (episode_id,),
        ).fetchall()
    if not rows:
        return {"ok": True, "filled": 0, "holes": 0}
    translations = _mt_translate([r["text"] for r in rows])
    if not translations:
        return {"ok": False, "reason": "translation failed", "holes": len(rows)}
    filled = 0
    with cursor() as cx:
        for r, tr in zip(rows, translations):
            tr = (tr or "").strip()
            if not tr:
                continue
            cx.execute("UPDATE subtitle_lines SET translation=? WHERE id=?", (tr, r["id"]))
            filled += 1
    return {"ok": True, "filled": filled, "holes": len(rows) - filled}


def _build_mt_srt(episode_id: int, ep: dict, dest: Path, force: bool = False) -> Optional[Path]:
    """Machine-translate the Japanese lines, write `dest` (.en.srt from the JA
    timestamps) and set subtitle_lines.translation 1:1. Returns dest or None.

    When the episode is already (mostly) translated and `dest` exists, skips the
    re-translation unless `force` — toggling llm→human→llm used to re-pay for
    the whole episode every time.
    """
    ja = _ja_lines(episode_id)
    if not ja:
        log.info("english(llm): no Japanese lines for ep %s yet — skipping", episode_id)
        return None

    if not force and dest.exists():
        with connect() as cx:
            holes = cx.execute(
                "SELECT COUNT(*) AS n FROM subtitle_lines WHERE episode_id=? "
                "AND (translation IS NULL OR translation='') AND text != ''",
                (episode_id,),
            ).fetchone()["n"]
        if holes == 0:
            log.info("english(llm): ep %s already fully translated — skipping", episode_id)
            return dest
        if holes < len(ja) / 2:
            log.info("english(llm): ep %s repairing %d hole(s) instead of full re-translate",
                     episode_id, holes)
            mt_fill_holes(episode_id)
            return dest

    translations = _mt_translate([r["text"] for r in ja])
    if not translations:
        return None

    subs = pysubs2.SSAFile()
    with cursor() as cx:
        cx.execute("UPDATE subtitle_lines SET translation=NULL WHERE episode_id=?", (episode_id,))
        for r, tr in zip(ja, translations):
            tr = (tr or "").strip()
            if not tr:
                continue
            subs.append(pysubs2.SSAEvent(start=int(r["start_ms"]), end=int(r["end_ms"]), text=tr))
            cx.execute("UPDATE subtitle_lines SET translation=? WHERE id=?", (tr, r["id"]))
    if len(subs) == 0:
        return None
    dest.parent.mkdir(parents=True, exist_ok=True)
    subs.save(str(dest), format_="srt")
    return dest


# --------------------------------------------------------------------------
# AnimeTosho — release-keyed official subtitles (the "human" online source)
# --------------------------------------------------------------------------
# AnimeTosho mirrors the same nyaa releases we download and hosts each release's
# EXTRACTED subtitle tracks as standalone files (e.g. the official Crunchyroll
# English as ``..._track3.eng.ass.xz``). We find the right release by AniDB id
# (the Fribb idmap's per-cour ``anidb_id``) + episode number, verify each
# candidate's ``anidb_aid``, reject recuts/specials, pull the English track,
# xz-decompress + clean it, and align it to our video. Quota-free, release-keyed —
# the English analogue of jimaku-by-AniList-ID.
_AT_FEED = "https://feed.animetosho.org/json"
_AT_UA = {"User-Agent": "Mozilla/5.0 (Mimi Lab)"}
# releases whose timeline doesn't match a normal broadcast episode — never use.
_AT_REJECT = re.compile(
    r"director'?s?\s*cut|新編集版|総集編|shin\s*henshuu|henshuu-?ban|recap|digest|"
    r"break\s*time|\bNCED\b|\bNCOP\b|\bPV\b|preview|\bOVA\b|\bspecial\b|menu",
    re.I,
)
def _at_episode_num(title: str) -> Optional[int]:
    try:
        p = anitopy.parse(title) or {}
        e = p.get("episode_number")
        if isinstance(e, list):
            e = e[0] if e else None
        return int(e) if e is not None else None
    except Exception:
        return None


def _at_rank(entry: dict) -> tuple:
    """Sort key (lower = better): 1080p, then more seeders."""
    title = (entry.get("title") or "").lower()
    res = 0 if "1080" in title else 1
    return (res, -int(entry.get("seeders") or 0))


def _at_english_attachment(view_url: str) -> Optional[str]:
    """The English subtitle attachment (.ass/.srt .xz) on an AnimeTosho view page,
    or None. English tracks are tagged ``.eng.``/``.en.`` in the filename."""
    try:
        html = httpx.get(view_url, headers=_AT_UA, timeout=25, follow_redirects=True).text
    except Exception as e:
        log.info("AnimeTosho view fetch failed (%s): %s", view_url, e)
        return None
    urls = re.findall(r"https://animetosho\.org/storage/attach/[^\"']+?\.(?:ass|srt)\.xz", html)
    eng = [u for u in urls if re.search(r"(?:\.eng|\.en)\.(?:ass|srt)\.xz$", u, re.I)]
    return eng[0] if eng else None


def _animetosho_fetch(ep: dict, dest: Path) -> Optional[Path]:
    """Find + download the release's official English subtitle for this episode via
    AnimeTosho; xz-decompress + clean it to `dest` (.srt). Returns dest or None.

    Keyed by AniDB id (per-cour) + episode, verified against each candidate's
    ``anidb_aid``; recuts/specials rejected. Quota-free."""
    try:
        from ..match import service as match
        anidb = match.anidb_id_for(int(ep.get("anilist_id")))
    except Exception:
        anidb = None
    epn = ep.get("ep_number")
    if anidb is None or epn is None:
        log.info("AnimeTosho: no AniDB id / episode for ep %s — skipping", ep.get("id"))
        return None

    title = ep.get("romaji") or ep.get("english") or ""
    try:
        r = httpx.get(_AT_FEED, params={"q": f"{title} {int(epn):02d}", "only_tor": 1},
                      headers=_AT_UA, timeout=25)
        entries = r.json() if r.status_code == 200 else []
    except Exception as e:
        log.info("AnimeTosho feed failed: %s", e)
        return None

    cands = [
        e for e in entries
        if e.get("anidb_aid") == anidb
        and _at_episode_num(e.get("title") or "") == int(epn)
        and not _AT_REJECT.search(e.get("title") or "")
        and e.get("link")
    ]
    cands.sort(key=_at_rank)
    if not cands:
        log.info("AnimeTosho: no verified release (aid=%s ep=%s)", anidb, epn)
        return None

    for e in cands[:5]:  # best few until one exposes an English attachment
        att = _at_english_attachment(e["link"])
        if not att:
            continue
        try:
            blob = httpx.get(att, headers=_AT_UA, timeout=60, follow_redirects=True).content
            raw = lzma.decompress(blob).decode("utf-8", "replace")
            fmt = "ass" if att.lower().endswith(".ass.xz") else "srt"
            src_subs = pysubs2.SSAFile.from_string(raw, format_=fmt)
        except Exception as ex:
            log.info("AnimeTosho attachment fetch/parse failed: %s", ex)
            continue
        out = pysubs2.SSAFile()
        for sev in src_subs:
            if getattr(sev, "is_comment", False):
                continue
            t = _clean_text(sev.text)
            if t:
                out.append(pysubs2.SSAEvent(start=int(sev.start), end=int(sev.end), text=t))
        if len(out) == 0:
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        out.save(str(dest), format_="srt")
        log.info("AnimeTosho: English for ep %s from %s", ep.get("id"), (e.get("title") or "")[:50])
        return dest

    log.info("AnimeTosho: no English attachment among %d release(s) for ep %s",
             len(cands), ep.get("id"))
    return None


# --------------------------------------------------------------------------
# translation merge (English cues -> subtitle_lines.translation)
# --------------------------------------------------------------------------
def _merge_translation(episode_id: int, en_path: str | Path) -> int:
    """Attach English cues to the Japanese lines by timestamp overlap, writing
    subtitle_lines.translation. Returns the number of lines updated."""
    try:
        ensubs = _load_subs(Path(en_path))
    except Exception as e:
        log.warning("merge: could not load EN subs %s: %s", en_path, e)
        return 0
    cues: list[tuple[int, int, str]] = []
    for ev in ensubs:
        if getattr(ev, "is_comment", False):
            continue
        txt = _clean_text(ev.text)
        if txt:
            cues.append((int(ev.start), int(ev.end), txt))
    if not cues:
        return 0
    cues.sort()

    ja = _ja_lines(episode_id)
    updated = 0
    with cursor() as cx:
        # wipe any prior translations first so a re-fetch can't leave stale lines
        cx.execute("UPDATE subtitle_lines SET translation=NULL WHERE episode_id=?", (episode_id,))
        for r in ja:
            s, e = int(r["start_ms"]), int(r["end_ms"])
            matched = [t for (cs, ce, t) in cues if cs < e and ce > s]  # any time overlap
            if not matched:
                mid = (s + e) // 2  # fallback: nearest cue midpoint within 1.5s
                cs, ce, t = min(cues, key=lambda c: abs(((c[0] + c[1]) // 2) - mid))
                if abs(((cs + ce) // 2) - mid) <= 1500:
                    matched = [t]
            if matched:
                cx.execute(
                    "UPDATE subtitle_lines SET translation=? WHERE id=?",
                    (" ".join(matched), r["id"]),
                )
                updated += 1
    return updated


# --------------------------------------------------------------------------
# subtitles row (lang='en') — NO subtitle_lines (English isn't a corpus)
# --------------------------------------------------------------------------
def _upsert_en_subtitle_row(episode_id: int, path: str | Path, source: str, aligned: int) -> int:
    """Insert/refresh the single English subtitles row for an episode. Never
    creates subtitle_lines — English is a reference track only."""
    with cursor() as cx:
        existing = cx.execute(
            "SELECT id FROM subtitles WHERE episode_id=? AND lang='en' LIMIT 1",
            (episode_id,),
        ).fetchone()
        if existing:
            sub_id = existing["id"]
            cx.execute(
                "UPDATE subtitles SET source=?, path=?, format='srt', aligned=?, "
                "align_tool=NULL, version=version+1 WHERE id=?",
                (source, str(path), aligned, sub_id),
            )
        else:
            cur = cx.execute(
                "INSERT INTO subtitles(episode_id,source,lang,path,format,aligned,version) "
                "VALUES(?,?,?,?,?,?,1)",
                (episode_id, source, "en", str(path), "srt", aligned),
            )
            sub_id = cur.lastrowid
    return sub_id


# --------------------------------------------------------------------------
# orchestrator
# --------------------------------------------------------------------------
def fetch_english_for_episode(episode_id: int) -> dict:
    """Resolve, store + merge the English secondary subtitle for one episode.

    Best-effort + idempotent. Returns a summary dict (status: ok | no_english |
    disabled | no_episode)."""
    if not settings.english_subs_enabled:
        return {"episode_id": episode_id, "status": "disabled"}

    ep = _episode_row(episode_id)
    if not ep:
        return {"episode_id": episode_id, "status": "no_episode"}

    pref = effective_english_source()
    dest = _en_dest(ep)
    have_path: Optional[Path] = None
    used_source: Optional[str] = None
    aligned = 0

    vp = ep.get("video_path")
    if pref == "llm":
        built = _build_mt_srt(episode_id, ep, dest)
        if built:
            have_path, used_source, aligned = built, "mt", 1  # generated from JA timing
    else:  # "human": embedded softsub -> AnimeTosho official -> MT last resort
        emb = _embedded_sidecar(ep)
        if emb:
            # the release's own embedded English (extracted at post-process) — same
            # file as the video, so already perfectly timed. Still gated (structure
            # validated on EVERY acceptance, never trusted from provenance alone),
            # then COPIED onto the served path — the artifact itself stays put so a
            # later run can re-promote it even if a fallback rewrites the served file.
            ok, why = sub_display_sane(emb, vp)
            if ok:
                shutil.copyfile(emb, dest)
                have_path, used_source, aligned = dest, "embedded", 1
            else:
                log.warning("english: embedded sidecar for ep %s rejected (%s)",
                            episode_id, why)
        if not have_path:
            # Release-keyed official English via AnimeTosho, re-timed to our video.
            # The candidate is fetched + aligned at an ISOLATED path and only
            # promoted onto the served .en.srt after it passes the gate — a bad or
            # interrupted candidate can never clobber (or pose as) an accepted
            # track, which is exactly how a broken file previously ended up served.
            cand = dest.with_name(dest.stem + ".cand.srt")
            try:
                at_path = _animetosho_fetch(ep, cand)
                if at_path:
                    align(at_path, vp)  # best-effort re-time to our video
                    # Validate the FINAL candidate (after align — the unverified
                    # re-time can itself push a wrong track out of range, and
                    # alass' split mode can FOLD an over-long wrong-cut track into
                    # range, which only the overlap half of the gate catches).
                    # The embedded and AnimeTosho candidates can originate from
                    # the same bad double-length source, so validate both.
                    ok, why = sub_display_sane(at_path, vp)
                    if ok:
                        at_path.replace(dest)
                        have_path, used_source, aligned = dest, "animetosho", 1
                    else:
                        log.info("english: AnimeTosho track for ep %s rejected (%s) "
                                 "— discarding (mismatched release)", episode_id, why)
            finally:
                cand.unlink(missing_ok=True)
        if not have_path and llm.available():
            # No trustworthy human English (absent, or only mismatched tracks) —
            # machine-translate the JA lines. Timing-correct by construction (built
            # from the JA cue times), so it's always a valid secondary track.
            built = _build_mt_srt(episode_id, ep, dest)
            if built:
                have_path, used_source, aligned = built, "mt", 1

    # Belt-and-braces on whatever won (any source, both prefs): a track that fails
    # the display gate must never be recorded/served — MT output failing here means
    # the JA corpus itself is broken (e.g. doubled lines), which should surface as
    # a loud warning, not ship as a broken English track.
    if have_path:
        ok, why = sub_display_sane(have_path, vp)
        if not ok:
            log.warning("english: %s track for ep %s failed the display gate (%s) "
                        "— not storing", used_source, episode_id, why)
            _record_event(
                "subtitle", f"English track rejected for {_ep_label(episode_id)}", "warning",
                detail=f"source={used_source}: {why}",
                meta={"episode_id": episode_id, "source": used_source},
            )
            if used_source == "mt":
                # a track WE generated must not linger at the served path (the
                # resolver's sidecar fallback would find it); an embedded file
                # stays — it's the extraction artifact, and serving is gated.
                Path(have_path).unlink(missing_ok=True)
            have_path = None

    if not have_path:
        return {"episode_id": episode_id, "status": "no_english", "source_pref": pref}

    sub_id = _upsert_en_subtitle_row(episode_id, have_path, used_source, aligned)
    # MT already set translations 1:1; for human sources merge by timestamp.
    merged = _ja_line_count_with_translation(episode_id) if used_source == "mt" \
        else _merge_translation(episode_id, have_path)

    # A video-timed English track (embedded softsub / AnimeTosho official) is a
    # trusted alignment reference. Re-evaluate the JA study sub against it now that
    # it exists — `align_episode` no-ops if the JA is already well aligned to it,
    # so this is cheap in the common (already-embedded-aligned) case and only does
    # real work when the reference is newly available (e.g. AnimeTosho).
    if used_source in ("embedded", "animetosho"):
        try:
            from ..jobs.service import enqueue
            enqueue("subtitle_align", {"episode_id": episode_id})
        except Exception as e:  # pragma: no cover
            log.info("could not enqueue subtitle_align for ep %s: %s", episode_id, e)

    return {
        "episode_id": episode_id,
        "status": "ok",
        "source": used_source,
        "subtitle_id": sub_id,
        "aligned": bool(aligned),
        "path": str(have_path),
        "translated_lines": merged,
    }


def _ja_line_count_with_translation(episode_id: int) -> int:
    with connect() as cx:
        row = cx.execute(
            "SELECT COUNT(*) AS n FROM subtitle_lines "
            "WHERE episode_id=? AND translation IS NOT NULL AND translation <> ''",
            (episode_id,),
        ).fetchone()
    return row["n"] if row else 0


# --------------------------------------------------------------------------
# job handlers + sweep
# --------------------------------------------------------------------------
def _ep_label(episode_id: int) -> str:
    try:
        ep = _episode_row(episode_id)
        if not ep:
            return f"episode {episode_id}"
        name = ep.get("romaji") or ep.get("english") or f"AniList {ep.get('anilist_id')}"
        en = ep.get("ep_number")
        return f"{name} - E{int(en):02d}" if en is not None else name
    except Exception:
        return f"episode {episode_id}"


def _job_english_subtitle_fetch(payload: dict) -> None:
    episode_id = payload.get("episode_id")
    if episode_id is None:
        raise RuntimeError("english_subtitle_fetch: missing episode_id")
    episode_id = int(episode_id)
    res = fetch_english_for_episode(episode_id)
    if res.get("status") == "ok":
        _record_event(
            "subtitle", f"English subtitle added for {_ep_label(episode_id)}", "success",
            detail=f"source={res.get('source')}, {res.get('translated_lines')} lines translated"
            + ("" if res.get("aligned") else ", unaligned"),
            meta={"episode_id": episode_id, "source": res.get("source"),
                  "subtitle_id": res.get("subtitle_id")},
        )
    elif res.get("status") == "no_english":
        log.info("no English subtitle found for ep %s (pref=%s)", episode_id, res.get("source_pref"))


# how many episodes to chase per English sweep, and how far to stagger them.
_EN_SWEEP_CAP = 20
_EN_SWEEP_STAGGER_SECONDS = 15


def english_sub_sweep(payload: Optional[dict] = None) -> dict:
    """Enqueue `english_subtitle_fetch` for episodes that have a video but no
    English (lang='en') subtitle row yet (capped, staggered). Backfills existing
    episodes + retries ones whose English wasn't available before."""
    if not settings.english_subs_enabled:
        return {"retried": 0, "episode_ids": []}
    payload = payload or {}
    cap = int(payload.get("cap", _EN_SWEEP_CAP))
    force = bool(payload.get("force"))  # re-fetch even episodes that already have an EN row
    from .service import _sweep_batch
    if force:
        where = "e.video_path IS NOT NULL AND e.video_path <> ''"
        cursor_key = "sweep_cursor:english_force"
    else:
        where = (
            "e.video_path IS NOT NULL AND e.video_path <> '' "
            "AND NOT EXISTS (SELECT 1 FROM subtitles s "
            "                WHERE s.episode_id=e.id AND s.lang='en')"
        )
        cursor_key = "sweep_cursor:english"
    episode_ids = _sweep_batch(where, cursor_key, cap)
    if episode_ids:
        try:
            from ..jobs.service import enqueue
            for i, eid in enumerate(episode_ids):
                enqueue("english_subtitle_fetch", {"episode_id": eid},
                        delay_seconds=i * _EN_SWEEP_STAGGER_SECONDS)
        except Exception as e:  # pragma: no cover
            log.warning("english_sub_sweep: could not enqueue: %s", e)
    log.info("english_sub_sweep: queued %d episode(s)", len(episode_ids))
    return {"retried": len(episode_ids), "episode_ids": episode_ids}


def _job_english_sub_sweep(payload: dict) -> None:
    english_sub_sweep(payload or {})


def register_english_jobs() -> None:
    try:
        from ..jobs.service import register
        register("english_subtitle_fetch", _job_english_subtitle_fetch)
        register("english_sub_sweep", _job_english_sub_sweep)
    except Exception as e:  # pragma: no cover
        log.warning("could not register english subs handlers: %s", e)


register_english_jobs()
