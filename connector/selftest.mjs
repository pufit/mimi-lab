// selftest.mjs — Connector config + WS-URL logic (no server/Chrome needed).
//
//   node selftest.mjs
//
// The full end-to-end protocol round-trip (server relay + a DRY_RUN connector)
// is exercised by the server selftest: `python -m app.connector.selftest`.
import { loadConfig } from './config.mjs';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const savedIdentityEnv = {
  CONNECTOR_STATE_DIR: process.env.CONNECTOR_STATE_DIR,
  CONNECTOR_DEVICE_ID: process.env.CONNECTOR_DEVICE_ID,
  CONNECTOR_DEVICE_NAME: process.env.CONNECTOR_DEVICE_NAME,
};
const testStateDir = fs.mkdtempSync(path.join(os.tmpdir(), 'migaku-connector-selftest-'));
process.env.CONNECTOR_STATE_DIR = testStateDir;
delete process.env.CONNECTOR_DEVICE_ID;
delete process.env.CONNECTOR_DEVICE_NAME;

const results = [];
function check(name, cond, detail = '') {
  results.push(!!cond);
  console.log(`${cond ? 'PASS' : 'FAIL'}  ${name}${detail ? '  — ' + detail : ''}`);
}

// 1) flags override env; https → wss; token in query; dry-run flag
{
  const c = loadConfig(['--server', 'https://lab.example.net', '--token', 'abc123', '--dry-run']);
  check('flag server parsed', c.serverUrl === 'https://lab.example.net', c.serverUrl);
  check('https → wss', c.wsUrl.startsWith('wss://'), c.wsUrl);
  check('ws path is /api/connector/ws', c.wsUrl.includes('/api/connector/ws'), c.wsUrl);
  check('token in query', c.wsUrl.includes('token=abc123'), c.wsUrl);
  check('dry-run flag', c.dryRun === true);
}

// 2) defaults: http → ws, default server/port
{
  const c = loadConfig([]);
  check('default server', c.serverUrl === 'http://127.0.0.1:8000', c.serverUrl);
  check('http → ws', c.wsUrl.startsWith('ws://') && !c.wsUrl.startsWith('wss://'), c.wsUrl);
  check('default cdp port 9222', c.debugPort === 9222, String(c.debugPort));
  check('player url targets EA player', c.playerUrl.includes('/pages/player/index.html'), c.playerUrl);
}

// 3) trailing slash on server URL is normalized
{
  const c = loadConfig(['--server', 'http://host:8000/']);
  check('trailing slash trimmed', c.serverUrl === 'http://host:8000', c.serverUrl);
  check('ws built from trimmed', c.wsUrl === 'ws://host:8000/api/connector/ws', c.wsUrl);
}

// 4) device identity: opaque, anonymous, stable, and overridable (multi-device)
{
  const c1 = loadConfig([]);
  check('device id is opaque', /^connector-[0-9a-f]{32}$/.test(c1.deviceId), c1.deviceId);
  check('device name is anonymous', /^Connector [0-9a-f]{6}$/.test(c1.deviceName), c1.deviceName);
  const c2 = loadConfig([]);
  check('device id stable across loads', c1.deviceId === c2.deviceId, `${c1.deviceId} vs ${c2.deviceId}`);
  process.env.CONNECTOR_DEVICE_ID = 'device-a';
  process.env.CONNECTOR_DEVICE_NAME = 'Device A';
  const c3 = loadConfig([]);
  check('env device id overrides', c3.deviceId === 'device-a', c3.deviceId);
  check('env device name overrides', c3.deviceName === 'Device A', c3.deviceName);
  const c4 = loadConfig(['--device-id', 'device-b', '--name', 'Device B']);
  check('--device-id overrides env', c4.deviceId === 'device-b', c4.deviceId);
  check('--name overrides env', c4.deviceName === 'Device B', c4.deviceName);
}

for (const [key, value] of Object.entries(savedIdentityEnv)) {
  if (value === undefined) delete process.env[key];
  else process.env[key] = value;
}
fs.rmSync(testStateDir, { recursive: true, force: true });

const passed = results.filter(Boolean).length;
console.log(`\n==== ${passed}/${results.length} checks passed ====`);
process.exit(passed === results.length ? 0 : 1);
