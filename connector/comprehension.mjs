// comprehension.mjs — scrape Migaku-native comprehension from the Player after an
// episode is loaded (ported from the old companion/player.mjs scrape path).
// Selectors are pinned to the semantic data-mgk-* attrs; CSS class names are hints.

/** Wait until the SubtitleBrowser has rendered status-tagged tokens (parser ran). */
async function waitForParse(page, { timeoutMs = 30000, cancelToken = null } = {}) {
  const deadline = Date.now() + timeoutMs;
  let last = 0;
  while (Date.now() < deadline) {
    if (cancelToken && cancelToken.cancelled) return 0;
    const n = await page.evaluate(() => {
      const all = [];
      const walk = (r) => { r.querySelectorAll('*').forEach((e) => { all.push(e); if (e.shadowRoot) walk(e.shadowRoot); }); };
      walk(document);
      const clsOf = (e) => (typeof e.className === 'string' ? e.className : (e.getAttribute && e.getAttribute('class')) || '');
      const items = all.filter((e) => /SubtitleBrowser__list__item(\s|$)/.test(clsOf(e)));
      let parsed = 0;
      for (const it of items) if (it.querySelector('.migaku-token[data-mgk-known-status]')) parsed++;
      return parsed;
    });
    if (n > 0 && n === last) return n;
    last = n;
    await page.waitForTimeout(700);
  }
  return last;
}

/** Read the ComprehensionStats panel + per-line tokens from the current Player page. */
export async function scrapeComprehension(page) {
  return page.evaluate(() => {
    const allEls = [];
    const walk = (r) => { r.querySelectorAll('*').forEach((e) => { allEls.push(e); if (e.shadowRoot) walk(e.shadowRoot); }); };
    walk(document);
    const clsOf = (e) => (typeof e.className === 'string' ? e.className : (e.getAttribute && e.getAttribute('class')) || '');

    const stats = { pct: null, rating: null, known: null, unknown: null, ignored: null, learning: null };
    const ratingEl = allEls.find((e) => /ComprehensionStats__statusLabel/.test(clsOf(e)) && e.getAttribute('page-rating'));
    if (ratingEl) stats.rating = ratingEl.getAttribute('page-rating');
    const numbers = allEls
      .filter((e) => /ComprehensionStatsUpperList__number/.test(clsOf(e)))
      .map((e) => (e.textContent || '').trim());
    if (numbers[0] != null) {
      const m = String(numbers[0]).match(/-?\d+/);
      if (m) stats.pct = +m[0];
    }
    allEls.filter((e) => /ComprehensionStatsChart__infoItem(\s|$)/.test(clsOf(e))).forEach((it) => {
      const t = (it.textContent || '').replace(/\s+/g, ' ').trim();
      const m = t.match(/(Known|Unknown|Ignored|Learning)\D*(\d+)/i);
      if (m) stats[m[1].toLowerCase()] = +m[2];
    });

    const browserItems = allEls.filter((e) => /SubtitleBrowser__list__item(\s|$)/.test(clsOf(e)));
    const lines = browserItems.map((it, idx) => {
      const toks = [...it.querySelectorAll('.migaku-token[data-mgk-known-status]')]
        .map((t) => ({
          term: t.getAttribute('data-mgk-term'),
          reading: t.getAttribute('data-mgk-secondary'),
          status: t.getAttribute('data-mgk-known-status'),
        }))
        .filter((t) => t.term);
      return { idx, text: (it.textContent || '').replace(/\s+/g, ' ').trim(), tokens: toks };
    });

    // backfill counts from unique rendered tokens when the panel was absent/partial
    const uniq = new Map();
    for (const ln of lines) for (const tk of ln.tokens) {
      const k = tk.term + '|' + tk.reading;
      if (!uniq.has(k)) uniq.set(k, tk.status);
    }
    const tally = {};
    for (const st of uniq.values()) if (st) tally[st] = (tally[st] || 0) + 1;
    for (const [key, st] of [['known', 'KNOWN'], ['unknown', 'UNKNOWN'], ['ignored', 'IGNORED'], ['learning', 'LEARNING']]) {
      if (stats[key] == null) stats[key] = tally[st] || 0;
    }

    const tokenCount = lines.reduce((a, l) => a + l.tokens.length, 0);
    return { stats, lines, lineCount: lines.length, tokenCount };
  });
}

/**
 * After a play(), wait for Migaku to parse and return its stats.
 * Returns { stats, lineCount, tokenCount } or null if nothing parsed in time.
 * `cancelToken.cancelled` (set when a new play supersedes this one) aborts the
 * scrape — play A's scraper must never read play B's stats.
 */
export async function scrapeAfterPlay(page, { timeoutMs = 30000, cancelToken = null } = {}) {
  const parsed = await waitForParse(page, { timeoutMs, cancelToken });
  if (!parsed || (cancelToken && cancelToken.cancelled)) return null;
  await page.waitForTimeout(800); // let the panel settle
  if (cancelToken && cancelToken.cancelled) return null;
  return scrapeComprehension(page);
}
