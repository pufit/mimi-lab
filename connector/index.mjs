#!/usr/bin/env node
// index.mjs — the Mimi Lab Connector.
//
// Runs on the machine where you watch. It:
//   1. ensures your dedicated Chrome+Migaku is up (external CDP),
//   2. dials OUT to the Lab server over a WebSocket (no inbound port; sidesteps
//      NAT/CORS/Private-Network-Access — notes/REMOTE_PLAYBACK_DESIGN.md §3),
//   3. on a one-click Play, injects the server's media URL into Migaku's Player
//      so Migaku fetches + plays + tokenizes (full immersion),
//   4. pushes known-words + Migaku-exact comprehension back up,
//   5. watches playback (telemetry every 15s) so the server can track progress,
//      auto-mark watched, and push to MAL — and re-syncs known words when a
//      viewing session ends (you mine words WHILE watching; that's exactly when
//      the known-set changes).
//
// A browser extension physically can't do this (Chrome blocks cross-extension
// debugging — design §2e); the Connector owns the external-CDP link instead.
//
// The heavy CDP deps (playwright-core / sql.js / pako) are imported lazily, so
// `CONNECTOR_DRY_RUN=1` runs the full protocol with zero installed packages.
import { loadConfig } from './config.mjs';

const cfg = loadConfig();
const state = { chrome: false, migaku: false, extVersion: null };

function log(...a) { console.log('[connector]', ...a); }
function warn(...a) { console.error('[connector]', ...a); }

/** Prefer Node's built-in WebSocket (Node ≥22); fall back to the `ws` package. */
async function getWebSocket() {
  if (typeof globalThis.WebSocket === 'function') return globalThis.WebSocket;
  return (await import('ws')).default;
}

// ---- authenticated POST back to the server ----
async function post(path, body) {
  const headers = { 'content-type': 'application/json' };
  if (cfg.token) headers.authorization = 'Bearer ' + cfg.token;
  const r = await fetch(cfg.serverUrl + path, { method: 'POST', headers, body: JSON.stringify(body) });
  if (!r.ok) {
    const t = await r.text().catch(() => '');
    throw new Error(`POST ${path} -> ${r.status} ${t.slice(0, 200)}`);
  }
  return r.json().catch(() => ({}));
}

// ---- {chrome, migaku, extVersion} for register/heartbeat ----
async function refreshState() {
  if (cfg.dryRun) { state.chrome = true; state.migaku = true; return state; }
  try {
    const { probe } = await import('./cdp.mjs');
    const p = await probe(cfg);
    state.chrome = p.chrome;
    state.migaku = p.migaku;
    state.extVersion = p.extVersion || state.extVersion;
  } catch { state.chrome = false; state.migaku = false; }
  return state;
}

// ---- proxy state (see boot) ----
let proxyUp = false;

function isLoopback(url) {
  try {
    const h = new URL(url).hostname;
    return h === '127.0.0.1' || h === 'localhost' || h === '::1';
  } catch { return false; }
}

/** Media URL for Migaku's secure-context fetch: through the loopback proxy when
 * it's up; direct when the server itself is loopback (always allowed); otherwise
 * fail loudly. */
function mediaUrl(originalUrl, proxifyFn) {
  if (!originalUrl) return null;
  if (proxyUp) return proxifyFn(cfg, originalUrl);
  if (isLoopback(cfg.serverUrl)) return originalUrl;
  throw new Error(
    `media proxy is not running on :${cfg.proxyPort} and the server is not loopback — ` +
    'Migaku cannot fetch plain http from a remote host. Free the port or set CONNECTOR_PROXY_PORT.'
  );
}

// ---- play serialization + post-play watchers ----
// Plays are single-flight so concurrent requests cannot interleave DOM injection
// AND let play A's comprehension scrape read play B's stats (cross-episode
// contamination). A new play cancels the previous scrape + telemetry.
let playQueue = Promise.resolve();
let currentWatch = null;   // { token, episodeId, timer }

function cancelCurrentWatch(reason) {
  if (currentWatch) {
    currentWatch.token.cancelled = true;
    if (currentWatch.timer) clearInterval(currentWatch.timer);
    currentWatch = null;
    if (reason) log(`stopped watching previous episode (${reason})`);
  }
}

const TELEMETRY_MS = 15000;

/** Poll the player's <video> and stream progress to the server. Ends (and
 * triggers a known-words re-sync) when the video finishes / changes / vanishes. */
function startTelemetry(page, episodeId, send) {
  cancelCurrentWatch('new play');
  const token = { cancelled: false };
  let lastSrc = null;
  let missed = 0;

  const readVideo = () => page.evaluate(() => {
    const findVideo = (root) => {
      const v = root.querySelector('video'); if (v) return v;
      for (const el of root.querySelectorAll('*')) { if (el.shadowRoot) { const x = findVideo(el.shadowRoot); if (x) return x; } }
      return null;
    };
    const v = findVideo(document);
    if (!v) return null;
    return {
      currentTime: v.currentTime, duration: v.duration, paused: v.paused,
      ended: v.ended, src: (v.currentSrc || v.src || '').slice(0, 64),
    };
  });

  const finish = async (why) => {
    if (token.cancelled) return;
    cancelCurrentWatch(why);
    send({ type: 'session-end', episode_id: episodeId, reason: why });
    log(`session ended for ep=${episodeId} (${why}) — re-syncing known words`);
    try {
      const res = await handleSyncKnown();
      log('post-session known sync:', JSON.stringify(res.result || res));
    } catch (e) { warn('post-session known sync failed:', e && e.message ? e.message : e); }
  };

  const timer = setInterval(async () => {
    if (token.cancelled) { clearInterval(timer); return; }
    let v = null;
    try { v = await readVideo(); } catch { v = null; }
    if (!v) {
      missed += 1;
      if (missed >= 2) await finish('player gone');
      return;
    }
    missed = 0;
    if (lastSrc && v.src && v.src !== lastSrc) { await finish('video changed'); return; }
    lastSrc = lastSrc || v.src;
    const durMs = Number.isFinite(v.duration) ? Math.round(v.duration * 1000) : null;
    send({
      type: 'telemetry', episode_id: episodeId,
      position_ms: Math.round((v.currentTime || 0) * 1000),
      duration_ms: durMs, paused: !!v.paused, ended: !!v.ended,
    });
    if (v.ended) await finish('ended');
  }, TELEMETRY_MS);

  currentWatch = { token, episodeId, timer };
  return token;
}

// ---- command handlers ----
async function doPlay(msg, send) {
  const { episode_id, video_url, sub_url, sub2_url, seek_ms } = msg;
  if (!video_url) throw new Error('play: missing video_url');
  if (cfg.dryRun) {
    log(`DRY_RUN play ep=${episode_id} seek=${seek_ms ?? 0}ms video=${String(video_url).split('?')[0]}` +
        `${sub2_url ? ' +en' : ''}`);
    return { verify: { dryRun: true, hasVideo: true, paused: false } };
  }
  const { ensureChrome, getPlayerPage } = await import('./cdp.mjs');
  const { playRemote } = await import('./inject.mjs');
  const { scrapeAfterPlay } = await import('./comprehension.mjs');
  const { proxify } = await import('./proxy.mjs');

  cancelCurrentWatch('superseded');
  await ensureChrome(cfg);
  const page = await getPlayerPage(cfg);
  try { await page.bringToFront(); } catch {}
  // Route media through the loopback proxy so Migaku (a secure context) can fetch
  // it over http://127.0.0.1 even when the server is plain http on the LAN.
  // sub2_url is the English reference track — Migaku shows it as the secondary
  // subtitle when that's enabled in Migaku's settings (Japanese stays the study track).
  const verify = await playRemote(page, {
    videoUrl: mediaUrl(video_url, proxify),
    subUrl: mediaUrl(sub_url || null, proxify),
    sub2Url: mediaUrl(sub2_url || null, proxify),
    seekMs: seek_ms ?? null,
    onProgress: (pct, phase) => send({ type: 'progress', episode_id, pct, phase }),
  });
  log(`play ep=${episode_id}:`, JSON.stringify(verify));

  // watch playback → progress telemetry → auto-watched + post-session known sync
  const token = startTelemetry(page, episode_id, send);

  // best-effort: scrape Migaku-exact comprehension + push it up (P2). Guarded:
  // a cancelled scrape (new play) or a non-numeric pct (Migaku UI drift) must
  // never be uploaded — a null pct used to be coerced to a permanent 0%.
  (async () => {
    try {
      const scraped = await scrapeAfterPlay(page, { timeoutMs: 30000, cancelToken: token });
      if (token.cancelled) return;
      const pct = scraped && scraped.stats ? Number(scraped.stats.pct) : NaN;
      if (!Number.isFinite(pct) || pct < 0 || pct > 100) {
        if (scraped && scraped.stats) warn(`comprehension scrape returned pct=${scraped.stats.pct} — NOT uploading (drift?)`);
        return;
      }
      if (episode_id != null) {
        await post('/api/learn/comprehension/upload', { episode_id, stats: scraped.stats });
        log(`comprehension pushed ep=${episode_id}: ${JSON.stringify(scraped.stats)}`);
      }
    } catch (e) { warn('comprehension push skipped:', e && e.message ? e.message : e); }
  })();

  return { verify };
}

function handlePlay(msg, send) {
  // single-flight: chain plays so injections never interleave
  const run = playQueue.then(() => doPlay(msg, send));
  playQueue = run.catch(() => {});
  return run;
}

async function handleSyncKnown() {
  if (cfg.dryRun) { log('DRY_RUN sync-known (no upload)'); return { result: { written: 0, dryRun: true } }; }
  const { readKnownWords } = await import('./knownWords.mjs');
  const { rows, counts } = await readKnownWords(cfg);
  const res = await post('/api/known/upload', { words: rows });
  log(`known synced: ${rows.length} words ${JSON.stringify(counts)}`);
  return { result: { written: res.written ?? rows.length, removed: res.removed ?? 0, counts: res.counts ?? counts } };
}

/** Self-check: play the server-hosted TestShow fixture and assert the whole
 * inject→tokenize→panel chain works. The server compares the returned stats to
 * its baseline (≈66% / K15 U5 I2) — this is the drift guard for the
 * browser-facing couplings (input seam, token attrs, panel classes). */
async function handleSelfcheck(msg, send) {
  if (cfg.dryRun) return { result: { dryRun: true } };
  const { ensureChrome, getPlayerPage } = await import('./cdp.mjs');
  const { playRemote } = await import('./inject.mjs');
  const { scrapeAfterPlay } = await import('./comprehension.mjs');
  const { proxify } = await import('./proxy.mjs');

  cancelCurrentWatch('selfcheck');
  await ensureChrome(cfg);
  const page = await getPlayerPage(cfg);
  const verify = await playRemote(page, {
    videoUrl: mediaUrl(msg.video_url, proxify),
    subUrl: mediaUrl(msg.sub_url || null, proxify),
    sub2Url: null,
    seekMs: null,
  });
  const scraped = await scrapeAfterPlay(page, { timeoutMs: 20000 });
  return { result: { verify, stats: scraped ? scraped.stats : null, extVersion: state.extVersion } };
}

async function dispatch(msg, send) {
  switch (msg.cmd) {
    case 'play': return handlePlay(msg, send);
    case 'sync-known': return handleSyncKnown(msg);
    case 'selfcheck': return handleSelfcheck(msg, send);
    default: throw new Error(`unknown command '${msg.cmd}'`);
  }
}

// ---- WebSocket lifecycle (dial out + reconnect) ----
let hb = null;
let knownSyncTimer = null;

async function connect(WebSocketImpl) {
  log(`connecting to ${cfg.serverUrl} (${cfg.dryRun ? 'DRY_RUN' : 'live'})`);
  const sock = new WebSocketImpl(cfg.wsUrl);

  const send = (obj) => { try { sock.send(JSON.stringify(obj)); } catch {} };

  // device identity in every register/heartbeat — this is what lets several
  // machines stay connected to the Lab at once (the server routes per device).
  const ident = { device_id: cfg.deviceId, device_name: cfg.deviceName };

  sock.addEventListener('open', async () => {
    await refreshState();
    send({ type: 'register', ...ident, chrome: state.chrome, migaku: state.migaku, version: cfg.version, ext_version: state.extVersion });
    log(`connected as ${cfg.deviceId} (${cfg.deviceName}) · chrome=${state.chrome} migaku=${state.migaku} ext=${state.extVersion || '?'}`);
    hb = setInterval(async () => {
      await refreshState();
      send({ type: 'heartbeat', ...ident, chrome: state.chrome, migaku: state.migaku, version: cfg.version, ext_version: state.extVersion });
    }, cfg.heartbeatMs);
    // periodic known-words push while connected: keeps comprehension current
    // without any manual action (words are mined/marked during watching).
    if (!cfg.dryRun && cfg.knownSyncMs > 0) {
      knownSyncTimer = setInterval(async () => {
        try {
          if (state.migaku) await handleSyncKnown();
        } catch (e) { warn('periodic known sync failed:', e && e.message ? e.message : e); }
      }, cfg.knownSyncMs);
    }
  });

  sock.addEventListener('message', async (ev) => {
    let msg;
    try { msg = JSON.parse(typeof ev.data === 'string' ? ev.data : ev.data.toString()); } catch { return; }
    if (!msg || msg.type !== 'command') return;
    try {
      const out = await dispatch(msg, send);
      send({ type: 'ack', id: msg.id, ok: true, ...out });
    } catch (e) {
      const error = e && e.message ? e.message : String(e);
      warn(`command '${msg.cmd}' failed:`, error);
      send({ type: 'ack', id: msg.id, ok: false, error });
    }
  });

  const reconnect = (why) => {
    if (hb) { clearInterval(hb); hb = null; }
    if (knownSyncTimer) { clearInterval(knownSyncTimer); knownSyncTimer = null; }
    log(`disconnected (${why}); reconnecting in ${cfg.reconnectMs}ms`);
    setTimeout(() => connect(WebSocketImpl), cfg.reconnectMs);
  };
  sock.addEventListener('close', (ev) => {
    if (ev && ev.code === 4401) warn('unauthorized — check MIMI_LAB_TOKEN matches the server');
    if (ev && ev.code === 4000) {
      warn(`another Connector registered with this device id (${cfg.deviceId}) and took over. ` +
           'If you run Connectors on several machines, give each its own id ' +
           '(delete ~/.mimi-lab-connector/device-id on the copy, or set CONNECTOR_DEVICE_ID).');
    }
    reconnect(`close ${ev && ev.code}`);
  });
  sock.addEventListener('error', (ev) => warn('ws error:', (ev && (ev.message || ev.error)) || 'error'));
}

// ---- boot ----
(async () => {
  log(`server=${cfg.serverUrl} cdp=${cfg.cdpUrl} token=${cfg.token ? 'set' : 'none'} v=${cfg.version} ` +
      `device=${cfg.deviceId} (${cfg.deviceName})`);
  if (!cfg.dryRun) {
    try {
      const { startMediaProxy } = await import('./proxy.mjs');
      await startMediaProxy(cfg);
      proxyUp = true;
      log(`media proxy on http://127.0.0.1:${cfg.proxyPort} (relays from ${cfg.serverUrl} so Migaku fetches over loopback — no TLS needed)`);
    } catch (e) {
      warn(`media proxy could not start on :${cfg.proxyPort}:`, e && e.message ? e.message : e);
      if (isLoopback(cfg.serverUrl)) {
        warn('server is loopback — playback will fetch it directly (no proxy needed)');
      } else {
        warn('PLAYBACK WILL FAIL until the port is free — set CONNECTOR_PROXY_PORT to another port');
      }
    }
    try {
      const { ensureChrome } = await import('./cdp.mjs');
      const up = await ensureChrome(cfg).catch(() => false);
      if (!up) warn(`Chrome CDP not reachable at ${cfg.cdpUrl} — will keep trying on each play. ` +
                    `Install Migaku (Early Access) + log in once in the dedicated profile.`);
    } catch (e) { warn('Chrome bootstrap skipped:', e && e.message ? e.message : e); }
  }
  const WebSocketImpl = await getWebSocket();
  connect(WebSocketImpl);
})();

process.on('unhandledRejection', (e) => warn('unhandledRejection:', e && e.message ? e.message : e));
process.on('SIGINT', () => { log('shutting down'); process.exit(0); });
process.on('SIGTERM', () => process.exit(0));
