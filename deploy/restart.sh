#!/usr/bin/env bash
# Restart Mimi Lab services THROUGH launchd (the only correct way — a manual
# `uvicorn` run fights the KeepAlive agent and causes repeated bind conflicts).
#
#   deploy/restart.sh              # restart the API server
#   deploy/restart.sh all          # server + transmission + tokenizer
#   deploy/restart.sh tokenizer    # one specific service
set -euo pipefail
UID_="$(id -u)"
LA="$HOME/Library/LaunchAgents"

restart_one() {
  local label="com.mimilab.$1"
  if launchctl print "gui/$UID_/$label" >/dev/null 2>&1; then
    echo "restarting $label (kickstart -k)"
    launchctl kickstart -k "gui/$UID_/$label"
  elif [ -f "$LA/$label.plist" ]; then
    echo "$label not loaded — bootstrapping"
    launchctl bootstrap "gui/$UID_" "$LA/$label.plist"
  else
    echo "$label has no plist — run deploy/install-launchd.sh first" >&2
    return 1
  fi
}

case "${1:-server}" in
  all)
    restart_one server
    restart_one transmission
    restart_one tokenizer
    ;;
  server|transmission|tokenizer)
    restart_one "$1"
    ;;
  *)
    echo "usage: $0 [server|transmission|tokenizer|all]" >&2
    exit 1
    ;;
esac

# quick health confirmation for the API
if [ "${1:-server}" = "server" ] || [ "${1:-server}" = "all" ]; then
  for _i in $(seq 1 20); do
    if curl -fsS -m 2 http://127.0.0.1:8000/api/health >/dev/null 2>&1; then
      echo "server is up"
      exit 0
    fi
    sleep 1
  done
  echo "WARNING: server did not answer /api/health within 20s — check data/server.log" >&2
  exit 1
fi
