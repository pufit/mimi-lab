// In-process replacement for Migaku's bundled `sqljs.worker-<hash>.js`.
//
// WHY: WordListMgr (known-status) reads the synced SQLite DB through
//   new Worker(new URL("/assets/sqljs.worker-<hash>.js"))
// but shims.mjs stubs Worker as a no-op, so every WordListMgr.getKnownNumber /
// queryWords / getMany request is posted into the void and the native
// comprehension scorer (CatalogMgr.calculateComprehensionScoreFromText) hangs.
//
// This class speaks the worker's protocol in-process, backed by the real `sql.js`
// over the seeded Migaku DB blob (the same core_<uid>.db we sync for known-words):
//   in :  { id, action:"exec", sql, params }        (+ transaction sql strings)
//   out:  { id, results:[{columns,values}] }  |  { id, error }  |  { id, result:"…" }
// Write/batch actions are no-ops that report success (the scorer is read-only).
//
// Faithful enough for the read path the scorer needs; instrument with
// MIGAKU_SQLJS_LOG=1 to dump the live protocol when validating against a new build.
import fs from 'node:fs';
import zlib from 'node:zlib';
import initSqlJs from 'sql.js';

const LOG = !!process.env.MIGAKU_SQLJS_LOG;
const GZ = process.env.MIGAKU_SRS_GZ;
const RAW = process.env.MIGAKU_SRS_DB; // optional already-gunzipped path

let _SQL = null;
async function getSQL() {
  if (!_SQL) _SQL = await initSqlJs();
  return _SQL;
}

function loadDbBytes() {
  if (!GZ) {
    throw new Error('MIGAKU_SRS_GZ is required; set it to a user-provided database path');
  }
  if (RAW && fs.existsSync(RAW)) return new Uint8Array(fs.readFileSync(RAW));
  if (!fs.existsSync(GZ)) throw new Error(`MIGAKU_SRS_GZ path not found: ${GZ}`);
  const buf = fs.readFileSync(GZ);
  // The srs/data blob is gzip (magic 1f 8b); tolerate an already-raw "SQLite" file too.
  if (buf[0] === 0x1f && buf[1] === 0x8b) return new Uint8Array(zlib.gunzipSync(buf));
  return new Uint8Array(buf);
}

export class SqlJsWorker {
  constructor(url) {
    this._url = String(url);
    this.onmessage = null;
    this.onerror = null;
    this._listeners = { message: [], error: [] };
    this._db = null;
    this._queue = [];
    this._ready = (async () => {
      const SQL = await getSQL();
      this._db = new SQL.Database(loadDbBytes());
      if (LOG) {
        try {
          const n = this._db.exec("SELECT COUNT(*) FROM WordList WHERE del=0 AND language='ja' AND knownStatus='KNOWN'");
          console.error(`[sqljs-worker] DB open; KNOWN(ja)=${n?.[0]?.values?.[0]?.[0]}`);
        } catch (e) { console.error('[sqljs-worker] DB open (count failed):', String(e)); }
      }
      const q = this._queue; this._queue = [];
      for (const m of q) this._handle(m);
    })().catch((e) => this._emitError(e));
  }

  // --- Web Worker surface ---
  postMessage(msg) {
    if (this._db) this._handle(msg);
    else this._queue.push(msg);
  }
  addEventListener(type, fn) { (this._listeners[type] ||= []).push(fn); }
  removeEventListener(type, fn) {
    const a = this._listeners[type]; if (!a) return;
    const i = a.indexOf(fn); if (i >= 0) a.splice(i, 1);
  }
  terminate() { try { this._db?.close(); } catch {} this._db = null; }

  // --- protocol ---
  _handle(msg) {
    const e = msg && typeof msg === 'object' && 'data' in msg ? msg.data : msg; // accept raw or {data}
    const id = e && e.id;
    try {
      if (LOG) console.error('[sqljs-worker] <<', JSON.stringify({ id, action: e?.action, sql: (e?.sql || '').slice(0, 90) }));
      // (re)open from an explicit buffer if Core ever passes one
      if (e && (e.action === 'open' || e.buffer)) {
        if (e.buffer) { getSQL().then((SQL) => { this._db = new SQL.Database(new Uint8Array(e.buffer)); this._post({ id, ready: true }); }); return; }
      }
      if (e && typeof e.sql === 'string') {
        const results = this._db.exec(e.sql, e.params || undefined); // [{columns,values}]
        this._post({ id, results });
        return;
      }
      // write/batch actions (Card*, WordList batch upsert, …): scorer never hits
      // these; acknowledge success so any stray write doesn't wedge a caller.
      this._post({ id, result: 'ok' });
    } catch (err) {
      if (LOG) console.error('[sqljs-worker] ERR', String(err?.message ?? err));
      this._post({ id, error: String(err?.message ?? err) });
    }
  }

  _post(data) {
    if (LOG) console.error('[sqljs-worker] >>', JSON.stringify({ id: data.id, rows: data.results ? data.results.length : undefined, error: data.error }));
    const ev = { data };
    queueMicrotask(() => {
      try { this.onmessage && this.onmessage(ev); } catch (e) { this._emitError(e); }
      for (const l of this._listeners.message) { try { l(ev); } catch (e) { this._emitError(e); } }
    });
  }
  _emitError(err) {
    const ev = { message: String(err?.message ?? err), error: err };
    try { this.onerror && this.onerror(ev); } catch {}
    for (const l of this._listeners.error) { try { l(ev); } catch {} }
  }
}

export function isSqljsWorkerUrl(url) {
  return /sqljs\.worker/.test(String(url));
}
