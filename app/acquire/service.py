"""Acquisition: nyaa.si search + RSS follows + qBittorrent + download tracking.

Flow (see notes/ARCHITECTURE.md §5.1):
    nyaa search / RSS follow  -> pick release -> push magnet to qBittorrent
    track download state      -> on complete -> enqueue(postprocess)

qBittorrent may not be running during tests; every qbt call degrades gracefully
(returns a clear status, never raises out of this module).
"""
from __future__ import annotations

import json
import logging
import re
import time
import urllib.parse
from typing import NamedTuple, Optional

import feedparser
import httpx

from ..config import settings
from ..db import cursor
from ..models import Download, NyaaResult, RssFollow

log = logging.getLogger("mimi_lab.acquire")

NYAA_BASE = "https://nyaa.si"
_UA = {"User-Agent": "Mozilla/5.0 (compatible; MimiLab/0.1)"}

# common public trackers so magnets built from an infohash actually find peers
_TRACKERS = [
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.stealth.si:80/announce",
    "udp://tracker.openbittorrent.com:6969/announce",
    "udp://exodus.desync.com:6969/announce",
    "udp://tracker.torrent.eu.org:451/announce",
]


# --------------------------------------------------------------------------- #
# nyaa.si search
# --------------------------------------------------------------------------- #
def _magnet_from_infohash(infohash: str, name: Optional[str] = None) -> str:
    parts = [f"magnet:?xt=urn:btih:{infohash}"]
    if name:
        parts.append("dn=" + urllib.parse.quote(name))
    for tr in _TRACKERS:
        parts.append("tr=" + urllib.parse.quote(tr, safe=""))
    return "&".join(parts)


def _nyaa_id_from_url(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    m = re.search(r"/(?:view|download)/(\d+)", url)
    return m.group(1) if m else None


def _entry_to_result(e: dict) -> NyaaResult:
    """Map a feedparser RSS entry (nyaa namespaced fields) to a NyaaResult."""
    title = e.get("title") or ""
    # link is the .torrent download URL; id/guid is the /view/<id> page
    torrent_url = e.get("link")
    view_url = e.get("id") or e.get("guid")
    nyaa_id = _nyaa_id_from_url(view_url) or _nyaa_id_from_url(torrent_url)

    infohash = e.get("nyaa_infohash")
    magnet = _magnet_from_infohash(infohash, title) if infohash else None

    def _to_int(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    trusted = str(e.get("nyaa_trusted", "No")).strip().lower() == "yes"

    return NyaaResult(
        nyaa_id=nyaa_id,
        title=title,
        magnet=magnet,
        torrent_url=torrent_url,
        size=e.get("nyaa_size"),
        seeders=_to_int(e.get("nyaa_seeders")),
        leechers=_to_int(e.get("nyaa_leechers")),
        trusted=trusted,
        timestamp=e.get("published"),
    )


def _rss_url(query: str, category: str = "1_2", trusted: bool = True) -> str:
    q = urllib.parse.quote(query)
    f = 2 if trusted else 0
    return f"{NYAA_BASE}/?page=rss&q={q}&c={category}&f={f}"


_RATE_LIMIT_BACKOFF_S = 1.5


def _http_get(url: str) -> Optional[httpx.Response]:
    """One nyaa GET; on 429 (nyaa rate-limits bursts, and a sequel's name ladder
    is several requests at once) wait briefly and retry once. None on failure."""
    for attempt in (0, 1):
        try:
            r = httpx.get(url, headers=_UA, timeout=20, follow_redirects=True)
        except Exception as e:
            log.warning("nyaa fetch failed (%s): %s", url, e)
            return None
        if r.status_code == 429 and attempt == 0:
            log.info("nyaa rate-limited (%s); retrying in %.1fs", url, _RATE_LIMIT_BACKOFF_S)
            time.sleep(_RATE_LIMIT_BACKOFF_S)
            continue
        if r.status_code >= 400:
            log.warning("nyaa fetch failed (%s): HTTP %s", url, r.status_code)
            return None
        return r
    return None


def nyaa_search(
    query: str, category: str = "1_2", trusted: bool = True
) -> list[NyaaResult]:
    """Search nyaa.si via its RSS feed. Robust: httpx fetch + feedparser parse.

    Falls back to scraping the HTML results table if RSS yields nothing.
    Returns [] (never raises) if nyaa is unreachable.
    """
    url = _rss_url(query, category, trusted)
    r = _http_get(url)
    if r is None:
        return _nyaa_search_html(query, category, trusted)

    feed = feedparser.parse(r.text)
    results = [_entry_to_result(e) for e in feed.entries]
    if not results and (feed.bozo or not feed.feed):
        # not a well-formed (empty) feed — nyaa served something else; scrape the
        # HTML table instead. A well-formed empty feed IS the answer: re-asking via
        # HTML doubled the latency of every miss (a sequel's name ladder has several).
        log.info("nyaa RSS unreadable for %r, trying HTML fallback", query)
        return _nyaa_search_html(query, category, trusted)
    return results


def _nyaa_search_html(
    query: str, category: str = "1_2", trusted: bool = True
) -> list[NyaaResult]:
    """HTML fallback parser (bs4). Best-effort; returns [] on any failure."""
    try:
        from bs4 import BeautifulSoup
    except Exception:  # pragma: no cover
        return []

    f = 2 if trusted else 0
    url = f"{NYAA_BASE}/?q={urllib.parse.quote(query)}&c={category}&f={f}"
    r = _http_get(url)
    if r is None:
        return []

    out: list[NyaaResult] = []
    try:
        soup = BeautifulSoup(r.text, "lxml")
        for tr in soup.select("table.torrent-list tbody tr"):
            tds = tr.find_all("td")
            if len(tds) < 8:
                continue
            cls = tr.get("class") or []
            trusted_row = "success" in cls
            # title cell: last <a> without a comment icon
            title_links = [
                a for a in tds[1].find_all("a") if "comments" not in (a.get("class") or [])
            ]
            if not title_links:
                continue
            a = title_links[-1]
            title = a.get("title") or a.get_text(strip=True)
            nyaa_id = _nyaa_id_from_url(a.get("href"))
            magnet = torrent_url = None
            for link in tds[2].find_all("a"):
                href = link.get("href") or ""
                if href.startswith("magnet:"):
                    magnet = href
                elif href.endswith(".torrent"):
                    torrent_url = urllib.parse.urljoin(NYAA_BASE, href)
            size = tds[3].get_text(strip=True)

            def _int(td):
                try:
                    return int(td.get_text(strip=True))
                except ValueError:
                    return None

            out.append(
                NyaaResult(
                    nyaa_id=nyaa_id,
                    title=title,
                    magnet=magnet,
                    torrent_url=torrent_url,
                    size=size,
                    seeders=_int(tds[5]),
                    leechers=_int(tds[6]),
                    trusted=trusted_row,
                    timestamp=tds[4].get("data-timestamp") or tds[4].get_text(strip=True),
                )
            )
    except Exception as e:  # pragma: no cover
        log.warning("nyaa HTML parse failed: %s", e)
    return out


# --------------------------------------------------------------------------- #
# Transmission client (headless daemon; degrades gracefully when not running).
# Function names kept (qbt_*) so the rest of this module + the /acquire/qbt route
# are unchanged; `downloads.qbt_hash` now stores the Transmission hashString.
# --------------------------------------------------------------------------- #
def _trans_client():
    """Return a transmission_rpc.Client, or None if unavailable. Never raises."""
    try:
        from transmission_rpc import Client
    except Exception as e:  # pragma: no cover
        log.warning("transmission-rpc import failed: %s", e)
        return None
    try:
        return Client(
            host=settings.transmission_host,
            port=settings.transmission_port,
            path=settings.transmission_path,
            timeout=8,
        )
    except Exception as e:
        log.info("Transmission unavailable: %s", e)
        return None


def _infohash_from_magnet(magnet: str) -> Optional[str]:
    m = re.search(r"btih:([0-9A-Za-z]+)", magnet or "")
    return m.group(1).lower() if m else None


def _t_hash(t) -> str:
    return (getattr(t, "hashString", None) or getattr(t, "hash_string", "") or "").lower()


def _t_content_path(t) -> Optional[str]:
    dd = getattr(t, "download_dir", None)
    name = getattr(t, "name", None)
    if dd and name:
        return str(dd).rstrip("/") + "/" + name
    return dd


def qbt_available() -> bool:
    """True if the Transmission daemon is reachable. Never raises.
    (Name kept for back-compat with callers + the /acquire/qbt route.)"""
    c = _trans_client()
    if c is None:
        return False
    try:
        c.get_session()
        return True
    except Exception as e:
        log.info("Transmission reachability check failed: %s", e)
        return False


def _apply_seed_policy(c, ids) -> None:
    """Apply the configured seed policy to a torrent so it stops seeding on its
    own — Transmission enforces this the instant the download completes, with no
    dependence on our poll loop. settings.max_seed_ratio == 0 means "stop
    immediately on completion"; a negative value means "seed unlimited".
    Best-effort; never raises.

    seed_ratio_mode: 1 = honor this torrent's own ratio limit (overrides the
    daemon's global default); 2 = unlimited.
    """
    try:
        ratio = settings.max_seed_ratio
        if ratio is None or ratio < 0:
            c.change_torrent(ids, seed_ratio_mode=2)
        else:
            c.change_torrent(ids, seed_ratio_limit=float(ratio), seed_ratio_mode=1)
    except Exception as e:  # pragma: no cover
        log.warning("could not apply seed policy to %s: %s", ids, e)


def add_torrent(magnet_or_url: str, save_path: Optional[str] = None) -> dict:
    """Add a magnet/torrent URL to Transmission. Returns {ok, hash?, reason?}. Never raises.

    Applies the configured seed policy (settings.max_seed_ratio) to the new
    torrent so it stops seeding once the download completes."""
    c = _trans_client()
    if c is None:
        return {"ok": False, "reason": "transmission unavailable"}
    save_path = save_path or str(settings.inbox_dir)
    try:
        t = c.add_torrent(magnet_or_url, download_dir=save_path)
        h = _t_hash(t) or (_infohash_from_magnet(magnet_or_url) or "")
        if h:
            _apply_seed_policy(c, h)
        return {"ok": True, "hash": h or None, "reason": None}
    except Exception as e:
        log.warning("transmission add_torrent failed: %s", e)
        return {"ok": False, "reason": str(e)}


def _find_torrent(c, qbt_hash: str):
    h = (qbt_hash or "").lower()
    try:
        for t in c.get_torrents():
            if _t_hash(t) == h:
                return t
    except Exception:
        return None
    return None


def remove_torrent(qbt_hash: str, delete_data: bool = True) -> dict:
    """Remove a torrent from Transmission (optionally deleting partial data).
    Returns {ok, reason?}. Never raises; a missing daemon/torrent is treated as
    'already gone' so cancelling a download always succeeds for the user."""
    if not qbt_hash:
        return {"ok": True, "reason": "no torrent hash"}
    c = _trans_client()
    if c is None:
        return {"ok": False, "reason": "transmission unavailable"}
    try:
        c.remove_torrent(qbt_hash, delete_data=delete_data)
        return {"ok": True}
    except Exception as e:
        log.warning("transmission remove_torrent failed for %s: %s", qbt_hash, e)
        return {"ok": False, "reason": str(e)}


def torrent_state(qbt_hash: str) -> dict:
    """Return {found, state, progress, save_path, name, content_path} for a hash.
    progress is 0.0-1.0. Never raises; returns {found:False,...} when unavailable."""
    empty = {"found": False, "state": "unknown", "progress": 0.0,
             "save_path": None, "name": None, "content_path": None}
    if not qbt_hash:
        return empty
    c = _trans_client()
    if c is None:
        return {**empty, "reason": "transmission unavailable"}
    t = _find_torrent(c, qbt_hash)
    if t is None:
        return empty
    try:
        return {
            "found": True,
            "state": str(getattr(t, "status", "")),
            "progress": float(getattr(t, "percent_done", 0.0) or 0.0),
            "save_path": getattr(t, "download_dir", None),
            "name": getattr(t, "name", None),
            "content_path": _t_content_path(t),
        }
    except Exception as e:
        log.warning("transmission torrent_state failed for %s: %s", qbt_hash, e)
        return {**empty, "reason": str(e)}


def list_torrents() -> list[dict]:
    """List all torrents in Transmission. Returns [] when unavailable. Never raises."""
    c = _trans_client()
    if c is None:
        return []
    try:
        out = []
        for t in c.get_torrents():
            out.append({
                "hash": _t_hash(t),
                "name": getattr(t, "name", None),
                "state": str(getattr(t, "status", "")),
                "progress": float(getattr(t, "percent_done", 0.0) or 0.0),
                "save_path": getattr(t, "download_dir", None),
                "content_path": _t_content_path(t),
                "size": getattr(t, "total_size", None),
            })
        return out
    except Exception as e:
        log.warning("transmission list_torrents failed: %s", e)
        return []


# Transmission statuses that mean "finished downloading" (progress>=0.999 in
# poll_downloads is the primary signal; a seeding torrent is also done).
_COMPLETE_STATES = {"seeding", "seed pending"}


# --------------------------------------------------------------------------- #
# downloads table
# --------------------------------------------------------------------------- #
def _row_get(row, key, default=None):
    """sqlite3.Row has no .get(); read a column if present, else default."""
    return row[key] if key in row.keys() else default


def _download_row_to_model(row) -> Download:
    return Download(
        id=row["id"],
        title_guess=row["title_guess"],
        state=row["state"],
        progress=row["progress"] or 0.0,
        anilist_id=row["anilist_id"],
        ep_number=row["ep_number"],
        release_group=row["release_group"],
        resolution=row["resolution"],
        size_bytes=row["size_bytes"],
        kind=_row_get(row, "kind") or "single",
        total_files=_row_get(row, "total_files"),
        done_files=_row_get(row, "done_files"),
    )


def _parse_size_to_bytes(size: Optional[str]) -> Optional[int]:
    if not size:
        return None
    m = re.match(r"\s*([\d.]+)\s*([KMGT]?i?B)\s*$", size, re.I)
    if not m:
        return None
    val = float(m.group(1))
    unit = m.group(2).upper().replace("I", "")
    mult = {"B": 1, "KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3, "TB": 1024 ** 4}
    return int(val * mult.get(unit, 1))


def add_download(result: NyaaResult) -> Download:
    """Insert a downloads row (state 'queued'); push to qBittorrent if available.

    If qbt accepts the torrent, store the hash and set state 'downloading'.
    De-duplicates: an identical release already active (same nyaa_id or same
    magnet, non-terminal state) returns the existing row instead of inserting —
    double-clicking "download" used to create a permanently-stuck duplicate.
    """
    magnet_or_url = result.magnet or result.torrent_url
    size_bytes = _parse_size_to_bytes(result.size)

    with cursor() as cx:
        # 'completed' = torrent done, import (transcode) in flight — a second click
        # then must NOT add a second row either: both rows would share ONE
        # Transmission torrent, so cancelling either deletes the data under the
        # other's import, and two imports of one file race (the first prunes the
        # source under the second). Bounded (a transcode is ≤ 2 h) so a row
        # stranded in 'completed' can't block re-downloading that release forever.
        dup = cx.execute(
            "SELECT * FROM downloads WHERE "
            "(state NOT IN ('completed','postprocessed','error','cancelled','pp_failed','lost') "
            " OR (state='completed' AND updated_at >= datetime('now','-2 hours'))) "
            "AND ((nyaa_id IS NOT NULL AND nyaa_id=?) OR (magnet IS NOT NULL AND magnet=?)) "
            "ORDER BY id DESC LIMIT 1",
            (result.nyaa_id, result.magnet),
        ).fetchone()
        if dup:
            log.info("add_download: duplicate of active download %s — reusing", dup["id"])
            return _download_row_to_model(dup)

    with cursor() as cx:
        cur = cx.execute(
            "INSERT INTO downloads(nyaa_id,title_guess,magnet,torrent_url,state,"
            "save_path,size_bytes,progress) VALUES(?,?,?,?,?,?,?,0)",
            (
                result.nyaa_id,
                result.title,
                result.magnet,
                result.torrent_url,
                "queued",
                str(settings.inbox_dir),
                size_bytes,
            ),
        )
        download_id = cur.lastrowid

    qbt_hash = None
    state = "queued"
    if magnet_or_url and qbt_available():
        add_res = add_torrent(magnet_or_url, str(settings.inbox_dir))
        if add_res.get("ok"):
            qbt_hash = add_res.get("hash")
            state = "downloading"
            with cursor() as cx:
                cx.execute(
                    "UPDATE downloads SET qbt_hash=?, state=?, updated_at=datetime('now') WHERE id=?",
                    (qbt_hash, state, download_id),
                )
        else:
            log.warning("qbt add failed for download %s: %s", download_id, add_res.get("reason"))

    with cursor() as cx:
        row = cx.execute("SELECT * FROM downloads WHERE id=?", (download_id,)).fetchone()
    return _download_row_to_model(row)


def list_downloads() -> list[Download]:
    with cursor() as cx:
        rows = cx.execute("SELECT * FROM downloads ORDER BY id DESC").fetchall()
    return [_download_row_to_model(r) for r in rows]


# states that are finished/terminal — cancelling these only updates our row.
# ('pp_failed' is NOT terminal here: its torrent may still be registered, so a
#  cancel should still try to remove it. 'lost' = torrent already gone.)
_TERMINAL_STATES = {"completed", "postprocessed", "cancelled", "error", "lost"}

# rows in these states still NEED their torrent's data on disk: the download is
# running, or finished and waiting for / failed in import (retryable).
_NEEDS_DATA_STATES = ("queued", "downloading", "completed", "pp_failed")


def other_rows_needing_torrent(qbt_hash: Optional[str], exclude_id: Optional[int]) -> list[int]:
    """Other download rows that still rely on this torrent (see _NEEDS_DATA_STATES).

    The same release added twice yields two rows sharing ONE Transmission torrent
    (Transmission de-duplicates by infohash); the torrent and its files must then
    outlive both — removing it for one row deletes the data under the other's
    import. Never raises."""
    if not qbt_hash:
        return []
    try:
        with cursor() as cx:
            rows = cx.execute(
                "SELECT id FROM downloads WHERE qbt_hash=? AND id IS NOT ? "
                f"AND state IN ({','.join('?' * len(_NEEDS_DATA_STATES))}) ORDER BY id",
                (qbt_hash, exclude_id, *_NEEDS_DATA_STATES),
            ).fetchall()
        return [r["id"] for r in rows]
    except Exception as e:  # pragma: no cover
        log.warning("other_rows_needing_torrent(%s) failed: %s", qbt_hash, e)
        return []


def cancel_download(download_id: int) -> dict:
    """Cancel a download: remove its torrent from Transmission (discarding any
    partial data) and mark the row 'cancelled'. Idempotent and never raises.

    Returns {ok, download_id, state, torrent_removed, reason?}. A 404-style
    {ok: False, reason: 'not found'} is returned when the id is unknown.
    """
    with cursor() as cx:
        row = cx.execute(
            "SELECT id, qbt_hash, state FROM downloads WHERE id=?", (download_id,)
        ).fetchone()
    if not row:
        return {"ok": False, "reason": "download not found"}

    torrent_removed = False
    reason = None
    if row["qbt_hash"] and row["state"] not in ("completed", "postprocessed"):
        others = other_rows_needing_torrent(row["qbt_hash"], download_id)
        if others:
            # the same torrent backs another row's download/import — leave it be
            reason = f"torrent shared with download {others[0]} — left in place"
            log.info("cancel_download %s: %s", download_id, reason)
        else:
            # only pull live/partial torrents; leave a finished torrent's data in place
            res = remove_torrent(row["qbt_hash"], delete_data=True)
            torrent_removed = bool(res.get("ok"))
            if not res.get("ok"):
                reason = res.get("reason")

    with cursor() as cx:
        cx.execute(
            "UPDATE downloads SET state='cancelled', updated_at=datetime('now') WHERE id=?",
            (download_id,),
        )

    try:
        from ..events import service as events
        events.record(
            "download", "Download cancelled", "info",
            meta={"download_id": download_id},
        )
    except Exception as e:  # pragma: no cover
        log.debug("event record (cancel) failed: %s", e)

    return {
        "ok": True,
        "download_id": download_id,
        "state": "cancelled",
        "torrent_removed": torrent_removed,
        "reason": reason,
    }


def retry_download(download_id: int) -> dict:
    """Retry a failed download row (the UI's answer to 'lost'/'pp_failed'/'error').

    - 'pp_failed'                 -> re-enqueue the postprocess job (data is on disk)
    - 'lost' / 'error' / 'queued' -> re-add the torrent to Transmission
    Never raises. Returns {ok, action?, reason?}.
    """
    with cursor() as cx:
        row = cx.execute("SELECT * FROM downloads WHERE id=?", (download_id,)).fetchone()
    if not row:
        return {"ok": False, "reason": "not found"}
    state = row["state"]
    if state == "pp_failed":
        try:
            from ..jobs.service import enqueue, retry_errors
            # heal any terminal-error postprocess jobs for this download first
            retry_errors(job_type="postprocess")
            enqueue("postprocess", {"download_id": download_id})
        except Exception as e:
            return {"ok": False, "reason": f"could not enqueue postprocess: {e}"}
        with cursor() as cx:
            cx.execute("UPDATE downloads SET state='completed', updated_at=datetime('now') WHERE id=?",
                       (download_id,))
        return {"ok": True, "action": "postprocess re-enqueued"}
    if state in ("lost", "error", "queued"):
        link = row["magnet"] or row["torrent_url"]
        if not link:
            return {"ok": False, "reason": "no magnet/torrent_url on the row"}
        if not qbt_available():
            return {"ok": False, "reason": "Transmission unreachable"}
        add_res = add_torrent(link, str(settings.inbox_dir))
        if not add_res.get("ok"):
            return {"ok": False, "reason": add_res.get("reason", "add failed")}
        with cursor() as cx:
            cx.execute(
                "UPDATE downloads SET qbt_hash=?, state='downloading', progress=0, "
                "updated_at=datetime('now') WHERE id=?",
                (add_res.get("hash"), download_id),
            )
        return {"ok": True, "action": "torrent re-added"}
    return {"ok": False, "reason": f"state '{state}' is not retryable"}


def release_torrent(download_id: Optional[int]) -> dict:
    """Drop a finished download's torrent from Transmission once it has been
    organized into the Library — keeping the data on disk (delete_data=False).

    This stops any remaining seeding and unregisters the torrent so a later prune
    of the inbox source file can't strand a still-registered torrent (the seed
    ratio policy already halts seeding on completion; this is the cleanup half).
    Idempotent and best-effort; never raises. A missing daemon/torrent is fine.
    """
    if not download_id:
        return {"ok": True, "reason": "no download_id"}
    try:
        with cursor() as cx:
            row = cx.execute(
                "SELECT qbt_hash FROM downloads WHERE id=?", (download_id,)
            ).fetchone()
    except Exception as e:  # pragma: no cover
        log.warning("release_torrent: could not read download %s: %s", download_id, e)
        return {"ok": False, "reason": str(e)}
    if not row or not row["qbt_hash"]:
        return {"ok": True, "reason": "no torrent hash"}
    others = other_rows_needing_torrent(row["qbt_hash"], download_id)
    if others:
        return {"ok": True, "reason": f"torrent shared with download {others[0]} — kept"}
    return remove_torrent(row["qbt_hash"], delete_data=False)


# --------------------------------------------------------------------------- #
# per-episode download (find on nyaa -> Transmission)
# --------------------------------------------------------------------------- #
_MAX_RELEASE_OPTIONS = 12


def _parsed_ep(parsed: dict) -> Optional[int]:
    epn = parsed.get("episode_number")
    if isinstance(epn, list):
        epn = epn[0] if epn else None
    try:
        return int(epn) if epn is not None else None
    except (TypeError, ValueError):
        return None


def _parsed_season(parsed: dict) -> Optional[int]:
    """anitopy `anime_season` as an int, or None when unmarked (treated as S1)."""
    s = parsed.get("anime_season")
    if isinstance(s, list):
        s = s[0] if s else None
    try:
        return int(s) if s is not None else None
    except (TypeError, ValueError):
        return None


def _title_season_ordinal(name: Optional[str]) -> int:
    """Season number a title refers to (e.g. '… 2nd Season' → 2). 1 when unmarked."""
    if not name:
        return 1
    try:
        import anitopy
        return _parsed_season(anitopy.parse(name) or {}) or 1
    except Exception:
        return 1


def title_season_ordinal(*names: Optional[str]) -> int:
    """Season a title refers to, considering EVERY name it goes by.

    Never read the ordinal off a single name. AniList's romaji and english
    routinely disagree about whether the season is stated at all, in both
    directions: a sequel's romaji is often the Japanese arc name with no
    ordinal ("EXAMPLE: Kami no Shiken-hen") while the
    english spells it out ("… Season 2") — and just as often the reverse
    ("… 2nd Season" vs a bare "… 2", which does not parse as a season at all).
    Reading `romaji or english` therefore silently returns 1 for a sequel, and
    every file correctly tagged S02 gets rejected as "different season".

    Taking the highest ordinal any name states is what makes both readable.
    """
    return max((_title_season_ordinal(n) for n in names), default=1)


# --------------------------------------------------------------------------- #
# show names → nyaa queries
#
# nyaa matches EVERY word of a query, so a query is only as good as its least-
# shared word. AniList names carry things release groups drop or spell
# differently: an arc subtitle ('… 4th Season 2-bu Ichi Shou' — Erai-raws
# writes '1 Shou', SubsPlease writes nothing at all) and the season marker itself
# ('4th Season' vs 'S4' vs 'Season 4'). Querying the exact AniList name therefore
# finds nothing for such a title — on every episode.
# --------------------------------------------------------------------------- #

# a season marker inside a show's NAME: "4th Season", "Season 4", "S4" / "S04"
_SEASON_MARK_RE = re.compile(
    r"\s*(?:[:\-–]\s*)?\b(?:(\d{1,2})(?:st|nd|rd|th)\s+Season|Season\s+(\d{1,2})|S(\d{1,2}))\b",
    re.I,
)
# a tail that DISAMBIGUATES ("Part 2", "Cour 2") — kept in queries, never stripped
_PART_TAIL_RE = re.compile(r"^(?:Part|Cour)\s*\d", re.I)
# characters nyaa's query parser reads as SYNTAX (field ':', regex '/', NOT '!' and a
# leading '-', ranges '[]{}', grouping / OR / AND '()|+', boost / fuzzy '^~',
# wildcards '?*'). A name containing one silently matches nothing — 'Re:Example kara
# Hajimeru Isekai Seikatsu 3rd Season 01' → 0 results, 'Re Example …' → hits. nyaa's
# own tokenizer drops them from release titles anyway, so spaces lose nothing.
_QUERY_SYNTAX_RE = re.compile(r"[:/!?\[\]{}^~\\*+|()]|(?<!\S)-|-(?!\S)")
_SEARCH_WORKERS = 4  # nyaa 429s on bigger bursts


def _query_text(name: str) -> str:
    """A show name as plain nyaa query words: syntax characters become spaces."""
    return " ".join(_QUERY_SYNTAX_RE.sub(" ", name or "").split())


def _marked_season(title: Optional[str]) -> Optional[int]:
    """Season a RELEASE title states in plain text ('4th Season', 'Season 04', 'S4'),
    for when anitopy drops it — its season detection derails on a subtitle with
    digits ('… 4th Season: 2-bu 1 Shou - 01' parses as no season at all,
    which both hid that release from the S4 page and offered it on the S1 page).
    None when the title carries no marker. Only consulted after anitopy."""
    m = _SEASON_MARK_RE.search(title or "")
    if not m:
        return None
    return int(next(g for g in m.groups() if g))


def _split_season_marker(name: Optional[str]) -> Optional[tuple[str, int, str]]:
    """(base, season, tail) around the season marker in a name, or None.

    'Example Gakuen Monogatari e 4th Season 2-bu Ichi Shou'
        → ('Example Gakuen Monogatari e', 4, '2-bu Ichi Shou')
    """
    m = _SEASON_MARK_RE.search(name or "")
    if not m:
        return None
    base = (name or "")[: m.start()].strip(" :-–")
    if not base:
        return None
    season = int(next(g for g in m.groups() if g))
    tail = (name or "")[m.end():].strip(" :-–")
    return base, season, tail


def _ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


class _Names(NamedTuple):
    exact: list[str]    # the AniList names verbatim (romaji, english) — what always worked
    derived: list[str]  # sequels: base + each season spelling groups use
    bare: list[str]     # base alone — last resort; relies on the season gate


def _search_names(title: str, alt: Optional[str] = None) -> _Names:
    """Show names to query nyaa with, most specific first (see the section note).

    A plain (season-1, no-marker) title yields ONLY its exact names — unchanged
    behaviour. A sequel additionally yields `Base S4`, `Base "4th Season"` and
    `Base "Season 4"` (quoted = nyaa phrase), minus any spelling that equals an
    exact name. A disambiguating 'Part N' tail is kept in every derived query —
    a Part-2 season's `Base S2` would otherwise match Part 1's files.
    """
    exact: list[str] = []
    derived: list[str] = []
    bare: list[str] = []
    keys: set = set()

    def _add(bucket: list[str], q: str) -> None:
        q = _query_text(q)  # keeps the '"…"' phrase quotes the marks below add
        key = q.replace('"', "").lower()
        if q and key not in keys:
            keys.add(key)
            bucket.append(q)

    for name in (title, alt):
        if name:
            _add(exact, name)
    for name in (title, alt):
        split = _split_season_marker(name)
        if not split:
            continue
        base, season, tail = split
        keep = f" {tail}" if tail and _PART_TAIL_RE.match(tail) else ""
        for mark in (f"S{season}", f'"{_ordinal(season)} Season"', f'"Season {season}"'):
            _add(derived, f"{base} {mark}{keep}")
        _add(bare, f"{base}{keep}")
    return _Names(exact, derived, bare)


def _release_noise_patterns(*names: Optional[str]) -> list[re.Pattern]:
    """Regexes that drop a show's DECORATIVE season tail (the arc subtitle after the
    season marker) from a release title before anitopy parses it.

    anitopy reads '… 4th Season: 2-bu 1 Shou - 01' as a title with no
    season at all (the subtitle's digits derail it), so the release fails the
    season gate. With the tail removed it parses as S4 E01. Matching tolerates
    the separators groups vary ('2-bu 1 Shou' / '2 Bu 1 Shou').
    'Part N' tails are not noise and are left alone.
    """
    pats: list[re.Pattern] = []
    seen: set = set()
    for name in names:
        split = _split_season_marker(name)
        if not split:
            continue
        tail = split[2]
        if not tail or _PART_TAIL_RE.match(tail):
            continue
        words = [w for w in re.split(r"[\s:\-–_]+", tail) if w]
        key = " ".join(words).lower()
        if len(key) < 3 or key in seen:
            continue
        seen.add(key)
        body = r"[\s\-–_]+".join(re.escape(w) for w in words)
        pats.append(re.compile(r"\s*[:\-–]?\s*\b" + body + r"\b", re.I))
    return pats


def _clean_release_title(title: str, noise: list[re.Pattern]) -> str:
    """The release title with the show's decorative tail removed (see above)."""
    t = title or ""
    if not noise:
        return t
    for pat in noise:
        t = pat.sub(" ", t)
    return " ".join(t.split())


def _nyaa_search_many(
    queries: list[str], category: str = "1_2", trusted: bool = False
) -> list[tuple[str, list[NyaaResult]]]:
    """Run several nyaa searches concurrently on a small pool, results in query
    order (so de-duplication stays deterministic). A sequel needs a handful of
    name spellings; the picker must not pay N× latency for them. Never raises."""
    queries = list(dict.fromkeys(q for q in queries if q))
    if not queries:
        return []

    def _one(q: str) -> list[NyaaResult]:
        try:
            return nyaa_search(q, category=category, trusted=trusted)
        except Exception as e:  # nyaa_search already never raises; belt and braces
            log.warning("nyaa search failed for %r: %s", q, e)
            return []

    if len(queries) == 1:
        return [(queries[0], _one(queries[0]))]
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=min(_SEARCH_WORKERS, len(queries))) as ex:
        results = list(ex.map(_one, queries))
    return list(zip(queries, results))


def _first_str(v) -> Optional[str]:
    """anitopy fields (video_resolution, release_group, …) may be a str OR a list.
    Coerce to a single string so downstream `.lower()` / Pydantic str fields are safe."""
    if isinstance(v, list):
        v = v[0] if v else None
    return v if isinstance(v, str) else (str(v) if v is not None else None)


def _episode_release_candidates(
    anilist_id: Optional[int], title: str, ep: int, alt: Optional[str] = None
) -> list[tuple[NyaaResult, dict]]:
    """Search nyaa for releases of one episode of THIS title; return (result,
    parsed)[] ranked best-first (trusted > 1080p > seeders).

    A search for a base title (e.g. 'Example Series 06') returns episode-6
    releases from *every* season, so we constrain by season, not just number:
      • relative form — parsed episode == ep AND the release's season matches the
        title's season (an unmarked release counts as season 1);
      • absolute form — an unmarked release whose number is the cross-season
        absolute (ep + Fribb offset), so a sequel's absolute-numbered releases
        still match.
    This stops a Season-4 'ep 6' release from being offered on the Season-1 page.

    Queries go out as a ladder (`_search_names`): the exact AniList names plus,
    for a sequel, the base title with each season spelling groups use; the bare
    base only when those found nothing. Release titles are stripped of the
    show's decorative arc subtitle before parsing (`_release_noise_patterns`) so
    the season gate reads the season groups actually tagged.
    """
    try:
        import anitopy  # noqa: F401
    except Exception:  # pragma: no cover
        pass

    target_season = title_season_ordinal(title, alt)
    abs_eps: set = set()
    if anilist_id is not None:
        try:
            from ..match import service as match_service
            abs_eps = {ep + off for off in match_service.episode_offsets_for(anilist_id)}
        except Exception:
            abs_eps = set()

    names = _search_names(title, alt)
    noise = _release_noise_patterns(title, alt)

    cands: list[tuple[NyaaResult, dict]] = []
    seen: set = set()

    def _collect(queries: list[str]) -> None:
        for _q, results in _nyaa_search_many(queries, category="1_2", trusted=False):
            for r in results:
                if not (r.magnet or r.torrent_url):
                    continue
                key = r.nyaa_id or r.title
                if key in seen:
                    continue
                seen.add(key)
                try:
                    import anitopy
                    parsed = anitopy.parse(_clean_release_title(r.title, noise)) or {}
                except Exception:
                    parsed = {}
                epn = _parsed_ep(parsed)
                rseason = _parsed_season(parsed)
                if rseason is None:
                    rseason = _marked_season(r.title)
                # relative: same episode number AND same season (unmarked == S1)
                rel_ok = epn == ep and (rseason or 1) == target_season
                # absolute cross-season numbering: unmarked season + offset-shifted number
                abs_ok = rseason is None and epn in abs_eps
                if not (rel_ok or abs_ok):
                    continue
                cands.append((r, parsed))

    primary = [f"{n} {ep:02d}" for n in names.exact + names.derived]
    # absolute-numbered releases ('Example Series - 15' for S2 ep 3) carry no season, so
    # only the bare base + THAT number finds them — the gate alone can't surface
    # what was never asked for.
    primary += [f"{n} {a:02d}" for a in sorted(abs_eps) for n in (names.bare or names.exact)]
    _collect(primary)
    if not cands and names.bare:
        _collect([f"{n} {ep:02d}" for n in names.bare])

    def _score(item):
        r, p = item
        res = (_first_str(p.get("video_resolution")) or "").lower()
        return (
            1 if r.trusted else 0,
            1 if "1080" in res else 0,
            r.seeders or 0,
        )

    cands.sort(key=_score, reverse=True)
    return cands[:_MAX_RELEASE_OPTIONS]


def _find_episode_release(
    title: str, ep: int, alt: Optional[str] = None, anilist_id: Optional[int] = None
) -> Optional[NyaaResult]:
    """Best single release for an episode (heuristic top pick). None if not found."""
    cands = _episode_release_candidates(anilist_id, title, ep, alt=alt)
    return cands[0][0] if cands else None


def _haiku_pick_release(title: str, ep: int, options: list) -> Optional[dict]:
    """Ask Claude Haiku which ReleaseOption best matches the wanted episode.
    Returns {"index": int, "reason": str} or None (no key / failure / refusal).

    Cached in kv keyed by a hash of the candidate titles — reopening the same
    picker used to re-pay (and re-wait up to 20s for) an identical call.
    """
    import hashlib
    from ..db import kv_get, kv_set
    from ..llm import haiku_json

    if not options:
        return None

    cache_key = None
    try:
        fingerprint = "\n".join(o.title or "" for o in options)
        digest = hashlib.sha256(f"{title}|{ep}|{fingerprint}".encode()).hexdigest()[:16]
        cache_key = f"haiku.pick.{digest}"
        cached = kv_get(cache_key)
        if cached:
            res = json.loads(cached)
            idx = res.get("index")
            if isinstance(idx, int) and 0 <= idx < len(options):
                return res
    except Exception:
        cache_key = None

    lines = []
    for i, o in enumerate(options):
        lines.append(
            f"{i}: {o.title!r} | group={o.release_group or '?'} | "
            f"res={o.resolution or '?'} | parsed_ep={o.parsed_episode} | "
            f"seeders={o.seeders or 0} | trusted={o.trusted}"
        )
    user = (
        f"Show: {title}\nWanted episode: {ep}\n\n"
        "The candidates below are ALREADY filtered to this exact show and season — "
        "do NOT prefer a different or newer season, and do not reason about which "
        "season is 'current'. Pick the single best release for this episode on "
        "quality and health only:\n"
        + "\n".join(lines)
        + "\n\nPrefer a trusted English-subbed release, 1080p, healthy seeders, "
        "and a single-episode release over a batch. "
        "Return the index."
    )
    schema = {
        "type": "object",
        "properties": {
            "index": {"type": "integer"},
            "reason": {"type": "string"},
        },
        "required": ["index", "reason"],
        "additionalProperties": False,
    }
    res = haiku_json(
        "You match anime torrent releases to a specific episode. Be precise about "
        "episode numbering and release quality. Reply only with the requested JSON.",
        user,
        schema=schema,
        max_tokens=300,
    )
    if not isinstance(res, dict):
        return None
    idx = res.get("index")
    if not isinstance(idx, int) or not (0 <= idx < len(options)):
        return None
    out = {"index": idx, "reason": (res.get("reason") or "").strip() or None}
    if cache_key:
        try:
            kv_set(cache_key, json.dumps(out, ensure_ascii=False))
        except Exception:
            pass
    return out


def _episode_title_row(episode_id: int):
    with cursor() as cx:
        return cx.execute(
            "SELECT e.ep_number, e.anilist_id, t.romaji, t.english "
            "FROM episodes e JOIN titles t ON t.anilist_id=e.anilist_id WHERE e.id=?",
            (episode_id,),
        ).fetchone()


def list_episode_releases(episode_id: int, use_llm: bool = True):
    """Build the release-picker payload for an episode: ranked nyaa releases with
    a recommended top pick (heuristic, optionally refined by Claude Haiku)."""
    from ..models import EpisodeReleases, ReleaseOption

    row = _episode_title_row(episode_id)
    if not row:
        return EpisodeReleases(episode_id=episode_id, releases=[], reason="episode not found")
    ep = int(row["ep_number"])
    title = row["romaji"] or row["english"]
    if not title:
        return EpisodeReleases(
            episode_id=episode_id, ep_number=ep, releases=[],
            reason="title has no name to search",
        )

    cands = _episode_release_candidates(row["anilist_id"], title, ep, alt=row["english"])
    options: list[ReleaseOption] = []
    for r, p in cands:
        epn = p.get("episode_number")
        if isinstance(epn, list):
            epn = epn[0] if epn else None
        try:
            epn = int(epn) if epn is not None else None
        except (TypeError, ValueError):
            epn = None
        options.append(
            ReleaseOption(
                **r.model_dump(),
                release_group=_first_str(p.get("release_group")),
                resolution=_first_str(p.get("video_resolution")),
                parsed_episode=epn,
            )
        )

    if not options:
        return EpisodeReleases(
            episode_id=episode_id, ep_number=ep, title=title, releases=[],
            reason=f"no nyaa release found for {title} ep {ep:02d}",
        )

    matched_by = "heuristic"
    reason = None
    rec_index = 0  # heuristic best is already first
    if use_llm:
        pick = _haiku_pick_release(title, ep, options)
        if pick:
            rec_index = pick["index"]
            matched_by = "haiku"
            reason = pick.get("reason")

    options[rec_index].recommended = True
    if reason:
        options[rec_index].reason = reason
    # keep the recommended option first so the split-button default is obvious
    if rec_index != 0:
        options.insert(0, options.pop(rec_index))

    return EpisodeReleases(
        episode_id=episode_id, ep_number=ep, title=title,
        releases=options, matched_by=matched_by, reason=reason,
    )


def download_episode(episode_id: int, chosen: Optional[NyaaResult] = None) -> dict:
    """Download a release for an episode via Transmission, linking it to the
    episode. `chosen` downloads that specific release; otherwise the heuristic
    best is auto-picked. Returns a status dict (never raises)."""
    row = _episode_title_row(episode_id)
    if not row:
        return {"ok": False, "reason": "episode not found"}
    ep = int(row["ep_number"])
    title = row["romaji"] or row["english"]

    result = chosen
    if result is None:
        if not title:
            return {"ok": False, "reason": "title has no name to search"}
        result = _find_episode_release(title, ep, alt=row["english"], anilist_id=row["anilist_id"])
    if not result:
        return {"ok": False, "reason": f"no nyaa release found for {title} ep {ep:02d}"}

    dl = add_download(result)
    with cursor() as cx:
        cx.execute(
            "UPDATE downloads SET anilist_id=?, ep_number=?, linked_episode_id=? WHERE id=?",
            (row["anilist_id"], ep, episode_id, dl.id),
        )
    return {
        "ok": True,
        "release": result.title,
        "seeders": result.seeders,
        "download_id": dl.id,
        "state": dl.state,
    }


# --------------------------------------------------------------------------- #
# season / batch download (find a season pack on nyaa -> Transmission -> import all)
# --------------------------------------------------------------------------- #
_MAX_BATCH_OPTIONS = 12

# an episode range in a release title: "(01-25)", "01~24", "1 - 12", "01–13".
# The first number must NOT follow a letter, so a compact season tag like
# "S2 - 10" (compact single-episode naming) isn't mistaken for a 2–10 range.
_RANGE_RE = re.compile(r"(?<![A-Za-z\d])(\d{1,3})\s*[-~–]\s*(\d{1,3})(?!\d)")
# explicit batch / disc-box markers
_BATCH_WORD_RE = re.compile(r"\b(?:batch|bd[\- ]?box|blu-?ray\s*box)\b", re.I)
# an explicit single-episode marker — "S04E11" or the compact "S2 - 10".
# anitopy sometimes fails to split these, so without this a bare 'S2'/'S04' would
# read as a season disc.
_SXXEYY_RE = re.compile(r"\bs\d{1,2}\s*(?:e\d{1,3}|-\s*\d{1,3})\b", re.I)
# genuinely MULTI-season / whole-series packs (these DO contain the target season).
# Both sides of a season range need an 's' so "S2 - 10" (a single) is NOT matched;
# deliberately does NOT match a bare single "Season 3" either.
_MULTISEASON_RE = re.compile(
    r"\b(?:complete(?:\s+series|\s+collection)?"
    r"|s\d{1,2}\s*[-+~&]\s*s\d{1,2}"                           # S1-S3 / S1+S2 (both have 's')
    r"|seasons?\s*\d{1,2}\s*[-+~&]\s*(?:season\s*)?\d{1,2}"    # Season 1-3 / Season 01 + Season 02
    r"|\d\s*[-+~&]\s*\d(?:st|nd|rd|th)?\s*seasons?)\b",        # 1+2 Season
    re.I,
)
# the numeric span of a multi-season pack ("Season 1-3", "S1+S2", "1+2 Season")
_SEASON_SPAN_RE = re.compile(
    r"\bs(\d{1,2})\s*[-+~&]\s*s(\d{1,2})\b"
    r"|\bseasons?\s*(\d{1,2})\s*[-+~&]\s*(?:season\s*)?(\d{1,2})\b"
    r"|\b(\d{1,2})\s*[-+~&]\s*(\d{1,2})(?:st|nd|rd|th)?\s*seasons?\b",
    re.I,
)


def _multiseason_span(title: str) -> Optional[tuple[int, int]]:
    """(first, last) season of a multi-season pack when its title states the
    range ('Season 1-3' → (1, 3)); None for 'Complete' packs or single seasons.
    A pack that names its seasons does NOT contain a season outside that range."""
    m = _SEASON_SPAN_RE.search(title or "")
    if not m:
        return None
    nums = [int(g) for g in m.groups() if g]
    if len(nums) != 2 or nums[0] > nums[1]:
        return None
    return nums[0], nums[1]


def _looks_like_batch(title: str, parsed: dict) -> bool:
    """True if a nyaa release is a multi-episode pack rather than a single ep.

    Order matters: an explicit batch/range/multi-season marker wins first (so a
    real batch like "01 ~ 28 [BATCH]" is caught), THEN single-episode markers
    ("S2 - 10", "SxxEyy") exclude, THEN a no-episode BD/Blu-ray disc counts as a
    pack. A bare season tag alone is NOT enough (it leaks misparsed singles).
    """
    t = title or ""
    epn = parsed.get("episode_number")
    if isinstance(epn, list) and len(epn) >= 2:  # anitopy ranges -> ["01","25"]
        return True
    if _RANGE_RE.search(t) or _MULTISEASON_RE.search(t) or _BATCH_WORD_RE.search(t):
        return True
    if _SXXEYY_RE.search(t):  # explicit single episode ("S2 - 10" / "S04E11")
        return False
    has_single_ep = epn is not None and not isinstance(epn, list)
    if has_single_ep:
        return False
    # no single episode number -> only a real BD/Blu-ray disc is a season pack
    return bool(re.search(r"\b(?:bd|blu-?ray)\b", t, re.I))


def _episode_span(
    title: str, parsed: dict, total_episodes: Optional[int]
) -> tuple[Optional[str], Optional[int]]:
    """Best-effort (span_label, episode_count) for a batch, for UI display."""
    t = title or ""
    seasons = _multiseason_span(t)
    if seasons:  # 'Season 1-3' is a season range, not episodes 1–3
        return (f"S{seasons[0]}–S{seasons[1]}", None)
    m = _RANGE_RE.search(t)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        if b >= a and (b - a) < 500:
            return (f"{a:02d}–{b:02d}", b - a + 1)
    epn = parsed.get("episode_number")
    if isinstance(epn, list) and len(epn) >= 2:
        try:
            a, b = int(epn[0]), int(epn[-1])
            if b >= a:
                return (f"{a:02d}–{b:02d}", b - a + 1)
        except (TypeError, ValueError):
            pass
    if _MULTISEASON_RE.search(t):
        return ("Complete", None)
    if total_episodes:
        return (f"~{total_episodes} eps", total_episodes)
    return (None, None)


def _batch_release_candidates(
    anilist_id: Optional[int], title: str, alt: Optional[str], total_episodes: Optional[int]
) -> list[tuple[NyaaResult, dict, tuple]]:
    """Search nyaa for season packs / batches of THIS title; return
    (result, parsed, (span, count))[] ranked best-first.

    Health-first: seeders dominate (committing to a multi-GB pack — a 0-seeder
    one is useless), then trusted, then 1080p. Unlike the per-episode search,
    queries carry NO episode number, so batch torrents actually surface; results
    are then gated to packs (`_looks_like_batch`) of this season.
    """
    target_season = title_season_ordinal(title, alt)
    names = _search_names(title, alt)
    noise = _release_noise_patterns(title, alt)

    # The exact names keep their full suffix set (as before). The derived season
    # spellings get the two that surface packs, and the bare base only ' batch' —
    # pack names vary the most ('S4 (01-16) [Batch]', '(Season 04) … (Batch)'), so
    # for packs the bare base is worth asking up front, not as a fallback; a bare
    # base ALONE would just return every season's single episodes.
    queries: list[str] = []

    def _add_queries(show_names: list[str], suffixes: tuple[str, ...]) -> None:
        for base in show_names:
            for suffix in suffixes:
                q = f"{base}{suffix}"
                if q not in queries:
                    queries.append(q)

    _add_queries(names.exact, (" batch", " BD 1080p", " 1080p", ""))
    _add_queries(names.derived, (" batch", ""))
    _add_queries(names.bare, (" batch",))

    cands: list[tuple[NyaaResult, dict, tuple]] = []
    seen: set = set()
    for _q, results in _nyaa_search_many(queries, category="1_2", trusted=False):
        for r in results:
            if not (r.magnet or r.torrent_url):
                continue
            key = r.nyaa_id or r.title
            if key in seen:
                continue
            clean_title = _clean_release_title(r.title, noise)
            try:
                import anitopy
                parsed = anitopy.parse(clean_title) or {}
            except Exception:
                parsed = {}
            if not _looks_like_batch(clean_title, parsed):
                continue
            # season gate: accept unmarked / this-season / multi-season (complete);
            # reject a *different single season's* pack (e.g. an S2-only BD on S1)
            # and a multi-season pack whose stated range excludes this season
            # (a 'Season 1-3' pack is not a Season 4 pack).
            # Fall back to the title text when anitopy doesn't tag a season.
            rseason = _parsed_season(parsed)
            if rseason is None:
                rseason = _marked_season(clean_title)  # None == unmarked, keep lenient
            multi = bool(_MULTISEASON_RE.search(clean_title))
            if rseason is not None and rseason != target_season and not multi:
                continue
            if multi:
                span = _multiseason_span(clean_title)
                if span and not (span[0] <= target_season <= span[1]):
                    continue
            seen.add(key)
            cands.append((r, parsed, _episode_span(r.title, parsed, total_episodes)))

    def _score(item):
        r, p, _span = item
        res = (_first_str(p.get("video_resolution")) or "").lower()
        return (r.seeders or 0, 1 if r.trusted else 0, 1 if "1080" in res else 0)

    cands.sort(key=_score, reverse=True)
    return cands[:_MAX_BATCH_OPTIONS]


def _haiku_pick_batch(title: str, total_episodes: Optional[int], options: list) -> Optional[dict]:
    """Ask Claude Haiku which season pack is best. Returns {index, reason} or None."""
    from ..llm import haiku_json

    if not options:
        return None
    lines = []
    for i, o in enumerate(options):
        lines.append(
            f"{i}: {o.title!r} | group={o.release_group or '?'} | res={o.resolution or '?'} | "
            f"span={o.episode_span or '?'} | size={o.size or '?'} | "
            f"seeders={o.seeders or 0} | trusted={o.trusted}"
        )
    user = (
        f"Show: {title}\nEpisodes this season: {total_episodes or 'unknown'}\n\n"
        "Pick the single best COMPLETE season pack / batch to download. The candidates "
        "are already filtered to this show + season. Prefer: healthy seeders (most "
        "important — these are multi-GB), a complete episode span (the whole season, not "
        "a partial range), 1080p, a reputable group, and BD/dual-audio when available. "
        "Avoid dead (0-seeder) packs.\n"
        + "\n".join(lines)
        + "\n\nReturn the index."
    )
    schema = {
        "type": "object",
        "properties": {"index": {"type": "integer"}, "reason": {"type": "string"}},
        "required": ["index", "reason"],
        "additionalProperties": False,
    }
    res = haiku_json(
        "You match anime season packs to a show. Be precise about completeness and "
        "torrent health. Reply only with the requested JSON.",
        user, schema=schema, max_tokens=300,
    )
    if not isinstance(res, dict):
        return None
    idx = res.get("index")
    if not isinstance(idx, int) or not (0 <= idx < len(options)):
        return None
    return {"index": idx, "reason": (res.get("reason") or "").strip() or None}


def _title_row(anilist_id: int):
    with cursor() as cx:
        return cx.execute(
            "SELECT anilist_id, romaji, english, total_episodes FROM titles WHERE anilist_id=?",
            (anilist_id,),
        ).fetchone()


def list_season_releases(anilist_id: int, use_llm: bool = True):
    """Build the batch-picker payload for a title: ranked nyaa season packs with a
    recommended top pick (heuristic, optionally refined by Claude Haiku)."""
    from ..models import ReleaseOption, SeasonReleases

    row = _title_row(anilist_id)
    if not row:
        return SeasonReleases(anilist_id=anilist_id, releases=[], reason="title not found")
    title = row["romaji"] or row["english"]
    total = row["total_episodes"]
    if not title:
        return SeasonReleases(
            anilist_id=anilist_id, total_episodes=total, releases=[],
            reason="title has no name to search",
        )

    cands = _batch_release_candidates(anilist_id, title, row["english"], total)
    options: list[ReleaseOption] = []
    for r, p, (span, count) in cands:
        options.append(
            ReleaseOption(
                **r.model_dump(),
                release_group=_first_str(p.get("release_group")),
                resolution=_first_str(p.get("video_resolution")),
                kind="batch",
                episode_span=span,
                episode_count=count,
            )
        )
    if not options:
        return SeasonReleases(
            anilist_id=anilist_id, title=title, total_episodes=total, releases=[],
            reason=f"no season pack found for {title}",
        )

    matched_by = "heuristic"
    reason = None
    rec_index = 0
    if use_llm:
        pick = _haiku_pick_batch(title, total, options)
        if pick:
            rec_index = pick["index"]
            matched_by = "haiku"
            reason = pick.get("reason")
    options[rec_index].recommended = True
    if reason:
        options[rec_index].reason = reason
    if rec_index != 0:
        options.insert(0, options.pop(rec_index))

    return SeasonReleases(
        anilist_id=anilist_id, title=title, total_episodes=total,
        releases=options, matched_by=matched_by, reason=reason,
    )


def download_batch(
    anilist_id: int, chosen: NyaaResult, episode_count: Optional[int] = None,
    selected_episodes: Optional[list] = None,
) -> dict:
    """Download a season pack for a title via Transmission. On completion the batch
    postprocess assigns every video file to this title's episodes.

    `selected_episodes` is the (1-based, season-relative) episode numbers the user
    chose. A strict subset triggers per-file selection in Transmission (the
    deselected episodes are NOT downloaded — see handle_select_files); None or the
    full set downloads the whole pack. Returns a status dict (never raises)."""
    row = _title_row(anilist_id)
    if not row:
        return {"ok": False, "reason": "title not found"}
    if not (chosen and (chosen.magnet or chosen.torrent_url)):
        return {"ok": False, "reason": "release has no magnet/torrent link"}

    season = title_season_ordinal(row["romaji"], row["english"])
    total = episode_count or row["total_episodes"]

    wanted: Optional[list] = None
    if selected_episodes:
        sel = sorted({int(e) for e in selected_episodes if e is not None})
        # only a real subset is worth selecting; all-or-unknown -> whole pack
        if total and 0 < len(sel) < int(total):
            wanted = sel

    expected = len(wanted) if wanted else total
    dl = add_download(chosen)  # inserts a downloads row + pushes to Transmission
    with cursor() as cx:
        cx.execute(
            "UPDATE downloads SET kind='batch', anilist_id=?, season=?, total_files=?, "
            "wanted_eps=?, files_selected=0 WHERE id=?",
            (anilist_id, season, expected, (json.dumps(wanted) if wanted else None), dl.id),
        )

    if wanted:
        try:
            from ..jobs.service import enqueue
            enqueue("select_files", {"download_id": dl.id, "attempt": 0})
        except Exception as e:
            log.warning("could not enqueue select_files for %s: %s", dl.id, e)

    try:
        from ..events import service as events
        label = chosen.title[:80]
        if wanted:
            label = f"{label} ({len(wanted)} of {total} eps)"
        events.record(
            "download", f"Season pack queued: {label}", "info",
            meta={"download_id": dl.id, "anilist_id": anilist_id, "kind": "batch"},
        )
    except Exception as e:  # pragma: no cover
        log.debug("event record (batch queue) failed: %s", e)

    return {
        "ok": True,
        "release": chosen.title,
        "seeders": chosen.seeders,
        "download_id": dl.id,
        "state": dl.state,
        "kind": "batch",
        "total_files": total,
        "wanted_eps": wanted,
    }


# --------------------------------------------------------------------------- #
# per-file selection for a batch (don't download episodes the user unchecked)
# --------------------------------------------------------------------------- #
_SELECT_VIDEO_EXTS = (".mkv", ".mp4", ".m4v", ".avi", ".mov", ".ts", ".webm")
_SELECT_MAX_ATTEMPTS = 40  # ~retry while waiting for magnet metadata to resolve
# non-canonical variants in a pack that must NOT be imported as the main broadcast
# (a Director's Cut / recap re-numbers the season and would overwrite real episodes)
_VARIANT_RE = re.compile(r"director'?s?\s*cut|\brecap\b|\bdigest\b", re.I)


def _basename(path: str) -> str:
    """Last path component of a torrent file path (parse the filename, not the
    whole path — a multi-season pack's folder name like 'Season 01 + Season 02'
    otherwise poisons anitopy's season detection)."""
    return (path or "").replace("\\", "/").rstrip("/").split("/")[-1]


def _variant_scope(path: str) -> str:
    """The filename + its immediate folder, EXCLUDING the pack's root folder.

    A pack root is often named for its whole contents (e.g. '… Director's Cut +
    OVAs + Movies'), which must NOT mark every file inside it as a variant — only
    the actual Director's-Cut subfolder/file should match."""
    parts = (path or "").replace("\\", "/").rstrip("/").split("/")
    return " / ".join(parts[-2:])


def get_torrent_files(qbt_hash: str) -> list:
    """Transmission's File list for a torrent (empty if unavailable or metadata
    not resolved yet). Each File has .name and .id (index). Never raises."""
    if not qbt_hash:
        return []
    c = _trans_client()
    if c is None:
        return []
    t = _find_torrent(c, qbt_hash)
    if t is None:
        return []
    try:
        return list(t.get_files() or [])
    except Exception as e:
        log.warning("get_torrent_files(%s) failed: %s", qbt_hash, e)
        return []


def _file_episode(
    name: str, anilist_id: Optional[int], total: Optional[int], target_season: int = 1
) -> Optional[int]:
    """Season-relative episode number a pack file belongs to for THIS title, or
    None when the file isn't this season's episode.

    Season-aware: a file explicitly tagged a DIFFERENT season than the target
    title (e.g. "… Season 02 - 20" in a multi-season pack viewed from the S1 page)
    returns None, so it is neither downloaded nor imported here. Unmarked files are
    treated as the target season.
    """
    base = _basename(name)
    if not base.lower().endswith(_SELECT_VIDEO_EXTS):
        return None
    # a Director's Cut / recap is not the canonical broadcast episode
    if _VARIANT_RE.search(_variant_scope(name)):
        return None
    try:
        import anitopy
        p = anitopy.parse(base) or {}
    except Exception:
        return None
    fs = p.get("anime_season")
    if isinstance(fs, list):
        fs = fs[0] if fs else None
    try:
        fs = int(fs) if fs is not None else None
    except (TypeError, ValueError):
        fs = None
    if fs is not None and fs != target_season:
        return None  # belongs to a different season — not this title's episode
    raw = p.get("episode_number")
    if isinstance(raw, list):
        raw = raw[0] if raw else None
    try:
        raw = int(raw) if raw is not None else None
    except (TypeError, ValueError):
        raw = None
    if raw is None:
        return None
    if anilist_id is not None:
        try:
            from ..match import service as match_service
            rel = match_service.normalize_episode_number(anilist_id, raw, total)
            if rel is not None:
                return rel
        except Exception:
            pass
    return raw


def handle_select_files(payload: dict) -> None:
    """Job handler: apply a batch's episode selection by deselecting the files for
    episodes the user unchecked. Waits (re-enqueues) for the torrent metadata to
    resolve, then sets files_unwanted once. Never raises."""
    from ..jobs.service import enqueue

    download_id = payload.get("download_id")
    attempt = int(payload.get("attempt", 0) or 0)
    if download_id is None:
        return

    with cursor() as cx:
        row = cx.execute(
            "SELECT qbt_hash, anilist_id, wanted_eps, files_selected FROM downloads WHERE id=?",
            (download_id,),
        ).fetchone()
    if not row or row["files_selected"] or not row["wanted_eps"]:
        return  # nothing to do / already applied
    try:
        wanted = {int(e) for e in json.loads(row["wanted_eps"])}
    except Exception:
        return

    def _retry():
        if attempt < _SELECT_MAX_ATTEMPTS:
            enqueue("select_files", {"download_id": download_id, "attempt": attempt + 1},
                    delay_seconds=5)

    qbt_hash = row["qbt_hash"]
    if not qbt_hash:
        return _retry()

    files = get_torrent_files(qbt_hash)
    if not files:  # metadata not resolved yet
        return _retry()

    anilist_id = row["anilist_id"]
    total = None
    target_season = 1
    with cursor() as cx:
        tr = cx.execute(
            "SELECT total_episodes, romaji, english FROM titles WHERE anilist_id=?", (anilist_id,)
        ).fetchone()
    if tr:
        total = tr["total_episodes"]
        target_season = title_season_ordinal(tr["romaji"], tr["english"])

    unwanted = []
    for f in files:
        fid = getattr(f, "id", None)
        if fid is None:
            continue
        ep = _file_episode(getattr(f, "name", "") or "", anilist_id, total, target_season)
        if ep is None or ep not in wanted:
            unwanted.append(fid)

    # never deselect everything (a broken mapping would download nothing) — only
    # apply a real partial selection.
    if unwanted and len(unwanted) < len(files):
        c = _trans_client()
        if c is not None:
            try:
                c.change_torrent(qbt_hash, files_unwanted=unwanted)
                log.info("batch %s: deselected %d/%d files (wanted eps=%s)",
                         download_id, len(unwanted), len(files), sorted(wanted))
            except Exception as e:
                log.warning("files_unwanted on %s failed: %s", qbt_hash, e)
                return _retry()

    with cursor() as cx:
        cx.execute("UPDATE downloads SET files_selected=1, updated_at=datetime('now') WHERE id=?",
                   (download_id,))


# --------------------------------------------------------------------------- #
# RSS follows
# --------------------------------------------------------------------------- #
def add_follow(
    query: str,
    title: Optional[str] = None,
    anilist_id: Optional[int] = None,
    category: str = "1_2",
    trusted_only: bool = True,
    resolution: str = "1080p",
) -> RssFollow:
    with cursor() as cx:
        cur = cx.execute(
            "INSERT INTO rss_follows(anilist_id,title,query,category,trusted_only,"
            "resolution,enabled,seen_json) VALUES(?,?,?,?,?,?,1,'[]')",
            (anilist_id, title, query, category, 1 if trusted_only else 0, resolution),
        )
        follow_id = cur.lastrowid
        row = cx.execute("SELECT * FROM rss_follows WHERE id=?", (follow_id,)).fetchone()
    return _follow_row_to_model(row)


def follow_title(anilist_id: int, resolution: str = "1080p") -> RssFollow:
    """One-click Follow for a library title: derive a sensible nyaa query from the
    title name and create an RSS follow (idempotent per anilist_id).

    The UI exposes this on the Title page (the anilist_id is already known); the
    freeform 'add follow by query' form stays for edge cases.
    """
    with cursor() as cx:
        t = cx.execute(
            "SELECT anilist_id, romaji, english FROM titles WHERE anilist_id=?",
            (anilist_id,),
        ).fetchone()
        if not t:
            raise ValueError(f"title {anilist_id} not found")
        existing = cx.execute(
            "SELECT * FROM rss_follows WHERE anilist_id=?", (anilist_id,)
        ).fetchone()
        if existing:
            return _follow_row_to_model(existing)
    # romaji matches fansub-group naming best; fall back to english. Query SYNTAX
    # characters (':' in 'Re:Example') would make the feed match nothing.
    name = t["romaji"] or t["english"] or ""
    query = _query_text(name.strip())
    return add_follow(
        query=query, title=name or None, anilist_id=anilist_id,
        resolution=resolution,
    )


def _follow_row_to_model(row) -> RssFollow:
    return RssFollow(
        id=row["id"],
        anilist_id=row["anilist_id"],
        title=row["title"],
        query=row["query"],
        category=row["category"],
        trusted_only=bool(row["trusted_only"]),
        resolution=row["resolution"],
        enabled=bool(row["enabled"]),
    )


def list_follows() -> list[RssFollow]:
    with cursor() as cx:
        rows = cx.execute("SELECT * FROM rss_follows ORDER BY id DESC").fetchall()
    return [_follow_row_to_model(r) for r in rows]


def remove_follow(follow_id: int) -> bool:
    with cursor() as cx:
        cur = cx.execute("DELETE FROM rss_follows WHERE id=?", (follow_id,))
        return cur.rowcount > 0


def _effective_query(row) -> str:
    """Combine the follow's query with its resolution preference for the feed."""
    q = row["query"] or ""
    res = row["resolution"]
    if res and res.lower() not in q.lower():
        q = f"{q} {res}".strip()
    return q


def check_follows() -> dict:
    """Poll every enabled RSS follow; add new (unseen) items to qbt + downloads.

    A follow's `seen_json` is a list of nyaa ids already handled. New items are
    added; seen_json + last_checked are updated. Returns a per-follow summary.
    Designed to be safe to call periodically and as the `rss_check` job handler.
    """
    summary = {"checked": 0, "new": 0, "follows": []}
    with cursor() as cx:
        follows = cx.execute("SELECT * FROM rss_follows WHERE enabled=1").fetchall()

    have_qbt = qbt_available()

    for row in follows:
        query = _effective_query(row)
        category = row["category"] or "1_2"
        trusted = bool(row["trusted_only"])
        try:
            seen = set(json.loads(row["seen_json"] or "[]"))
        except Exception:
            seen = set()

        results = nyaa_search(query, category=category, trusted=trusted)
        new_for_follow = 0
        for res in results:
            key = res.nyaa_id or (res.magnet or res.torrent_url)
            if not key or key in seen:
                continue
            seen.add(key)
            new_for_follow += 1
            dl = add_download(res)  # also pushes to qbt if available
            # tag the download with the follow's anilist hint
            if row["anilist_id"]:
                with cursor() as cx:
                    cx.execute(
                        "UPDATE downloads SET anilist_id=? WHERE id=?",
                        (row["anilist_id"], dl.id),
                    )

        with cursor() as cx:
            cx.execute(
                "UPDATE rss_follows SET seen_json=?, last_checked=datetime('now') WHERE id=?",
                (json.dumps(sorted(seen)), row["id"]),
            )

        summary["checked"] += 1
        summary["new"] += new_for_follow
        summary["follows"].append(
            {"id": row["id"], "query": query, "new": new_for_follow, "total_seen": len(seen)}
        )

    summary["qbt_available"] = have_qbt
    return summary


# --------------------------------------------------------------------------- #
# download polling -> postprocess
# --------------------------------------------------------------------------- #
def _completed_save_path(state_info: dict, fallback_save_path: Optional[str]) -> Optional[str]:
    """Prefer qbt content_path (the actual file/dir); else save_path."""
    return state_info.get("content_path") or state_info.get("save_path") or fallback_save_path


# consecutive poll cycles a torrent has been missing from Transmission.
# In-memory (resets on restart — harmless; the count just starts over).
_MISS_COUNTS: dict[int, int] = {}
_MISS_LIMIT = 10  # ~5 minutes at the 30s poll interval


def _repush_stranded(summary: dict) -> None:
    """Push rows that were queued while Transmission was unreachable.

    add_download leaves state='queued', qbt_hash=NULL when the daemon is down;
    the old poll only selected `qbt_hash IS NOT NULL`, so such rows were never
    pushed later — a permanent stuck state. Heal them here on every poll.
    """
    with cursor() as cx:
        rows = cx.execute(
            "SELECT id, magnet, torrent_url FROM downloads "
            "WHERE state='queued' AND qbt_hash IS NULL"
        ).fetchall()
    for row in rows:
        link = row["magnet"] or row["torrent_url"]
        if not link:
            continue
        add_res = add_torrent(link, str(settings.inbox_dir))
        if add_res.get("ok"):
            with cursor() as cx:
                cx.execute(
                    "UPDATE downloads SET qbt_hash=?, state='downloading', updated_at=datetime('now') WHERE id=?",
                    (add_res.get("hash"), row["id"]),
                )
            summary["repushed"] = summary.get("repushed", 0) + 1
            log.info("re-pushed stranded queued download %s to Transmission", row["id"])


def poll_downloads() -> dict:
    """Refresh progress/state of active downloads from qBittorrent.

    When a torrent completes, mark it 'completed' and enqueue a `postprocess`
    job carrying {download_id}. A torrent missing from Transmission for
    _MISS_LIMIT consecutive polls (crash / manual removal) transitions the row
    to 'lost' instead of spinning at 'downloading' forever. Never raises.
    """
    from ..jobs.service import enqueue

    summary = {"active": 0, "completed": 0, "updated": 0}
    with cursor() as cx:
        rows = cx.execute(
            "SELECT * FROM downloads WHERE state NOT IN "
            "('completed','postprocessed','error','cancelled','pp_failed','lost') "
            "AND qbt_hash IS NOT NULL"
        ).fetchall()

    if not qbt_available():
        summary["qbt_available"] = False
        return summary
    summary["qbt_available"] = True
    _repush_stranded(summary)

    for row in rows:
        summary["active"] += 1
        info = torrent_state(row["qbt_hash"])
        if not info.get("found"):
            misses = _MISS_COUNTS.get(row["id"], 0) + 1
            _MISS_COUNTS[row["id"]] = misses
            if misses >= _MISS_LIMIT:
                _MISS_COUNTS.pop(row["id"], None)
                with cursor() as cx:
                    cx.execute(
                        "UPDATE downloads SET state='lost', updated_at=datetime('now') WHERE id=?",
                        (row["id"],),
                    )
                log.warning("download %s: torrent missing from Transmission for %d polls -> lost",
                            row["id"], _MISS_LIMIT)
                try:
                    from ..events import service as events
                    events.record(
                        "download",
                        f"Download lost: {row['title_guess'] or row['id']}",
                        "warning",
                        detail="Torrent vanished from Transmission (crash or manual removal). Re-add it from Acquire.",
                        meta={"download_id": row["id"]},
                    )
                except Exception:
                    pass
            continue
        _MISS_COUNTS.pop(row["id"], None)
        progress = info.get("progress", 0.0)
        qstate = info.get("state", "")
        # Complete means EVERY wanted byte is on disk: Transmission reports
        # percentDone == 1.0 exactly then (and flips to seeding, or stops under
        # our ratio-0 policy). 99.9% is NOT complete — the endgame's last pieces
        # can take minutes, and until a file is whole it is still 'name.part',
        # which the importer rightly does not see: a pack declared done at
        # 0.999 imported nothing.
        is_done = qstate in _COMPLETE_STATES or progress >= 1.0

        new_state = "completed" if is_done else "downloading"
        save_path = _completed_save_path(info, row["save_path"]) if is_done else row["save_path"]

        with cursor() as cx:
            cx.execute(
                "UPDATE downloads SET progress=?, state=?, save_path=?, updated_at=datetime('now') WHERE id=?",
                (progress, new_state, save_path, row["id"]),
            )
        summary["updated"] += 1

        if is_done and row["state"] != "completed":
            summary["completed"] += 1
            enqueue("postprocess", {"download_id": row["id"]})
            log.info("download %s complete -> enqueued postprocess", row["id"])
            try:
                from ..events import service as events
                name = info.get("name") or row["title_guess"] or f"download {row['id']}"
                events.record(
                    "download", f"Downloaded {name}", "success",
                    meta={"download_id": row["id"]},
                )
            except Exception as e:  # pragma: no cover
                log.debug("event record (download) failed: %s", e)

    return summary


# --------------------------------------------------------------------------- #
# job handlers
# --------------------------------------------------------------------------- #
def handle_rss_check(payload: dict) -> None:
    """Job handler: poll RSS follows, then refresh download progress."""
    res = check_follows()
    log.info("rss_check: %s", res)
    poll_downloads()


def handle_postprocess(payload: dict) -> None:
    """Job handler: media post-processing (probe -> remux/transcode -> organize).

    Delegates to app.media.service.postprocess. Imported lazily so a half-built
    media module never breaks acquire at import time.
    """
    try:
        from ..media.service import postprocess as media_postprocess
    except Exception as e:
        log.warning("media.postprocess unavailable: %s", e)
        return
    media_postprocess(payload)


def _register_jobs() -> None:
    """Register acquire's job handlers + periodic RSS polling. Safe & idempotent."""
    try:
        from ..jobs.service import register, add_periodic
    except Exception as e:  # pragma: no cover
        log.warning("jobs.service unavailable, handlers not registered: %s", e)
        return
    register("rss_check", handle_rss_check)
    register("postprocess", handle_postprocess)
    register("poll_downloads", lambda p: poll_downloads())
    register("select_files", handle_select_files)
    # periodic-friendly: refresh follows + downloads every 5 min (no-op if scheduler off)
    try:
        add_periodic(handle_rss_check, seconds=300, job_id="acquire_rss_check")
    except Exception:
        pass


# register at import time (per spec)
_register_jobs()
