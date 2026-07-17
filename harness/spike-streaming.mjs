// spike-streaming.mjs — P3 spike from notes/REMOTE_PLAYBACK_DESIGN.md §5:
// can Migaku attach to a streaming <video src=range-URL> instead of an
// injected File (instant start, no full download)?
//
// Mechanism under test (from the RE notes + bundle reading):
//   * the adapter polls document.querySelector(videoElementSelector) for
//     `data-mgk-has-video-init="true"` and adopts that element;
//   * the page<->adapter bridge is CustomEvent("MigakuVideoInterfaceSend",
//     {detail:{msgId, msgKey, payload}}) answered on
//     "MigakuVideoInterfaceResponse" with a matching msgId.
//
// Usage: node spike-streaming.mjs <videoUrl> <subUrl>
import { chromium } from 'playwright-core';

const CDP = process.env.CDP_URL || 'http://127.0.0.1:9222';
const EXT = process.env.MIGAKU_EXT_ID || 'dmeppfcidcpcocleneopiblmpnbokhep';
const [videoUrl, subUrl] = process.argv.slice(2);
if (!videoUrl) { console.error('usage: node spike-streaming.mjs <videoUrl> [subUrl]'); process.exit(2); }

const browser = await chromium.connectOverCDP(CDP);
const ctx = browser.contexts()[0];
const page = await ctx.newPage();
try {
  await page.goto(`chrome-extension://${EXT}/pages/player/index.html`, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(1500);

const subText = subUrl ? await (await fetch(subUrl)).text() : null;

const result = await page.evaluate(async ({ videoUrl, subText }) => {
  const out = { steps: [] };
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));

  // bridge helper: send + await response (2s timeout)
  const bridge = (msgKey, payload) => new Promise((resolve) => {
    const msgId = 'spike-' + Math.random().toString(36).slice(2);
    const to = setTimeout(() => { document.removeEventListener('MigakuVideoInterfaceResponse', onResp); resolve({ timeout: true }); }, 2000);
    const onResp = (ev) => {
      const d = ev && ev.detail;
      if (d && d.msgId === msgId) {
        clearTimeout(to);
        document.removeEventListener('MigakuVideoInterfaceResponse', onResp);
        resolve({ ok: true, payload: d.payload });
      }
    };
    document.addEventListener('MigakuVideoInterfaceResponse', onResp);
    document.dispatchEvent(new CustomEvent('MigakuVideoInterfaceSend', { detail: { msgId, msgKey, payload } }));
  });

  // 0. is anything even listening on the bridge in this page?
  out.probe_isLocalPlayerMode = await bridge('isLocalPlayerMode', {});
  out.probe_getVideoData = await bridge('mgk--GetVideoData', {});

  // 1. plant a streaming video element with the adoption attribute
  const v = document.createElement('video');
  v.setAttribute('data-mgk-has-video-init', 'true');
  v.src = videoUrl;
  v.controls = true;
  v.style.cssText = 'position:fixed;bottom:8px;right:8px;width:480px;z-index:99999;background:#000';
  document.body.appendChild(v);
  out.steps.push('video element planted');
  try { await v.play(); out.playStarted = true; } catch (e) { out.playStarted = false; out.playErr = String(e && e.message); }
  await sleep(3000);
  out.videoState = { currentTime: v.currentTime, readyState: v.readyState, paused: v.paused, duration: v.duration };

  // 2. try pushing subtitles over the bridge (several plausible payload shapes)
  const shapes = subText ? [
    { name: 'array-of-files', payload: [{ fileName: 'TestShow.ja.srt', name: 'TestShow.ja.srt', content: subText, language: 'ja' }] },
    { name: 'object-files', payload: { files: [{ fileName: 'TestShow.ja.srt', content: subText }] } },
    { name: 'single', payload: { fileName: 'TestShow.ja.srt', content: subText, language: 'ja' } },
  ] : [];
  out.subAttempts = [];
  for (const s of shapes) {
    const r = await bridge('loadLocalSubtitles', s.payload);
    out.subAttempts.push({ shape: s.name, resp: r });
    if (r && r.ok) break;
  }

  await sleep(4000);
  // 3. did Migaku attach? tokens rendered anywhere / overlay near our video?
  let tokens = 0;
  const walk = (r) => { r.querySelectorAll('*').forEach((e) => { try { if (e.matches && e.matches('.migaku-token[data-mgk-known-status]')) tokens++; } catch {} if (e.shadowRoot) walk(e.shadowRoot); }); };
  walk(document);
  out.migakuTokens = tokens;
  out.overlayPresent = !!document.querySelector('[class*="SubtitleOverlay"]');
  out.adapterAdopted = v.hasAttribute('data-mgk-adopted') || out.overlayPresent;

  // cleanup our element
  try { v.pause(); v.remove(); } catch {}
  return out;
}, { videoUrl, subText });

console.log(JSON.stringify(result, null, 2));
} finally {
  try { await page.close(); } catch {}
  await browser.close();
}
