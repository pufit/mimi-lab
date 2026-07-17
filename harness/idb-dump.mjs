// idb-dump.mjs — Goal 2 (the holy grail of known-words): pull srs/data gzipped SQLite blob out of the
// EA extension origin via CDP, inflate (zlib), parse with sql.js, and report the schema + WordList stats.
// READ-ONLY on the live instance. Private row counts, samples, and decoded copies
// under harness/_dump/ require MIGAKU_UNSAFE_DEBUG=1.
import { chromium } from 'playwright-core';
import zlib from 'node:zlib';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import initSqlJs from 'sql.js';
import { requireTestShowPage, UNSAFE_DEBUG } from './safety.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const EA = process.env.MIGAKU_EXT_ID || 'dmeppfcidcpcocleneopiblmpnbokhep';
const OUT_DIR = path.join(__dirname, '_dump');
if (UNSAFE_DEBUG) fs.mkdirSync(OUT_DIR, { recursive: true });

const b = await chromium.connectOverCDP(process.env.CDP_URL || 'http://localhost:9222');
const ctx = b.contexts()[0];
let page = ctx.pages().find(p => { try { return p.url().includes(EA); } catch { return false; } });
({ page } = await requireTestShowPage(ctx, EA));
console.log(`reading srs/data from guarded TestShow tab (${UNSAFE_DEBUG ? 'unsafe raw debug enabled' : 'schema summary only'})`);

// Extract the blob as base64 inside the page (binary-safe through CDP). Chunked btoa to avoid call-stack limits.
const meta = await page.evaluate(async () => {
  const open = (name) => new Promise((ok, no) => { const r = indexedDB.open(name); r.onsuccess = () => ok(r.result); r.onerror = () => no(r.error); });
  const getAll = (db, store) => new Promise((ok, no) => { const tx = db.transaction(store, 'readonly'); const r = tx.objectStore(store).getAll(); r.onsuccess = () => ok(r.result); r.onerror = () => no(r.error); });
  const db = await open('srs');
  const rows = await getAll(db, 'data');
  db.close();
  const rec = rows.find(r => r.data && (r.data instanceof Uint8Array || r.data.byteLength));
  if (!rec) return { error: 'no data blob' };
  const u8 = rec.data instanceof Uint8Array ? rec.data : new Uint8Array(rec.data);
  // chunked base64
  let bin = '';
  const CH = 0x8000;
  for (let i = 0; i < u8.length; i += CH) bin += String.fromCharCode.apply(null, u8.subarray(i, i + CH));
  return { path: rec.path, byteLength: u8.length, b64: btoa(bin) };
});
if (meta.error) { console.log('ERROR:', meta.error); await b.close(); process.exit(1); }
console.log('blob records=1 gzip bytes=', meta.byteLength);

const gz = Buffer.from(meta.b64, 'base64');
const raw = zlib.gunzipSync(gz);
if (UNSAFE_DEBUG) {
  fs.writeFileSync(path.join(OUT_DIR, 'core.db.gz'), gz);
  fs.writeFileSync(path.join(OUT_DIR, 'core.db'), raw);
}
console.log('inflated bytes=', raw.length, 'header=', raw.slice(0, 16).toString('latin1').replace(/\0/g, '\\0'));

const SQL = await initSqlJs();
const sqldb = new SQL.Database(raw);

// 1) full schema
const schema = sqldb.exec("SELECT type,name,sql FROM sqlite_master WHERE type IN ('table','view','index') ORDER BY type,name");
console.log('\n=== SQLITE SCHEMA (tables/views) ===');
if (schema[0]) for (const row of schema[0].values) {
  const [type, name, sql] = row;
  if (type === 'table' || type === 'view') console.log(`\n[${type}] ${name}\n${sql}`);
}
console.log('\n--- table list ---');
const tl = sqldb.exec("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name");
if (tl[0]) console.log(tl[0].values.map(r => r[0]).join(', '));

// 2) row counts for every table
console.log('\n=== ROW COUNTS ===');
if (!UNSAFE_DEBUG) console.log('(suppressed; set MIGAKU_UNSAFE_DEBUG=1 to opt in)');
if (UNSAFE_DEBUG && tl[0]) for (const [name] of tl[0].values) {
  try { const c = sqldb.exec(`SELECT COUNT(*) FROM "${name}"`); console.log(name.padEnd(28), c[0].values[0][0]); }
  catch (e) { console.log(name.padEnd(28), 'ERR', e.message); }
}

// 3) WordList specifics (if present)
const hasWordList = tl[0] && tl[0].values.some(r => /wordlist/i.test(r[0]));
const wlName = hasWordList ? tl[0].values.find(r => /wordlist/i.test(r[0]))[0] : null;
if (wlName) {
  console.log(`\n=== ${wlName} columns ===`);
  const cols = sqldb.exec(`PRAGMA table_info("${wlName}")`);
  if (cols[0]) console.log(cols[0].values.map(r => `${r[1]}:${r[2]}`).join(', '));

  // counts by knownStatus (guard column existence)
  const colNames = cols[0] ? cols[0].values.map(r => r[1]) : [];
  const hasDel = colNames.includes('del');
  const hasStatus = colNames.includes('knownStatus');
  const hasLang = colNames.includes('language');
  const where = hasDel ? 'WHERE del=0' : '';
  if (hasStatus && UNSAFE_DEBUG) {
    console.log(`\n=== counts by knownStatus (${where || 'all'}) ===`);
    const q = sqldb.exec(`SELECT knownStatus, ${hasLang ? 'language' : "'?' AS language"}, COUNT(*) FROM "${wlName}" ${where} GROUP BY knownStatus${hasLang ? ', language' : ''} ORDER BY 3 DESC`);
    if (q[0]) for (const r of q[0].values) console.log(`  ${String(r[0]).padEnd(10)} lang=${String(r[1]).padEnd(6)} ${r[2]}`);
  }
  // Raw rows are private learner data and require an explicit opt-in.
  if (UNSAFE_DEBUG) console.log('\n=== sample rows (10; unsafe raw debug) ===');
  const sampleCols = colNames.filter(c => /dictform|secondary|reading|partofspeech|language|knownstatus|del|hassrs|created|interval|due/i.test(c));
  const sel = (sampleCols.length ? sampleCols : colNames).map(c => `"${c}"`).join(',');
  const samp = UNSAFE_DEBUG ? sqldb.exec(`SELECT ${sel} FROM "${wlName}" ${where} ${hasStatus ? "" : ""} LIMIT 10`) : [];
  if (samp[0]) { console.log('cols:', samp[0].columns.join(' | ')); for (const r of samp[0].values) console.log('  ', r.map(x => x === null ? '' : String(x)).join(' | ')); }
  // a few KNOWN japanese words specifically
  if (hasStatus && UNSAFE_DEBUG) {
    console.log('\n=== sample KNOWN ja words (15) ===');
    const jq = sqldb.exec(`SELECT ${sel} FROM "${wlName}" WHERE knownStatus='KNOWN' ${hasLang ? "AND language='ja'" : ''} ${hasDel ? 'AND del=0' : ''} LIMIT 15`);
    if (jq[0]) { console.log('cols:', jq[0].columns.join(' | ')); for (const r of jq[0].values) console.log('  ', r.map(x => x === null ? '' : String(x)).join(' | ')); }
  }
}

sqldb.close();
console.log(UNSAFE_DEBUG
  ? '\n(local decoded copy: harness/_dump/core.db — unsafe private artifact; keep ignored)'
  : '\n(no database artifact written; set MIGAKU_UNSAFE_DEBUG=1 to opt in)');
await b.close();
