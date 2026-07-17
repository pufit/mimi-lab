// config.mjs — Connector configuration from env + CLI flags.
//
// Required:  SERVER_URL (the Lab server), MIMI_LAB_TOKEN (shared secret).
// CDP:       CDP_URL (default http://127.0.0.1:9222) + Chrome launch opts.
// Identity:  every Connector announces a stable device id + name, so several
//            machines can stay connected to the Lab at once and the UI can
//            target plays at a specific one.
//
// Flags override env:  --server <url>  --token <secret>  --cdp <url>
//                      --device-id <id>  --name <label>  --dry-run
import crypto from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

function pkgVersion() {
  try {
    const p = path.join(path.dirname(fileURLToPath(import.meta.url)), 'package.json');
    return JSON.parse(fs.readFileSync(p, 'utf8')).version || '0.0.0';
  } catch { return '0.0.0'; }
}

function parseFlags(argv) {
  const out = {};
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (!a.startsWith('--')) continue;
    const key = a.slice(2);
    const next = argv[i + 1];
    if (next === undefined || next.startsWith('--')) out[key] = true;
    else { out[key] = next; i++; }
  }
  return out;
}

function defaultChrome() {
  if (process.platform === 'darwin') return '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
  if (process.platform === 'win32') return 'C:/Program Files/Google/Chrome/Application/chrome.exe';
  return 'google-chrome'; // linux: resolved via PATH
}

function toWs(serverUrl, token) {
  const u = new URL(serverUrl);
  u.protocol = u.protocol === 'https:' ? 'wss:' : 'ws:';
  u.pathname = (u.pathname.replace(/\/$/, '')) + '/api/connector/ws';
  if (token) u.searchParams.set('token', token);
  return u.toString();
}

/** Stable, pseudonymous device identity for multi-Connector routing.
 *
 * New ids are opaque random values persisted outside the bundle directory, so
 * neither the id nor its default display label reveals the machine hostname.
 * Existing persisted ids are preserved for routing compatibility. Overrides:
 * `--device-id` / CONNECTOR_DEVICE_ID and `--name` / CONNECTOR_DEVICE_NAME.
 * CONNECTOR_STATE_DIR is primarily useful for isolated tests/packaging. */
function deviceIdentity(env, f) {
  let deviceId = String(f['device-id'] || env.CONNECTOR_DEVICE_ID || '').trim();
  if (!deviceId) {
    const stateDir = env.CONNECTOR_STATE_DIR
      ? path.resolve(env.CONNECTOR_STATE_DIR)
      : path.join(os.homedir(), '.mimi-lab-connector');
    const idFile = path.join(stateDir, 'device-id');
    try { deviceId = fs.readFileSync(idFile, 'utf8').trim(); } catch { /* first run */ }
    if (!deviceId) {
      deviceId = `connector-${crypto.randomBytes(16).toString('hex')}`;
      try {
        fs.mkdirSync(path.dirname(idFile), { recursive: true });
        fs.writeFileSync(idFile, deviceId + '\n', { mode: 0o600 });
      } catch { /* unwritable home — ephemeral id, see above */ }
    }
  }
  const anonymousSuffix = crypto.createHash('sha256').update(deviceId).digest('hex').slice(0, 6);
  const deviceName = String(
    f.name || env.CONNECTOR_DEVICE_NAME || `Connector ${anonymousSuffix}`,
  ).trim() || `Connector ${anonymousSuffix}`;
  return { deviceId, deviceName };
}

export function loadConfig(argv = process.argv.slice(2)) {
  const f = parseFlags(argv);
  const env = process.env;

  const serverUrl = (f.server || env.SERVER_URL || env.MIMI_LAB_SERVER || 'http://127.0.0.1:8000').replace(/\/$/, '');
  const token = f.token || env.MIMI_LAB_TOKEN || '';
  const cdpUrl = f.cdp || env.CDP_URL || 'http://127.0.0.1:9222';
  const extId = env.MIGAKU_EXT_ID || 'dmeppfcidcpcocleneopiblmpnbokhep';
  const chromeBin = env.CHROME_BIN || defaultChrome();
  const userDataDir = env.MIGAKU_USER_DATA_DIR || path.join(os.homedir(), '.migaku-cdp');
  let debugPort = parseInt(env.CDP_PORT || '', 10);
  if (!debugPort) { try { debugPort = parseInt(new URL(cdpUrl).port || '9222', 10); } catch { debugPort = 9222; } }
  // 8787 is the Connector's loopback media proxy; 8788 remains reserved for the
  // migaku-tokenizer sidecar. CONNECTOR_PROXY_PORT still overrides this value.
  const proxyPort = parseInt(env.CONNECTOR_PROXY_PORT || '8787', 10);
  const dryRun = !!f['dry-run'] || /^(1|true|yes|on)$/i.test(env.CONNECTOR_DRY_RUN || '');
  const launchChrome = !/^(0|false|no|off)$/i.test(env.CONNECTOR_LAUNCH_CHROME || '1');
  const heartbeatMs = parseInt(env.CONNECTOR_HEARTBEAT_MS || '15000', 10);
  const reconnectMs = parseInt(env.CONNECTOR_RECONNECT_MS || '3000', 10);
  // Periodic known-words push while connected (0 = off), so changes made during
  // a viewing session are reflected without a manual sync.
  const knownSyncMs = parseInt(env.CONNECTOR_KNOWN_SYNC_MS || String(30 * 60 * 1000), 10);

  return {
    serverUrl,
    wsUrl: toWs(serverUrl, token),
    token,
    ...deviceIdentity(env, f),
    cdpUrl,
    extId,
    playerUrl: `chrome-extension://${extId}/pages/player/index.html`,
    chromeBin,
    userDataDir,
    debugPort,
    proxyPort,
    dryRun,
    launchChrome,
    heartbeatMs,
    reconnectMs,
    knownSyncMs,
    version: pkgVersion(),
  };
}
