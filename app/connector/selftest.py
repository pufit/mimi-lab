"""End-to-end self-test for the Connector topology (no Chrome / Node needed).

Run:  .venv/bin/python -m app.connector.selftest

Spins up the real FastAPI app on a temp DB + a known token, connects a *fake*
Connector over the WebSocket, and exercises the whole relay:

  * play with no Connector            -> 409
  * connector/status                   -> connected + chrome/migaku from heartbeat
  * watch/play                         -> relayed command (URLs + signed token + seek), acked, 200
  * media/episode/{id}/video           -> HTTP Range 206 (+ 200 full, + 401 without token)
  * media/episode/{id}/subtitle        -> 200
  * watch/play-moment                  -> relayed with seek = line.start_ms
  * known/upload (auth)                -> writes WordList (+ 401 without token)
  * known/sync                         -> relays `sync-known`, returns summary
  * learn/comprehension/upload (auth)  -> stores exact comprehension
  * Connector disconnect               -> status back to offline
  * multi-device                       -> two identified Connectors coexist;
    play routes to an explicit device_id / defaults to the Migaku-ready one;
    unknown device -> 409; a same-device reconnect supersedes only its own
    old socket (close 4000); one device closing leaves the other connected.
    (The first leg above registers WITHOUT a device_id — the pre-multi-device
    protocol — proving old bundles keep working.)

Exit code 0 = all PASS, 1 = any FAIL.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="migaku_conn_selftest_")
    token = "selftest-secret-abc123"
    # MUST be set before app.config.settings is constructed.
    os.environ["MIMI_LAB_DB"] = os.path.join(tmp, "test.db")
    os.environ["MIMI_LAB_TOKEN"] = token
    os.environ["SERVER_PUBLIC_URL"] = ""  # derive media URLs from the request

    import pathlib

    import httpx
    import uvicorn
    import websockets

    from app.db import cursor, init_db
    from app.main import app

    root = pathlib.Path(__file__).resolve().parents[2]
    video = root / "lib" / "TestShow" / "TestShow - S01E01.mp4"
    sub = root / "lib" / "TestShow" / "TestShow - S01E01.ja.srt"

    results: list[tuple[str, bool, str]] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        results.append((name, bool(cond), detail))
        print(f"{'PASS' if cond else 'FAIL'}  {name}{('  — ' + detail) if detail else ''}")

    # ---- seed a title + episode + subtitle + one line ----
    init_db()
    aid = -424242
    with cursor() as cx:
        cx.execute("INSERT OR IGNORE INTO titles(anilist_id, romaji) VALUES(?,?)", (aid, "Connector Selftest"))
        ep_id = cx.execute(
            "INSERT INTO episodes(anilist_id, ep_number, video_path) VALUES(?,?,?)",
            (aid, 1, str(video)),
        ).lastrowid
        sub_id = cx.execute(
            "INSERT INTO subtitles(episode_id, source, path, lang) VALUES(?,?,?,?)",
            (ep_id, "jimaku", str(sub), "ja"),
        ).lastrowid
        line_id = cx.execute(
            "INSERT INTO subtitle_lines(subtitle_id, episode_id, idx, start_ms, end_ms, text) "
            "VALUES(?,?,?,?,?,?)",
            (sub_id, ep_id, 0, 6000, 8000, "テスト"),
        ).lastrowid

    # ---- boot the real app on a temp port ----
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on")
    server = uvicorn.Server(config)
    th = threading.Thread(target=server.run, daemon=True)
    th.start()

    async def run() -> None:
        async with httpx.AsyncClient(timeout=30) as c:
            # wait for startup
            for _ in range(100):
                try:
                    if (await c.get(base + "/api/health")).status_code == 200:
                        break
                except Exception:
                    pass
                await asyncio.sleep(0.1)

            # 1) play with no connector -> 409
            r = await c.post(base + "/api/watch/play", json={"episode_id": ep_id})
            check("play without Connector -> 409", r.status_code == 409, f"status={r.status_code}")

            # 2) status disconnected
            st = (await c.get(base + "/api/connector/status")).json()
            check("status: disconnected", st.get("connected") is False, str(st))

            # 3) setup endpoint exposes token + ws url
            setup = (await c.get(base + "/api/connector/setup")).json()
            check("setup: has_token + token", setup.get("has_token") and setup.get("token") == token)
            check("setup: ws_url is ws://", str(setup.get("ws_url", "")).startswith("ws://"), setup.get("ws_url", ""))

            ws_url = base.replace("http", "ws", 1) + f"/api/connector/ws?token={token}"
            async with websockets.connect(ws_url) as ws:
                await ws.send(json.dumps({"type": "register", "chrome": True, "migaku": True, "version": "test"}))
                await asyncio.sleep(0.2)
                st = (await c.get(base + "/api/connector/status")).json()
                check("status: connected + chrome + migaku", st.get("connected") and st.get("chrome") and st.get("migaku"), str(st))

                # 4) play -> relayed command, ack, 200
                cap: dict = {}

                async def play_call():
                    return await c.post(base + "/api/watch/play", json={"episode_id": ep_id, "seek_ms": 6000})

                async def connector_play():
                    msg = json.loads(await ws.recv())
                    cap.update(msg)
                    await ws.send(json.dumps({"type": "ack", "id": msg["id"], "ok": True,
                                              "verify": {"hasVideo": True, "paused": False}}))

                play_res, _ = await asyncio.gather(play_call(), connector_play())
                check("play -> 200", play_res.status_code == 200, f"status={play_res.status_code} {play_res.text[:160]}")
                check("play cmd=play", cap.get("cmd") == "play")
                check("play video_url carries token", "video_url" in cap and "token=" in cap["video_url"], cap.get("video_url", ""))
                check("play sub_url present", bool(cap.get("sub_url")))
                check("play seek_ms=6000", cap.get("seek_ms") == 6000, str(cap.get("seek_ms")))

                # 5) media range
                vurl = cap["video_url"]
                r = await c.get(vurl, headers={"Range": "bytes=0-99"})
                check("media Range -> 206", r.status_code == 206, f"status={r.status_code}")
                check("media Range len=100", len(r.content) == 100, f"len={len(r.content)}")
                check("media Content-Range", r.headers.get("content-range", "").startswith("bytes 0-99/10948509"), r.headers.get("content-range", ""))
                check("media Accept-Ranges", r.headers.get("accept-ranges") == "bytes")
                r = await c.get(vurl)
                check("media full -> 200 (10948509B)", r.status_code == 200 and len(r.content) == 10948509, f"status={r.status_code} len={len(r.content)}")
                r = await c.get(vurl.split("?")[0])  # no token
                check("media without token -> 401", r.status_code == 401, f"status={r.status_code}")
                # tampered scope: video token used on the subtitle route
                vtok = vurl.split("token=")[1]
                r = await c.get(base + f"/api/media/episode/{ep_id}/subtitle?token={vtok}")
                check("media wrong-scope token -> 401", r.status_code == 401, f"status={r.status_code}")

                # 6) subtitle
                r = await c.get(cap["sub_url"])
                check("subtitle -> 200 (306B)", r.status_code == 200 and len(r.content) == 306, f"status={r.status_code} len={len(r.content)}")

                # 7) play-moment relays seek = line.start_ms
                cap2: dict = {}

                async def moment_call():
                    return await c.post(base + "/api/watch/play-moment", json={"line_id": line_id})

                async def connector_moment():
                    msg = json.loads(await ws.recv())
                    cap2.update(msg)
                    await ws.send(json.dumps({"type": "ack", "id": msg["id"], "ok": True}))

                mres, _ = await asyncio.gather(moment_call(), connector_moment())
                check("play-moment -> 200", mres.status_code == 200, f"status={mres.status_code}")
                check("play-moment seek=6000", cap2.get("seek_ms") == 6000, str(cap2.get("seek_ms")))

                # 8) known/upload (auth)
                words = [
                    {"dictForm": "テスト", "reading": "てすと", "knownStatus": "KNOWN"},
                    {"dictForm": "猫", "reading": "ねこ", "knownStatus": "LEARNING"},
                ]
                r = await c.post(base + "/api/known/upload", headers={"authorization": f"Bearer {token}"}, json={"words": words})
                check("known/upload -> 200 (wrote 2)", r.status_code == 200 and r.json().get("written") == 2, r.text[:160])
                r = await c.post(base + "/api/known/upload", json={"words": []})
                check("known/upload without token -> 401", r.status_code == 401, f"status={r.status_code}")
                r = await c.get(base + "/api/known/summary")
                check("known/summary known>=1", r.json().get("known", 0) >= 1, r.text[:160])

                # 9) known/sync relays `sync-known`
                cap3: dict = {}

                async def sync_call():
                    return await c.post(base + "/api/known/sync")

                async def connector_sync():
                    msg = json.loads(await ws.recv())
                    cap3.update(msg)
                    await ws.send(json.dumps({"type": "ack", "id": msg["id"], "ok": True,
                                              "result": {"written": 2, "counts": {"KNOWN": 1, "LEARNING": 1}}}))

                sres, _ = await asyncio.gather(sync_call(), connector_sync())
                check("known/sync -> 200", sres.status_code == 200, f"status={sres.status_code} {sres.text[:160]}")
                check("known/sync cmd=sync-known", cap3.get("cmd") == "sync-known")

                # 10) comprehension/upload (auth)
                r = await c.post(
                    base + "/api/learn/comprehension/upload",
                    headers={"authorization": f"Bearer {token}"},
                    json={"episode_id": ep_id, "stats": {"pct": 66, "rating": "Challenging", "known": 15, "unknown": 5, "ignored": 2}},
                )
                body = r.json() if r.status_code == 200 else {}
                check("comprehension/upload -> 200", r.status_code == 200, r.text[:160])
                check("comprehension exact pct=66 source=exact", abs(body.get("comprehension_pct", 0) - 66) < 0.01 and body.get("source") == "exact", str(body)[:160])

            # 11) after WS close -> disconnected
            await asyncio.sleep(0.3)
            st = (await c.get(base + "/api/connector/status")).json()
            check("status: offline after Connector closes", st.get("connected") is False, str(st))

            # 12) multi-device: two identified Connectors coexist + targeted routing
            async def register(w, device_id, name, migaku):
                await w.send(json.dumps({
                    "type": "register", "device_id": device_id, "device_name": name,
                    "chrome": True, "migaku": migaku, "version": "test",
                }))

            async def serve_play(w):
                """Receive one command on `w`, ack it, return the command."""
                msg = json.loads(await w.recv())
                await w.send(json.dumps({"type": "ack", "id": msg["id"], "ok": True,
                                         "verify": {"hasVideo": True, "paused": False}}))
                return msg

            async def play_via(payload):
                return await c.post(base + "/api/watch/play", json=payload)

            async with websockets.connect(ws_url) as wa, websockets.connect(ws_url) as wb:
                await register(wa, "device-a", "Device A", migaku=True)
                await register(wb, "device-b", "Device B", migaku=False)
                await asyncio.sleep(0.2)

                st = (await c.get(base + "/api/connector/status")).json()
                ids = {d.get("device_id") for d in st.get("devices", [])}
                check("multi: both devices listed",
                      st.get("device_count") == 2 and ids == {"device-a", "device-b"}, str(st)[:200])
                check("multi: aggregate connected+migaku (any device)",
                      st.get("connected") and st.get("migaku"), str(st)[:160])
                check("multi: default prefers the Migaku-ready device",
                      st.get("default_device_id") == "device-a", str(st.get("default_device_id")))

                # targeted play -> lands on steam-test only
                (pres, pcmd) = await asyncio.gather(
                    play_via({"episode_id": ep_id, "device_id": "device-b"}), serve_play(wb))
                check("multi: targeted play -> 200", pres.status_code == 200,
                      f"status={pres.status_code} {pres.text[:120]}")
                check("multi: command reached the targeted device", pcmd.get("cmd") == "play")
                check("multi: response names the device",
                      pres.json().get("device_id") == "device-b", pres.text[:160])
                try:
                    await asyncio.wait_for(wa.recv(), timeout=0.4)
                    other_got_frame = True
                except asyncio.TimeoutError:
                    other_got_frame = False
                check("multi: other device saw no command", not other_got_frame)

                # untargeted play -> default routing picks the Migaku-ready device
                (pres, pcmd) = await asyncio.gather(
                    play_via({"episode_id": ep_id}), serve_play(wa))
                check("multi: untargeted play -> Migaku-ready device",
                      pres.status_code == 200 and pres.json().get("device_id") == "device-a",
                      pres.text[:160])

                # unknown device -> 409 (and names the device in the detail)
                r = await play_via({"episode_id": ep_id, "device_id": "no-such-box"})
                check("multi: unknown device -> 409", r.status_code == 409, f"status={r.status_code}")
                check("multi: 409 names the missing device", "no-such-box" in r.text, r.text[:160])

                # same-device reconnect supersedes ONLY its own old socket
                async with websockets.connect(ws_url) as wb2:
                    await register(wb2, "device-b", "Device B", migaku=False)
                    await asyncio.sleep(0.2)
                    st = (await c.get(base + "/api/connector/status")).json()
                    check("multi: reconnect keeps device_count=2",
                          st.get("device_count") == 2, str(st.get("device_count")))
                    superseded_code = None
                    try:
                        await asyncio.wait_for(wb.recv(), timeout=2.0)
                    except websockets.exceptions.ConnectionClosed as e:
                        superseded_code = e.rcvd.code if e.rcvd else None
                    except asyncio.TimeoutError:
                        pass
                    check("multi: old same-device socket closed with 4000",
                          superseded_code == 4000, str(superseded_code))
                    (pres, _) = await asyncio.gather(
                        play_via({"episode_id": ep_id, "device_id": "device-b"}), serve_play(wb2))
                    check("multi: commands route to the new socket", pres.status_code == 200,
                          f"status={pres.status_code}")

                # Device B's socket closed -> Device A remains connected + default
                await asyncio.sleep(0.3)
                st = (await c.get(base + "/api/connector/status")).json()
                check("multi: one device left after the other closes",
                      st.get("connected") is True and st.get("device_count") == 1
                      and st.get("default_device_id") == "device-a", str(st)[:200])

            # 13) all sockets closed -> offline again
            await asyncio.sleep(0.3)
            st = (await c.get(base + "/api/connector/status")).json()
            check("multi: offline after all devices close", st.get("connected") is False, str(st)[:160])

    try:
        asyncio.run(run())
    except Exception as e:
        import traceback
        traceback.print_exc()
        check("selftest ran without exception", False, str(e))
    finally:
        server.should_exit = True
        th.join(timeout=5)
        shutil.rmtree(tmp, ignore_errors=True)

    failed = [n for n, ok, _ in results if not ok]
    print(f"\n==== {len(results) - len(failed)}/{len(results)} checks passed ====")
    if failed:
        print("FAILED:")
        for n in failed:
            print("  -", n)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
