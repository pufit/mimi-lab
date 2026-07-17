#!/usr/bin/env bash
# Remove the Mimi Lab LaunchAgents.
set -euo pipefail
LA="$HOME/Library/LaunchAgents"
UID_="$(id -u)"
for L in com.mimilab.server com.mimilab.companion com.mimilab.transmission com.mimilab.tokenizer; do
  : # com.mimilab.companion kept here so older installs are cleaned up too
  launchctl bootout "gui/$UID_/$L" 2>/dev/null || true
  rm -f "$LA/$L.plist"
done
echo "removed Mimi Lab LaunchAgents"
