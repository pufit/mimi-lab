"""Live-deployment smoke test: read-only checks against the RUNNING server.

Run after a deploy/restart:  .venv/bin/python integration_test.py
(For the full relay/media/upload E2E with a fake Connector on a scratch DB,
run `.venv/bin/python -m app.connector.selftest` instead.)
"""
from __future__ import annotations

import sys

import httpx

BASE = "http://127.0.0.1:8000"
results: list[tuple[str, bool]] = []


def check(name: str, cond, extra: str = "") -> bool:
    ok = bool(cond)
    results.append((name, ok))
    print(("PASS " if ok else "FAIL "), name, "—", extra)
    return ok


def get(path: str, timeout: float = 15.0):
    return httpx.get(BASE + path, timeout=timeout)


def main() -> int:
    r = get("/api/health")
    check("health liveness", r.status_code == 200 and r.json().get("ok"), str(r.json()))

    r = get("/api/health?full=1")
    comp = r.json().get("components", {})
    check("health full: db+disk ok",
          comp.get("db", {}).get("ok") and comp.get("disk", {}).get("ok"),
          f"db={comp.get('db', {}).get('size_bytes', 0) >> 20}MB free={comp.get('disk', {}).get('free_gb')}GB")
    check("health full: tokenizer", comp.get("tokenizer", {}).get("ok") is True,
          f"ext={comp.get('tokenizer', {}).get('ext_version')}")
    check("health full: transmission", comp.get("transmission", {}).get("ok") is True)

    r = get("/api/jobs/stats")
    st = r.json()
    check("jobs stats", r.status_code == 200 and "states" in st,
          f"states={st.get('states')}")
    check("no terminal-error jobs", (st.get("states", {}).get("error", 0)) == 0,
          f"error={st.get('states', {}).get('error', 0)} (retry via /api/jobs/retry-errors)")

    r = get("/api/mal/status")
    check("mal status", r.status_code == 200 and "authed" in r.json(),
          f"authed={r.json().get('authed')} synced={r.json().get('synced_titles')}")

    r = get("/api/catalog/titles")
    titles = r.json()
    check("titles list", r.status_code == 200 and isinstance(titles, list), f"{len(titles)} titles")

    r = get("/api/catalog/continue")
    check("continue rail", r.status_code == 200 and isinstance(r.json(), list),
          f"{len(r.json())} entries")

    r = get("/api/learn/moments?q=%E6%99%82%E9%96%93&limit=5")
    moments = r.json()
    check("moments search", r.status_code == 200 and isinstance(moments, list), f"{len(moments)} results")
    if moments:
        m = moments[0]
        check("moment has show title", bool(m.get("title")) and not str(m.get("title", "")).startswith("Episode"),
              f"title={m.get('title')!r}")
        check("moment media urls null-safe",
              (m.get("video_path") is not None) or (m.get("image_url") is None),
              f"video={bool(m.get('video_path'))} image_url={m.get('image_url')}")

    r = get("/api/learn/sweet-spot?limit=10")
    ss = r.json()
    check("sweet-spot", r.status_code == 200 and isinstance(ss, list), f"{len(ss)} titles")
    if ss:
        ids = [e["anilist_id"] for e in ss]
        check("sweet-spot deduped per title", len(ids) == len(set(ids)))

    r = get("/api/learn/leverage?top=5", timeout=60.0)
    lv = r.json()
    check("leverage", r.status_code == 200 and isinstance(lv.get("words"), list),
          f"{len(lv.get('words', []))} words / {lv.get('episodes_in_band')} eps in band")

    r = get("/api/learn/stats")
    stats = r.json()
    check("stats", r.status_code == 200 and "known_series" in stats,
          f"watched={stats.get('watched_total')} known_pts={len(stats.get('known_series', []))}")

    r = get("/api/connector/status")
    check("connector status", r.status_code == 200 and "connected" in r.json(),
          f"connected={r.json().get('connected')}")

    r = get("/api/events?limit=3")
    check("events feed", r.status_code == 200 and isinstance(r.json(), list))

    failed = [n for n, ok in results if not ok]
    print(f"\n==== {len(results) - len(failed)}/{len(results)} checks passed ====")
    if failed:
        print("failed:", ", ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
