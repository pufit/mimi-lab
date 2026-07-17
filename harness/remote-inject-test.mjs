// remote-inject-test.mjs — PROOF of the new remote design's core mechanism:
// fetch media from an HTTP server (as a remote browser would) -> build File objects
// in the Migaku Player page -> inject into the <input type=file multiple> via DataTransfer
// -> confirm Migaku ingests, plays, and tokenizes. This is exactly what a user-installed
// connector extension would do via chrome.debugger Runtime.evaluate (same privileged eval).
import { chromium } from 'playwright-core';

const CDP = process.env.CDP_URL || 'http://127.0.0.1:9222';
const EA = process.env.MIGAKU_EXT_ID || 'dmeppfcidcpcocleneopiblmpnbokhep';
const PLAYER = `chrome-extension://${EA}/pages/player/index.html`;
const BASE = process.env.MEDIA_BASE || 'http://127.0.0.1:8899';
const VID = process.env.VID || 'TestShow - S01E01.mp4';
const SUB = process.env.SUB || 'TestShow - S01E01.ja.srt';

const b = await chromium.connectOverCDP(CDP);
const ctx = b.contexts()[0];
const page = await ctx.newPage();
try {
await page.goto(PLAYER, { waitUntil: 'domcontentloaded' });
await page.waitForTimeout(1500);
try { await page.bringToFront(); } catch {}

const inject = await page.evaluate(async ({ base, vid, sub }) => {
  const fetchFile = async (name, type) => {
    const r = await fetch(`${base}/${encodeURIComponent(name)}`);
    if (!r.ok) throw new Error(`fetch ${name} -> ${r.status}`);
    const buf = await r.arrayBuffer();
    return new File([buf], name, { type });
  };
  let files;
  try { files = [await fetchFile(vid, 'video/mp4'), await fetchFile(sub, 'text/plain')]; }
  catch (e) { return { ok: false, err: String(e) }; }
  const findInput = (root) => {
    const d = root.querySelector('input[type=file][multiple]'); if (d) return d;
    for (const el of root.querySelectorAll('*')) { if (el.shadowRoot) { const x = findInput(el.shadowRoot); if (x) return x; } }
    return null;
  };
  const input = findInput(document);
  if (!input) return { ok: false, err: 'no input[type=file][multiple] found' };
  const dt = new DataTransfer();
  for (const f of files) dt.items.add(f);
  input.files = dt.files;
  input.dispatchEvent(new Event('change', { bubbles: true }));
  input.dispatchEvent(new Event('input', { bubbles: true }));
  return { ok: true, set: files.map((f) => `${f.name} (${f.size}B)`) };
}, { base: BASE, vid: VID, sub: SUB });
console.log('INJECT:', JSON.stringify(inject));

await page.waitForTimeout(7000);
const verify = await page.evaluate(() => {
  const findVideo = (root) => { const v = root.querySelector('video'); if (v) return v; for (const el of root.querySelectorAll('*')) { if (el.shadowRoot) { const x = findVideo(el.shadowRoot); if (x) return x; } } return null; };
  const v = findVideo(document);
  let tokens = 0;
  const walk = (r) => { r.querySelectorAll('*').forEach((e) => { try { if (e.matches && e.matches('.migaku-token[data-mgk-known-status]')) tokens++; } catch {} if (e.shadowRoot) walk(e.shadowRoot); }); };
  walk(document);
  return {
    hasVideo: !!v,
    currentTime: v ? +v.currentTime.toFixed(2) : null,
    duration: v ? (Number.isFinite(v.duration) ? +v.duration.toFixed(2) : null) : null,
    paused: v ? v.paused : null,
    readyState: v ? v.readyState : null,
    srcKind: v ? (v.currentSrc || v.src || '').slice(0, 12) : null,
    migakuTokens: tokens,
  };
});
console.log('VERIFY:', JSON.stringify(verify));
} finally {
  try { await page.close(); } catch {}
  await b.close(); // CDP: disconnects client only; real Chrome keeps running
}
