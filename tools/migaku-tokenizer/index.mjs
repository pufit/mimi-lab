// Public API for the pure-Node Migaku tokenizer port.
//
// Runs Migaku's own bundled Kotlin/JS analyzer (the real Kuromoji + models +
// dictForm pipeline) entirely in Node — no browser, no network — by loading the
// extension bundle with browser/storage shims and calling its in-process Core
// over JSON-RPC. Produces Migaku-EXACT tokens (surface, dictForm, reading, pos,
// pitch), which is what makes comprehension match Migaku's own value.
//
// CLI:  node index.mjs "日本語のテストです。"
import { loadCore } from './core.mjs';

export { loadCore };

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

/** Tokenize a single line. Returns [{surface, dictForm, reading, pos, pitches}]. */
export async function tokenizeLine(text, lang = 'ja') {
  const c = await loadCore();
  // NB: call per-line — the many-texts batch path routes through a Worker that
  // isn't shimmed; single-text runs fully in-process.
  const r = await c.call('ParsingMgr.parseTokensMany', { lang, texts: [text || ''] });
  return ((r && r[0]) || []).map(simplify);
}

/** Tokenize many lines (sequential, in-process). Returns array of token arrays. */
export async function tokenizeLines(texts, lang = 'ja') {
  const c = await loadCore();
  const out = [];
  for (const text of texts) {
    const r = await c.call('ParsingMgr.parseTokensMany', { lang, texts: [text || ''] });
    out.push(((r && r[0]) || []).map(simplify));
  }
  return out;
}

if (import.meta.url === `file://${process.argv[1]}`) {
  const text = process.argv[2] || '日本語のテストです。これは簡単な文章です。';
  const toks = await tokenizeLine(text);
  console.log(JSON.stringify(toks, null, 2));
  process.exit(0);
}
