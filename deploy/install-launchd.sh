#!/usr/bin/env bash
# Install Mimi Lab as macOS LaunchAgents (auto-start on login, KeepAlive).
#   server       → API + web UI on 127.0.0.1:8000 (also streams media + relays Play)
#   transmission → headless torrent daemon on :9091
# The Connector runs on the machine where you WATCH (Chrome+Migaku), not here —
# see connector/README.md. Re-runnable. Uninstall with deploy/uninstall-launchd.sh
set -euo pipefail
ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
LA="$HOME/Library/LaunchAgents"
UID_="$(id -u)"
mkdir -p "$LA" "$ROOT/data"

NODE="$(command -v node)"
UVICORN="$ROOT/.venv/bin/uvicorn"
BINPATH="$(dirname "$NODE"):/opt/homebrew/bin:/usr/bin:/bin:/usr/local/bin"

cat > "$LA/com.mimilab.server.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.mimilab.server</string>
  <key>ProgramArguments</key><array>
    <string>$UVICORN</string><string>app.main:app</string>
    <string>--host</string><string>127.0.0.1</string><string>--port</string><string>8000</string>
  </array>
  <key>WorkingDirectory</key><string>$ROOT</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$ROOT/data/server.log</string>
  <key>StandardErrorPath</key><string>$ROOT/data/server.log</string>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>$BINPATH</string></dict>
</dict></plist>
PLIST

cat > "$LA/com.mimilab.transmission.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.mimilab.transmission</string>
  <key>ProgramArguments</key><array>
    <string>$(command -v transmission-daemon)</string><string>-f</string>
    <string>-g</string><string>$ROOT/data/transmission</string>
    <string>-w</string><string>$ROOT/data/inbox</string>
    <string>-T</string><string>-p</string><string>9091</string>
    <string>--rpc-bind-address</string><string>127.0.0.1</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$ROOT/data/transmission.log</string>
  <key>StandardErrorPath</key><string>$ROOT/data/transmission.log</string>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>$BINPATH</string></dict>
</dict></plist>
PLIST

cat > "$LA/com.mimilab.tokenizer.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.mimilab.tokenizer</string>
  <key>ProgramArguments</key><array>
    <string>$NODE</string><string>$ROOT/tools/migaku-tokenizer/server.mjs</string>
  </array>
  <key>WorkingDirectory</key><string>$ROOT/tools/migaku-tokenizer</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$ROOT/data/tokenizer.log</string>
  <key>StandardErrorPath</key><string>$ROOT/data/tokenizer.log</string>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>$BINPATH</string>
    <key>MIGAKU_TOK_PORT</key><string>8788</string>
  </dict>
</dict></plist>
PLIST

# Drop the superseded server-side Companion agent if it's still around
# (topology replaced by the user-side Connector, 2026-06-27).
launchctl bootout "gui/$UID_/com.mimilab.companion" 2>/dev/null || true
rm -f "$LA/com.mimilab.companion.plist"

# Free the ports from any manually-started instances, then (re)load.
pkill -f "uvicorn app.main:app" 2>/dev/null || true
pkill -f "transmission-daemon" 2>/dev/null || true
pkill -f "migaku-tokenizer/server.mjs" 2>/dev/null || true
sleep 1
for L in com.mimilab.server com.mimilab.transmission com.mimilab.tokenizer; do
  launchctl bootout "gui/$UID_/$L" 2>/dev/null || true
  # bootout is async; bootstrap can return EIO(5) while the old instance is still
  # unloading. Retry until it takes (and never abort the loop under `set -e`).
  for _i in 1 2 3 4 5 6 7 8 9 10; do
    if launchctl bootstrap "gui/$UID_" "$LA/$L.plist" 2>/dev/null; then break; fi
    sleep 1
  done
  launchctl enable "gui/$UID_/$L" 2>/dev/null || true
done
echo "loaded: com.mimilab.server, com.mimilab.transmission, com.mimilab.tokenizer"
