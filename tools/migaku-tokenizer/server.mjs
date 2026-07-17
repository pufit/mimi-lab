// Long-lived tokenizer sidecar: loads Migaku's analyzer once, then serves
// tokenization over localhost HTTP. The Python learn pipeline calls this instead
// of spawning a process per episode and repeatedly paying initialization cost.
//
//   GET  /health                      -> {ok, ext, lang}
//   POST /tokenize {lines:[...],lang} -> {tokens: [[{surface,dictForm,reading,pos,pitches}],...]}
import http from 'node:http';
import { loadCore } from './core.mjs';

const PORT = Number(process.env.MIGAKU_TOK_PORT || 8788);
const HOST = process.env.MIGAKU_TOK_HOST || '127.0.0.1';

const c = await loadCore();
// warm the parser (first parse loads the dictionary)
await c.call('ParsingMgr.parseTokensMany', { lang: 'ja', texts: ['ウォームアップ'] });
console.log(`[migaku-tokenizer] core ready (ext ${c.ext.version})`);

function simplify(tok) {
  const t0 = tok.terms && tok.terms[0];
  return {
    surface: tok.surface,
    dictForm: t0 ? t0.term : tok.surface,
    reading: t0 ? t0.reading : '',
    pos: t0 ? t0.pos : '',
    pitches: t0 ? t0.pitches : [],
    unrecognized: !!tok.unrecognized,
  };
}

async function tokenizeLines(lines, lang) {
  const out = [];
  for (const line of lines) {
    const r = await c.call('ParsingMgr.parseTokensMany', { lang, texts: [line || ''] });
    out.push(((r && r[0]) || []).map(simplify));
  }
  return out;
}

const server = http.createServer(async (req, res) => {
  const json = (code, obj) => { res.writeHead(code, { 'content-type': 'application/json' }); res.end(JSON.stringify(obj)); };
  try {
    if (req.method === 'GET' && req.url === '/health') return json(200, { ok: true, ext: c.ext.version, lang: 'ja' });
    if (req.method === 'POST' && req.url === '/tokenize') {
      let body = '';
      for await (const chunk of req) body += chunk;
      const payload = JSON.parse(body || '{}');
      const lang = payload.lang || 'ja';
      const lines = Array.isArray(payload.lines) ? payload.lines : [];
      const t0 = performance.now();
      const tokens = await tokenizeLines(lines, lang);
      return json(200, { tokens, lines: lines.length, ms: Math.round(performance.now() - t0) });
    }
    json(404, { error: 'not found' });
  } catch (e) {
    json(500, { error: String((e && e.message) || e) });
  }
});

server.listen(PORT, HOST, () => console.log(`[migaku-tokenizer] listening on http://${HOST}:${PORT}`));
