// inject.mjs — the proven remote one-click play (generalized from
// harness/remote-inject-test.mjs), run as a privileged CDP Runtime.evaluate.
//
// The whole chain — fetch the server's media over HTTP → build File objects →
// inject into Migaku's <input type=file multiple> via DataTransfer → seek →
// play() — runs inside ONE Runtime.evaluate with `userGesture:true`. That
// gesture flag is why we use a raw CDP session instead of Playwright's
// page.evaluate (which has no gesture flag and left the PoC paused — design §9.1):
// with it, video.play() autoplays reliably. Migaku ingests the File, makes its
// own blob: URL, plays, and tokenizes — full immersion, no drag-and-drop.
//
// Memory: the video is streamed into 32 MB Blob segments (Chrome disk-backs
// Blob storage) instead of one giant arrayBuffer, so large episodes do not
// materialize entirely in renderer heap. Streaming also gives us download
// progress, reported out through console messages → onProgress → the WS.

const PROGRESS_TAG = '[mgklab-progress]';

/**
 * Inject + play media into the Migaku Player page.
 * @param page  Playwright Page on the EA player origin
 * @param videoUrl  absolute, token-bearing URL to the episode video
 * @param subUrl    absolute URL to the Japanese (study) subtitle (or null)
 * @param sub2Url   absolute URL to the English (secondary/reference) subtitle (or null)
 * @param seekMs    optional start offset in ms
 * @param onProgress (pct, phase) => void — download progress callback
 * @returns verify info {ok, set, hasVideo, paused, currentTime, readyState, srcKind, migakuTokens}
 */
export async function playRemote(page, { videoUrl, subUrl = null, sub2Url = null, seekMs = null, onProgress = null }) {
  const client = await page.context().newCDPSession(page);
  const ARGS = JSON.stringify({ videoUrl, subUrl, sub2Url, seekMs, TAG: PROGRESS_TAG });
  const expression = `(async () => {
    const A = ${ARGS};
    const report = (pct, phase) => { try { console.debug(A.TAG, JSON.stringify({ pct, phase })); } catch (e) {} };
    // Stream into 32MB Blob segments: Chrome pages Blob data to disk, so a
    // multi-GB episode never sits in JS heap (arrayBuffer() did).
    const fetchFile = async (url, fallbackName, type, phase) => {
      const r = await fetch(url);
      if (!r.ok) throw new Error('fetch ' + url.split('?')[0] + ' -> ' + r.status);
      let name = fallbackName;
      const cd = r.headers.get('content-disposition');
      const mm = cd && cd.match(/filename="?([^"]+)"?/);
      if (mm) name = mm[1];
      const total = parseInt(r.headers.get('content-length') || '0', 10) || null;
      const parts = [];
      let chunk = [];
      let chunkBytes = 0;
      let got = 0;
      let lastPct = -1;
      const reader = r.body.getReader();
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        chunk.push(value);
        chunkBytes += value.byteLength;
        got += value.byteLength;
        if (chunkBytes >= 32 * 1024 * 1024) {
          parts.push(new Blob(chunk));
          chunk = []; chunkBytes = 0;
        }
        if (total && phase) {
          const pct = Math.floor((got / total) * 100);
          if (pct !== lastPct && (pct % 5 === 0 || pct === 100)) { lastPct = pct; report(pct, phase); }
        }
      }
      if (chunk.length) parts.push(new Blob(chunk));
      return new File(parts, name, { type });
    };
    const findInput = (root) => {
      const d = root.querySelector('input[type=file][multiple]'); if (d) return d;
      for (const el of root.querySelectorAll('*')) { if (el.shadowRoot) { const x = findInput(el.shadowRoot); if (x) return x; } }
      return null;
    };
    const findVideo = (root) => {
      const v = root.querySelector('video'); if (v) return v;
      for (const el of root.querySelectorAll('*')) { if (el.shadowRoot) { const x = findVideo(el.shadowRoot); if (x) return x; } }
      return null;
    };
    const waitFor = async (fn, timeoutMs, stepMs) => {
      const t0 = Date.now();
      for (;;) {
        const x = fn();
        if (x) return x;
        if (Date.now() - t0 > timeoutMs) return null;
        await new Promise((r) => setTimeout(r, stepMs));
      }
    };

    // The Player may still be booting (fresh tab): poll for the input instead
    // of failing instantly (the old fixed 1500ms settle was a guess).
    const input = await waitFor(() => findInput(document), 10000, 250);
    if (!input) return { ok: false, err: 'no input[type=file][multiple] found in Migaku Player (page not ready or UI changed)' };

    const files = [await fetchFile(A.videoUrl, 'episode.mp4', 'video/mp4', 'video')];
    if (A.subUrl) files.push(await fetchFile(A.subUrl, 'episode.ja.srt', 'text/plain', null));
    // English secondary track: Migaku ingests it as a second localSubItem and
    // (with "secondary subtitles" enabled in its settings) shows it beneath the
    // Japanese study line. The .en. filename helps Migaku detect its language.
    if (A.sub2Url) files.push(await fetchFile(A.sub2Url, 'episode.en.srt', 'text/plain', null));
    report(100, 'loading');

    const dt = new DataTransfer();
    for (const f of files) dt.items.add(f);
    input.files = dt.files;
    input.dispatchEvent(new Event('change', { bubbles: true }));
    input.dispatchEvent(new Event('input', { bubbles: true }));

    // wait for Migaku to ingest (video element appears with a source) instead
    // of a blind 1400ms sleep — slow first boots used to race it.
    let v = await waitFor(() => {
      const x = findVideo(document);
      return (x && (x.currentSrc || x.src)) ? x : null;
    }, 15000, 250);
    if (!v) v = findVideo(document);
    if (v && A.seekMs != null) { try { v.currentTime = A.seekMs / 1000; } catch (e) {} }
    if (v) { try { await v.play(); } catch (e) {} }
    let tokens = 0;
    const walk = (r) => { r.querySelectorAll('*').forEach((e) => { try { if (e.matches && e.matches('.migaku-token[data-mgk-known-status]')) tokens++; } catch (e2) {} if (e.shadowRoot) walk(e.shadowRoot); }); };
    walk(document);
    return {
      ok: true,
      set: files.map((f) => f.name + ' (' + f.size + 'B)'),
      hasVideo: !!v,
      paused: v ? v.paused : null,
      currentTime: v ? +v.currentTime.toFixed(2) : null,
      duration: v ? (Number.isFinite(v.duration) ? +v.duration.toFixed(2) : null) : null,
      readyState: v ? v.readyState : null,
      srcKind: v ? (v.currentSrc || v.src || '').slice(0, 12) : null,
      migakuTokens: tokens,
    };
  })()`;

  let progressHandler = null;
  try {
    if (onProgress) {
      await client.send('Runtime.enable');
      progressHandler = (ev) => {
        try {
          const args = ev.args || [];
          if (args.length >= 2 && args[0].value === PROGRESS_TAG) {
            const { pct, phase } = JSON.parse(args[1].value);
            onProgress(pct, phase);
          }
        } catch {}
      };
      client.on('Runtime.consoleAPICalled', progressHandler);
    }

    const { result, exceptionDetails } = await client.send('Runtime.evaluate', {
      expression,
      awaitPromise: true,
      returnByValue: true,
      userGesture: true,
    });
    if (exceptionDetails) {
      const d = exceptionDetails.exception && (exceptionDetails.exception.description || exceptionDetails.exception.value);
      throw new Error('inject eval threw: ' + (d || exceptionDetails.text || 'unknown'));
    }
    const out = result && result.value;
    if (!out || !out.ok) throw new Error('inject failed: ' + ((out && out.err) || 'unknown'));
    return out;
  } finally {
    // the CDP session used to leak when the eval threw
    try { if (progressHandler) client.off('Runtime.consoleAPICalled', progressHandler); } catch {}
    try { await client.detach(); } catch {}
  }
}
