"""Self-serve Connector distribution.

The server hosts the Connector source as a tarball + a one-line installer, so the
user runs a single `curl … | bash` on their watching machine and gets a
**self-updating** Connector (it re-pulls the latest bundle from the server on each
launch). No manual copying when the Connector code changes.
"""
from __future__ import annotations

import io
import tarfile
from pathlib import Path

CONNECTOR_DIR = Path(__file__).resolve().parents[2] / "connector"

_EXCLUDE_DIRS = {"node_modules", ".git", "__pycache__"}
_EXCLUDE_NAMES = {".env"}


def build_bundle() -> bytes:
    """Tar.gz the connector/ source (no node_modules, no .env, no scratch files).

    Arcnames are relative to connector/, so `tar -xz -C <dir>` drops the files
    straight into <dir>.
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for p in sorted(CONNECTOR_DIR.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(CONNECTOR_DIR)
            if set(rel.parts) & _EXCLUDE_DIRS:
                continue
            if p.name in _EXCLUDE_NAMES or p.suffix == ".log" or p.name.startswith("_"):
                continue
            tar.add(str(p), arcname=str(rel))
    return buf.getvalue()


# `__SERVER__` / `__TOKEN__` are substituted per-request. The inner start.sh is
# emitted via a QUOTED heredoc so its `$(dirname "$0")` stays literal; the two
# placeholders inside it are still replaced here (baked into start.sh).
_INSTALL_TEMPLATE = r'''#!/usr/bin/env bash
# Mimi Lab Connector — installer (served by your Lab server). Safe to re-run.
set -euo pipefail

SERVER="__SERVER__"
TOKEN="__TOKEN__"
DIR="${MIGAKU_CONNECTOR_DIR:-$HOME/.mimi-lab-connector}"

for c in node npm curl tar; do
  command -v "$c" >/dev/null 2>&1 || { echo "✗ '$c' is required. Install it (Node.js: https://nodejs.org) and re-run."; exit 1; }
done

echo "→ Installing the Mimi Lab Connector into $DIR"
mkdir -p "$DIR"
curl -fsSL "$SERVER/api/connector/bundle.tgz?token=$TOKEN" | tar -xz -C "$DIR"

# self-updating launcher: pulls the latest bundle on each start, then runs
cat > "$DIR/start.sh" <<'MIGAKU_START_EOF'
#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
curl -fsSL "__SERVER__/api/connector/bundle.tgz?token=__TOKEN__" | tar -xz -C .
npm install --no-audit --no-fund --silent
exec node index.mjs --server "__SERVER__" --token "__TOKEN__"
MIGAKU_START_EOF
chmod +x "$DIR/start.sh"

echo "→ Installing dependencies (first run is the slow one)…"
( cd "$DIR" && npm install --no-audit --no-fund --silent )

echo ""
echo "✓ Connector installed at $DIR"
echo "  Starting it now — leave this running (Ctrl-C to stop)."
echo "  Next time just run:  $DIR/start.sh   (it auto-updates each launch)"
echo ""
exec "$DIR/start.sh"
'''


def install_script(server_url: str, token: str) -> str:
    return _INSTALL_TEMPLATE.replace("__SERVER__", server_url).replace("__TOKEN__", token)
