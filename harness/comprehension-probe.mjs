// comprehension-probe.mjs — DELIVERABLE: extract Migaku-native comprehension for the loaded subtitle.
// READ-ONLY. Reads (a) the ComprehensionStats UI values (general comprehension %, rating, unique
// Known/Unknown/Ignored counts, recommended sentences) and (b) EVERY rendered migaku-token with its
// data-mgk-* attributes (term/reading/known-status/freq). Then cross-checks token counts.
//
// Assumes a subtitle is already loaded in the EA player. To load the TestShow clip first, run load2.mjs.
import { chromium } from 'playwright-core';
import { requireTestShowPage, UNSAFE_DEBUG } from './safety.mjs';
const EA = process.env.MIGAKU_EXT_ID || 'dmeppfcidcpcocleneopiblmpnbokhep';
const b = await chromium.connectOverCDP(process.env.CDP_URL || 'http://localhost:9222');
const ctx = b.contexts()[0];
const { page } = await requireTestShowPage(ctx, EA);
console.log('on guarded TestShow fixture');

const data = await page.evaluate(() => {
  const allEls = [];
  const walk = (r) => { r.querySelectorAll('*').forEach(e => { allEls.push(e); if (e.shadowRoot) walk(e.shadowRoot); }); };
  walk(document);
  const clsOf = (e) => (typeof e.className === 'string' ? e.className : (e.getAttribute && e.getAttribute('class')) || '');
  const txt = (sel, root = document) => { const m = [...root.querySelectorAll(sel)]; return m.map(e => (e.textContent || '').replace(/\s+/g, ' ').trim()); };

  // ---- (a) ComprehensionStats UI scrape ----
  const stats = { rating: null, generalComprehensionPct: null, recommendedSentences: null, avgFrequencyStars: null, uniqueCounts: {} };
  const ratingEl = allEls.find(e => /ComprehensionStats__statusLabel/.test(clsOf(e)) && e.getAttribute('page-rating'));
  if (ratingEl) stats.rating = ratingEl.getAttribute('page-rating');
  // general comprehension number (first ComprehensionStatsUpperList__number after "General Comprehension")
  const numbers = allEls.filter(e => /ComprehensionStatsUpperList__number/.test(clsOf(e))).map(e => (e.textContent || '').trim());
  if (numbers[0]) stats.generalComprehensionPct = numbers[0];
  if (numbers[1]) stats.recommendedSentences = numbers[1];
  const freqTag = allEls.find(e => /ComprehensionStatsUpperList__frequencyTag/.test(clsOf(e)));
  if (freqTag) stats.avgFrequencyStars = (freqTag.getAttribute('title') || (freqTag.textContent || '').trim());
  // unique Known/Unknown/Ignored from the chart info items
  allEls.filter(e => /ComprehensionStatsChart__infoItem(\s|$)/.test(clsOf(e))).forEach(it => {
    const t = (it.textContent || '').replace(/\s+/g, ' ').trim(); // e.g. "Known 15 words"
    const m = t.match(/(Known|Unknown|Ignored)\D*(\d+)/i);
    if (m) stats.uniqueCounts[m[1].toLowerCase()] = +m[2];
  });

  // ---- (b) every migaku-token ----
  const tokenEls = allEls.filter(e => /migaku-token/.test(clsOf(e)));
  const tokens = tokenEls.map(t => ({
    term: t.getAttribute('data-mgk-term'),
    secondary: t.getAttribute('data-mgk-secondary'),
    surfaceReading: t.getAttribute('data-mgk-surface-reading'),
    status: t.getAttribute('data-mgk-known-status'),
    freqStars: t.getAttribute('data-mgk-freq-stars'),
    pitch: t.getAttribute('data-mgk-pitch'),
    text: (t.textContent || '').replace(/\s+/g, '').slice(0, 20),
  }));
  // tally by status over rendered tokens (note: tokens repeat per sentence-group render)
  const tally = {};
  for (const tk of tokens) if (tk.status) tally[tk.status] = (tally[tk.status] || 0) + 1;
  // unique by term+secondary
  const uniq = new Map();
  for (const tk of tokens) { const k = tk.term + '|' + tk.secondary; if (!uniq.has(k)) uniq.set(k, tk.status); }
  const uniqTally = {};
  for (const st of uniq.values()) if (st) uniqTally[st] = (uniqTally[st] || 0) + 1;

  // ---- AUTHORITATIVE per-line source: the SubtitleBrowser list holds EVERY subtitle line, fully
  // parsed with per-token known-status (the on-video SubtitleOverlay only has the current line; the
  // UiDictEntry popup contains duplicates — so scope whole-episode comprehension to SubtitleBrowser). ----
  const browserItems = allEls.filter(e => /SubtitleBrowser__list__item(\s|$)/.test(clsOf(e)));
  const lines = browserItems.map(it => {
    const toks = [...it.querySelectorAll('.migaku-token')]
      .map(t => ({ term: t.getAttribute('data-mgk-term'), reading: t.getAttribute('data-mgk-secondary'), status: t.getAttribute('data-mgk-known-status'), freq: t.getAttribute('data-mgk-freq-stars') }))
      .filter(t => t.term);
    const c = (st) => toks.filter(t => t.status === st).length;
    return { text: (it.textContent || '').replace(/\s+/g, '').slice(0, 60), tokenCount: toks.length, known: c('KNOWN'), unknown: c('UNKNOWN'), ignored: c('IGNORED'), learning: c('LEARNING'), tokens: toks };
  });

  // also the on-video overlay (current line) for reference
  const overlayEls = allEls.filter(e => /SubtitleOverlay__targetSubs__line/.test(clsOf(e)));
  const overlay = overlayEls.map(s => (s.textContent || '').replace(/\s+/g, '').slice(0, 60));

  return { stats, tokenCount: tokens.length, statusTallyAllRendered: tally, uniqueTermCount: uniq.size, uniqueStatusTally: uniqTally, subtitleBrowserLineCount: browserItems.length, lines, overlay, tokensSample: tokens.slice(0, 40) };
});

console.log('\n=== Migaku ComprehensionStats (native) ===');
console.log(UNSAFE_DEBUG ? JSON.stringify(data.stats, null, 2) : JSON.stringify({ fieldsPresent: Object.keys(data.stats).filter((key) => data.stats[key] !== null) }));
console.log('\n=== token harvest ===');
console.log('rendered token count:', data.tokenCount, '| rendered status tally:', JSON.stringify(data.statusTallyAllRendered));
console.log('unique term count:', data.uniqueTermCount, '| unique status tally:', JSON.stringify(data.uniqueStatusTally));
console.log('SubtitleBrowser lines (authoritative whole-episode source):', data.subtitleBrowserLineCount);
console.log('on-video overlay count:', data.overlay.length);
console.log('\n=== per-line comprehension (from SubtitleBrowser) ===');
if (UNSAFE_DEBUG) {
  for (const ln of data.lines) console.log(`  K=${ln.known} U=${ln.unknown} I=${ln.ignored} L=${ln.learning}  ${ln.text}`);
} else {
  console.log(JSON.stringify({ lineCount: data.lines.length, tokenCounts: data.lines.map((line) => line.tokenCount) }));
}
console.log('\n=== full per-line tokens ===');
console.log(UNSAFE_DEBUG ? JSON.stringify(data.lines, null, 1) : '(suppressed; set MIGAKU_UNSAFE_DEBUG=1 to opt in)');
await b.close();
