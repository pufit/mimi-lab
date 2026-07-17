# Mimi Lab — Connector

The **Connector** is the small piece that runs on the viewing device
(Device A or Device B with Chrome + Migaku).

It does four things:

1. **Ensures your dedicated Chrome+Migaku is running** (external CDP on
   `127.0.0.1:9222`, dedicated `--user-data-dir`).
2. **Dials *out* to the Lab server** over a WebSocket — no inbound port, so it
   works from behind NAT and sidesteps browser CORS / Private-Network-Access.
3. On a one-click **Play** in the Lab UI, **injects the server's media URL** into
   Migaku's Player. Migaku `fetch()`es the bytes itself, plays, and tokenizes —
   full immersion (dictionary, furigana, known-status, SRS). No drag-and-drop.
4. **Pushes your known-words + Migaku-exact comprehension** back up to the server.

> Why not a browser extension? Chrome blocks `chrome.debugger` from attaching to
> another extension's pages, so an extension physically can't inject into Migaku's
> Player. External CDP (what the Connector uses) is exempt. (Design §2.)

## Install & run

Requires **Node ≥ 18** (uses the built-in `WebSocket` on Node ≥ 22; falls back to
the `ws` package otherwise) and Google Chrome.

### Easiest: one-line installer (recommended)
The server hosts the Connector + a self-updating installer. On the machine where
you watch, run the command shown on the Lab's **Settings → Connector** page:

```bash
curl -fsSL "https://<your-server>/api/connector/install.sh?token=<token>" | bash
```

It downloads the Connector, installs deps, and starts a launcher at
`~/.mimi-lab-connector/start.sh` that **re-pulls the latest bundle on every
launch** — so when the server's Connector code changes, you just re-run
`start.sh` (or it updates next start). No manual copying.

### Manual (from the repo)
```bash
cd connector
npm install                         # playwright-core, sql.js, pako, ws
node index.mjs --server https://<your-server> --token <secret>
# or via env / .env vars (SERVER_URL, MIMI_LAB_TOKEN); see .env.example
```

### First-time setup (once)
1. Start the Connector — it launches a dedicated Chrome profile and opens the
   Migaku Player page.
2. In that Chrome, **install Migaku (Early Access) from the Web Store and log in**.
   (Same one-time prerequisite as before; it's the dedicated profile so it stays
   logged in.)
3. The Lab UI's **Settings → Connector** chip turns green (`Migaku ✓`).

### Every day
Just leave it running (or set it up as a login item). Click **▶ Play in Migaku**
on any episode in the Lab and it autoplays in your Migaku.

## Configuration

| Env / flag | Default | Meaning |
|---|---|---|
| `SERVER_URL` / `--server` | `http://127.0.0.1:8000` | Lab server base URL. **Plain http on your LAN is fine** — the Connector relays media over loopback (no HTTPS/certs needed). |
| `MIMI_LAB_TOKEN` / `--token` | — | Shared secret; must match the server's `.env`. |
| `CDP_URL` / `--cdp` | `http://127.0.0.1:9222` | Dedicated Chrome's CDP endpoint (loopback). |
| `CONNECTOR_PROXY_PORT` | `8787` | Loopback media-proxy port (Migaku fetches `http://127.0.0.1:<port>`). Port 8788 is reserved for the migaku-tokenizer sidecar. |
| `CONNECTOR_KNOWN_SYNC_MS` | `1800000` (30 min) | Periodic known-words push while connected (0 = off). A sync also fires automatically when a viewing session ends. |
| `CONNECTOR_DEVICE_ID` / `--device-id` | opaque random id (persisted) | Stable routing id stored at `~/.mimi-lab-connector/device-id`, outside the self-updating bundle. It does not contain the hostname. |
| `CONNECTOR_DEVICE_NAME` / `--name` | anonymous `Connector <suffix>` | Human label shown in the device picker. Set an explicit friendly label if desired. |
| `CHROME_BIN` | OS default | Chrome binary to launch. |
| `MIGAKU_USER_DATA_DIR` | derived from the user's home directory | Dedicated profile dir (keeps Migaku logged in). |
| `CONNECTOR_LAUNCH_CHROME` | `1` | `0` = never auto-launch Chrome (you start it yourself). |
| `CONNECTOR_DRY_RUN` / `--dry-run` | off | Speak the protocol + ack play commands **without** Chrome (no playwright/sql.js needed). For first-run wiring tests. |

## Multiple devices

Run the Connector on each viewing device (for example, **Device A** and
**Device B**) — they all stay connected to the Lab at once. Each Connector
announces a stable `device_id` + `device_name` (see the config table), the
server keeps one socket per device, and:

- with **one** device connected everything behaves exactly as before;
- with **several**, the sidebar Connector chip grows a picker — target a
  specific device or leave it on **Auto** (Migaku-ready, most recently used
  device wins). Settings → Connector lists every device.
- a reconnect from the *same* device replaces its old (possibly half-open)
  socket; different devices never kick each other off.

If Device B was cloned from Device A (including `~/.mimi-lab-connector`), the
two copies share a device id and will supersede each other — delete
`~/.mimi-lab-connector/device-id` on one (or set `CONNECTOR_DEVICE_ID`) to
split them.

## How it talks to the server

- **Out:** WebSocket `wss?://<server>/api/connector/ws?token=…` — registers
  (with its `device_id`/`device_name`), heartbeats `{chrome, migaku}`, and
  receives `{cmd:"play"|"sync-known", …}`, acking each.
- **Up (authenticated POST, `Authorization: Bearer <token>`):**
  - `POST /api/known/upload` — `{words:[{dictForm,reading,knownStatus}]}`
  - `POST /api/learn/comprehension/upload` — `{episode_id, stats}`
- **Media (loopback proxy):** Migaku's Player is a secure context and won't fetch
  plain `http://` from a non-loopback host. So the Connector runs a tiny proxy on
  `http://127.0.0.1:<CONNECTOR_PROXY_PORT>` and rewrites the play URLs to it;
  Migaku fetches loopback (always allowed) and the Connector relays the bytes
  (Range-forwarded) from `GET /api/media/episode/{id}/video|subtitle?token=…` on
  the server. **This is why plain http on the LAN works with no TLS.**

## Self-tests
```bash
node selftest.mjs        # config + WS-URL logic (no server/Chrome needed)
# full end-to-end protocol test (server + a DRY_RUN connector):
#   from the repo root:  .venv/bin/python -m app.connector.selftest
```

## Packaging (later — design §7/§8 P4)
For P1 this Node CLI is the Connector. A signed tray app / login-item with a
guided first-run is the P4 packaging step.
