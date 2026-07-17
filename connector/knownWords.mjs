// knownWords.mjs — read Migaku's known-words DB straight from the EA extension
// origin's IndexedDB (where Migaku now lives — the viewing machine):
//   IndexedDB 'srs' → store 'data' → the one gzipped SQLite blob (core_<uid>.db),
//   gunzip (pako), load with sql.js, SELECT from WordList.
// READ-ONLY. Ported verbatim from the old companion/knownWords.mjs.
import initSqlJs from 'sql.js';
import pako from 'pako';
import { getEAPage } from './cdp.mjs';

let _sqlJs = null;
async function sqlJs() {
  if (!_sqlJs) _sqlJs = await initSqlJs();
  return _sqlJs;
}

async function pullBlob(cfg) {
  const page = await getEAPage(cfg);
  const meta = await page.evaluate(async () => {
    const open = (name) => new Promise((ok, no) => {
      const r = indexedDB.open(name);
      r.onsuccess = () => ok(r.result);
      r.onerror = () => no(r.error);
    });
    const getAll = (db, store) => new Promise((ok, no) => {
      const tx = db.transaction(store, 'readonly');
      const r = tx.objectStore(store).getAll();
      r.onsuccess = () => ok(r.result);
      r.onerror = () => no(r.error);
    });
    let db;
    try { db = await open('srs'); }
    catch (e) { return { error: 'cannot open IndexedDB "srs": ' + (e && e.message || e) }; }
    let rows;
    try { rows = await getAll(db, 'data'); }
    catch (e) { return { error: 'cannot read store "data": ' + (e && e.message || e) }; }
    finally { try { db.close(); } catch {} }
    const rec = rows.find((r) => r.data && (r.data instanceof Uint8Array || r.data.byteLength));
    if (!rec) return { error: 'no known-words blob in srs/data (not logged in?)' };
    const u8 = rec.data instanceof Uint8Array ? rec.data : new Uint8Array(rec.data);
    let bin = '';
    const CH = 0x8000;
    for (let i = 0; i < u8.length; i += CH) bin += String.fromCharCode.apply(null, u8.subarray(i, i + CH));
    return { path: rec.path, byteLength: u8.length, b64: btoa(bin) };
  });
  if (meta.error) throw new Error(meta.error);
  return meta;
}

/**
 * Read the WordList → [{ dictForm, reading, knownStatus }] (del=0, language='ja').
 * Returns { rows, counts, blobPath, gzipBytes }.
 */
export async function readKnownWords(cfg) {
  const meta = await pullBlob(cfg);
  const gz = Buffer.from(meta.b64, 'base64');
  if (!(gz[0] === 0x1f && gz[1] === 0x8b)) {
    throw new Error(`blob is not gzip (magic ${gz.slice(0, 3).toString('hex')})`);
  }
  const raw = Buffer.from(pako.ungzip(gz));
  if (raw.slice(0, 15).toString('latin1') !== 'SQLite format 3') {
    throw new Error('inflated blob is not a SQLite database');
  }

  const SQL = await sqlJs();
  const db = new SQL.Database(raw);
  try {
    const tbl = db.exec("SELECT name FROM sqlite_master WHERE type='table' AND name='WordList'");
    if (!tbl.length) throw new Error('WordList table not found in Migaku DB');
    const cols = db.exec('PRAGMA table_info("WordList")');
    const colNames = cols[0] ? cols[0].values.map((r) => r[1]) : [];
    for (const need of ['dictForm', 'secondary', 'knownStatus']) {
      if (!colNames.includes(need)) throw new Error(`WordList missing column "${need}"`);
    }
    const where = [];
    if (colNames.includes('del')) where.push('del=0');
    if (colNames.includes('language')) where.push("language='ja'");
    const whereSql = where.length ? 'WHERE ' + where.join(' AND ') : '';

    const res = db.exec(`SELECT dictForm, secondary, knownStatus FROM WordList ${whereSql}`);
    const rows = [];
    const counts = {};
    if (res[0]) {
      for (const [dictForm, secondary, knownStatus] of res[0].values) {
        rows.push({
          dictForm,
          reading: secondary && secondary.length ? secondary : dictForm,
          knownStatus,
        });
        counts[knownStatus] = (counts[knownStatus] || 0) + 1;
      }
    }
    return { rows, counts, blobPath: meta.path, gzipBytes: meta.byteLength };
  } finally {
    db.close();
  }
}
