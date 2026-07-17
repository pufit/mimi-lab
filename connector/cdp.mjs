// cdp.mjs — external-CDP link to the user's dedicated Chrome+Migaku, plus a
// best-effort Chrome lifecycle (launch the dedicated profile if its debug port
// isn't up). Ported from the old server-side companion/cdp.mjs.
//
// Chrome ≥136 refuses a remote-debugging port on the *default* profile, so we
// always use a dedicated --user-data-dir (notes/REMOTE_PLAYBACK_DESIGN.md §9.5).
// browser.close() over CDP only disconnects the client — the real Chrome keeps
// running — so we never call it during normal operation.
import { spawn } from 'node:child_process';
import { chromium } from 'playwright-core';

let _browser = null;

function isAlive(b) {
  try { return !!b && b.isConnected(); } catch { return false; }
}

async function cdpUp(cdpUrl) {
  try {
    const r = await fetch(new URL('/json/version', cdpUrl), { signal: AbortSignal.timeout(1500) });
    return r.ok;
  } catch { return false; }
}

/** Launch the dedicated Chrome profile with the debug port + Migaku player open. */
function launchChrome(cfg) {
  const args = [
    `--user-data-dir=${cfg.userDataDir}`,
    `--remote-debugging-port=${cfg.debugPort}`,
    '--remote-debugging-address=127.0.0.1',
    '--no-first-run',
    '--no-default-browser-check',
    '--restore-last-session=false',
    cfg.playerUrl,
  ];
  const child = spawn(cfg.chromeBin, args, { detached: true, stdio: 'ignore' });
  child.unref();
}

/**
 * Ensure Chrome's CDP endpoint is reachable. If not and launching is enabled,
 * spawn the dedicated profile and wait for the port to come up.
 * Returns true if CDP is up, false if it never came up (caller degrades).
 */
export async function ensureChrome(cfg, { timeoutMs = 25000 } = {}) {
  if (await cdpUp(cfg.cdpUrl)) return true;
  if (!cfg.launchChrome) return false;
  try {
    console.log(`[connector] launching dedicated Chrome (${cfg.chromeBin}) on :${cfg.debugPort}`);
    launchChrome(cfg);
  } catch (e) {
    console.error('[connector] could not launch Chrome:', e && e.message ? e.message : e);
    return false;
  }
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await cdpUp(cfg.cdpUrl)) return true;
    await new Promise((r) => setTimeout(r, 500));
  }
  return false;
}

export async function getBrowser(cfg) {
  if (isAlive(_browser)) return _browser;
  _browser = await chromium.connectOverCDP(cfg.cdpUrl);
  _browser.on('disconnected', () => { _browser = null; });
  return _browser;
}

export async function getContext(cfg) {
  const b = await getBrowser(cfg);
  const ctxs = b.contexts();
  if (!ctxs.length) throw new Error('CDP connected but no browser context present');
  return ctxs[0];
}

export async function findEAPage(cfg, { playerOnly = false } = {}) {
  const ctx = await getContext(cfg);
  return ctx.pages().find((p) => {
    try {
      const u = p.url();
      return u.includes(cfg.extId) && (!playerOnly || u.includes('/pages/player/'));
    } catch { return false; }
  }) || null;
}

/** Get (or open) any EA extension page — used for IndexedDB reads (known-words). */
export async function getEAPage(cfg, { playerOnly = false } = {}) {
  let page = await findEAPage(cfg, { playerOnly });
  if (page) return page;
  const ctx = await getContext(cfg);
  page = await ctx.newPage();
  try {
    await page.goto(cfg.playerUrl, { waitUntil: 'domcontentloaded' });
  } catch (e) {
    // navigation to the extension URL fails when Migaku isn't installed —
    // close the page instead of leaking an error tab (the heartbeat probe
    // used to open a fresh one every 15s)
    try { await page.close(); } catch {}
    throw e;
  }
  // brief settle for the Vue app to mount; inject.mjs polls for the actual
  // input, so this doesn't need to be exact
  await page.waitForTimeout(800);
  return page;
}

/** Get (or open) the EA *player* page specifically — used for play + comprehension. */
export async function getPlayerPage(cfg) {
  return getEAPage(cfg, { playerOnly: true });
}

/** Reachability probe for the heartbeat: {chrome, migaku, extVersion}.
 * Never throws, and never leaks tabs — a failed navigation used to strand an
 * error-page tab that didn't match the extId, so EVERY 15s heartbeat opened
 * another one. extVersion (chrome.runtime.getManifest) lets the server pin /
 * notify on Migaku extension updates. */
export async function probe(cfg) {
  let chrome = false;
  let migaku = false;
  let extVersion = null;

  const readVersion = async (p) => {
    try {
      return await p.evaluate(() => {
        try { return chrome.runtime && chrome.runtime.getManifest ? chrome.runtime.getManifest().version : null; }
        catch { return null; }
      });
    } catch { return null; }
  };

  try {
    const ctx = await getContext(cfg);
    chrome = true;
    const page = ctx.pages().find((p) => { try { return p.url().includes(cfg.extId); } catch { return false; } });
    if (page) {
      migaku = true;
      extVersion = await readVersion(page);
    } else {
      let p = null;
      try {
        p = await ctx.newPage();
        await p.goto(cfg.playerUrl, { waitUntil: 'domcontentloaded', timeout: 8000 });
        const title = await p.title();
        migaku = /migaku/i.test(title) || p.url().includes(cfg.extId);
        if (migaku) {
          extVersion = await readVersion(p);
        } else {
          try { await p.close(); } catch {}
        }
      } catch {
        migaku = false;
        if (p) { try { await p.close(); } catch {} }
      }
    }
  } catch { chrome = false; }
  return { chrome, migaku, extVersion };
}
