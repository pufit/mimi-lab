#!/usr/bin/env bash
# Mimi Lab launcher: the API server (serves the web UI, streams media, and relays
# Play to the Connector). Playback no longer runs here — the Connector runs on the
# machine where you WATCH (Chrome + Migaku); see connector/README.md.
set -e
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

echo "→ API + UI on http://${HOST:-127.0.0.1}:${API_PORT:-8000}"
.venv/bin/uvicorn app.main:app --host "${HOST:-127.0.0.1}" --port "${API_PORT:-8000}"
# Transmission daemon, if not under launchd:
#   transmission-daemon -f -g data/transmission -w data/inbox -T -p 9091 --rpc-bind-address 127.0.0.1
# The Connector (on your viewing machine):
#   cd connector && npm install && node index.mjs --server http://<this-host>:8000 --token <MIMI_LAB_TOKEN>
