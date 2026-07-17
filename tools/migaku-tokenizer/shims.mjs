// Browser/extension global shims so the Kotlin/JS analyzer bundle
// (assets/player-store-*.js) can evaluate and run under Node.
//
// Strategy: the bundle is self-contained (no top-level imports) and already has
// Node-detection branches. We provide the web globals it expects and route every
// network/file access (fetch, chrome.runtime.getURL, Worker URLs) to the
// extension's on-disk assets/ and core/ dirs. Fully offline.
import 'fake-indexeddb/auto'; // provides globalThis.indexedDB (Core's resource registry)
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { createRequire } from 'node:module';
import http from 'node:http';
import https from 'node:https';
import { EventEmitter } from 'node:events';
import { Readable } from 'node:stream';
import { makeStorage, opfsLog } from './opfs.mjs';
import { SqlJsWorker, isSqljsWorkerUrl } from './sqljs-worker.mjs';

// The bundle's isNode path loads dict files (fst.bin, etc.) via Node http with a
// malformed hostless URL (///core/fst.bin). Intercept http(s).request/get and
// serve any /core/* or /assets/* request straight from the extension dir on disk.
export const netLog = [];
function diskFileForUrl(urlStr) {
  if (!urlStr || !EXT) return null;
  const s = String(urlStr).split(/[?#]/)[0];
  let m = s.match(/\/core\/(.+)$/);
  if (m) return path.join(EXT.core, m[1]);
  m = s.match(/\/assets\/(.+)$/);
  if (m) return path.join(EXT.assets, m[1]);
  m = s.match(/([^/]+\.(?:bin|model|table|json|db))$/);
  if (m) { const inCore = path.join(EXT.core, m[1]); if (fs.existsSync(inCore)) return inCore; }
  return null;
}
function mockRequest(file, urlStr, cb) {
  const req = new EventEmitter();
  req.setHeader = () => {}; req.getHeader = () => undefined; req.removeHeader = () => {};
  req.write = () => true; req.abort = () => {}; req.destroy = () => { req.emit('close'); };
  req.setTimeout = () => req; req.flushHeaders = () => {}; req.removeAllListeners = () => req;
  req.end = () => {
    setImmediate(() => {
      const exists = fs.existsSync(file) && fs.statSync(file).isFile();
      const res = new Readable({ read() {} });
      res.statusCode = exists ? 200 : 404;
      res.statusMessage = exists ? 'OK' : 'Not Found';
      res.headers = { 'content-type': 'application/octet-stream' };
      if (typeof cb === 'function') cb(res);
      req.emit('response', res);
      if (exists) res.push(fs.readFileSync(file));
      res.push(null);
    });
  };
  return req;
}
for (const m of [http, https]) {
  for (const fn of ['request', 'get']) {
    const orig = m[fn];
    if (orig && !orig.__migWrapped) {
      const wrapped = function (...args) {
        let urlStr = null, cb = null;
        for (const a of args) if (typeof a === 'function') cb = a;
        const a0 = args[0];
        if (typeof a0 === 'string') urlStr = a0;
        else if (a0 instanceof URL) urlStr = a0.href;
        else if (a0 && typeof a0 === 'object') urlStr = a0.href || a0.path || a0.pathname || null;
        const file = diskFileForUrl(urlStr);
        netLog.push(`${fn} ${urlStr}${file ? ' -> disk' : ''}`);
        if (file) return mockRequest(file, urlStr, cb);
        return orig.apply(this, args);
      };
      wrapped.__migWrapped = true;
      m[fn] = wrapped;
    }
  }
}

const ORIGIN = 'https://migaku-ext.local';
const BASE_HREF = `${ORIGIN}/pages/player/index.html`;

// Diagnostics: surface the sub-errors hidden inside AggregateError / Promise.any,
// which Core swallows into a bare "AggregateError" during parser init.
export const errLog = [];
(function patchDiagnostics() {
  const g = globalThis;
  if (typeof Promise.any === 'function' && !Promise.__migPatched) {
    const orig = Promise.any.bind(Promise);
    Promise.any = function (iterable) {
      let arr; try { arr = Array.from(iterable); } catch { return orig(iterable); }
      const wrapped = arr.map((p) => Promise.resolve(p).catch((e) => {
        try { errLog.push('any⊥ ' + String((e && (e.stack || e.message)) || e).slice(0, 400)); } catch {}
        throw e;
      }));
      return orig(wrapped);
    };
    Promise.__migPatched = true;
  }
  const AE = g.AggregateError;
  if (AE && !AE.__migPatched) {
    class P extends AE {
      constructor(errs, msg) {
        super(errs, msg);
        try { for (const e of (errs || [])) errLog.push('AE• ' + String((e && (e.stack || e.message)) || e).slice(0, 400)); } catch {}
      }
    }
    P.__migPatched = true;
    try { Object.defineProperty(g, 'AggregateError', { value: P, configurable: true, writable: true }); } catch {}
  }
})();

let EXT = null; // { assets, core }
const fetchLog = [];
const workerLog = [];

function diskPathFor(pathname) {
  // Map an URL pathname onto the unpacked extension dirs.
  // e.g. /assets/sqljs.worker-x.js -> <assets>/sqljs.worker-x.js
  //      /core/en.db.json          -> <core>/en.db.json
  //      /core/models_light/x      -> <core>/models_light/x
  const clean = pathname.replace(/^\/+/, '');
  if (clean.startsWith('assets/')) return path.join(EXT.assets, clean.slice('assets/'.length));
  if (clean.startsWith('core/')) return path.join(EXT.core, clean.slice('core/'.length));
  // bare filenames the analyzer asks for (it sometimes uses a VFS rooted at core/)
  const inCore = path.join(EXT.core, clean);
  if (fs.existsSync(inCore)) return inCore;
  const inAssets = path.join(EXT.assets, clean);
  if (fs.existsSync(inAssets)) return inAssets;
  return null;
}

function toURL(input) {
  const raw = typeof input === 'string' ? input
    : (input && input.url) ? input.url
    : String(input);
  try { return new URL(raw); } catch { return new URL(raw, BASE_HREF); }
}

async function diskFetch(input) {
  const url = toURL(input);
  fetchLog.push(url.pathname);
  const file = diskPathFor(url.pathname);
  if (!file || !fs.existsSync(file)) {
    return new Response(null, { status: 404, statusText: `no disk map for ${url.pathname}` });
  }
  const buf = fs.readFileSync(file);
  return new Response(new Uint8Array(buf), {
    status: 200,
    headers: { 'content-type': guessType(file) },
  });
}

function guessType(file) {
  if (file.endsWith('.json')) return 'application/json';
  if (file.endsWith('.js')) return 'text/javascript';
  if (file.endsWith('.wasm')) return 'application/wasm';
  return 'application/octet-stream';
}

// Minimal Worker stub. For the *load* probe no worker should be constructed; if
// one is, we record it (so we know the dict path needs the worker shim) without
// crashing module evaluation.
class StubWorker {
  constructor(url, opts) {
    workerLog.push({ url: String(url), opts });
    this.onmessage = null;
    this.onerror = null;
  }
  postMessage() {}
  terminate() {}
  addEventListener() {}
  removeEventListener() {}
}

// Some globals in modern Node are getter-only (navigator, sometimes fetch).
// safeSet assigns directly, falling back to defineProperty, swallowing failures.
function safeSet(obj, key, value) {
  try { obj[key] = value; if (obj[key] === value) return true; } catch {}
  try { Object.defineProperty(obj, key, { value, writable: true, configurable: true }); return true; }
  catch { return false; }
}

export function installShims(ext) {
  EXT = ext;
  const g = globalThis;

  if (typeof g.self === 'undefined') safeSet(g, 'self', g);
  if (typeof g.window === 'undefined') safeSet(g, 'window', g);

  // The bundle's Node-detection path uses CommonJS require() for built-ins
  // (e.g. crypto/worker_threads); provide a real one for the ESM context.
  if (typeof g.require === 'undefined') safeSet(g, 'require', createRequire(import.meta.url));

  safeSet(g, 'location', {
    href: BASE_HREF, origin: ORIGIN, protocol: 'https:',
    host: 'migaku-ext.local', hostname: 'migaku-ext.local', pathname: '/pages/player/index.html',
    search: '', hash: '', toString() { return this.href; },
  });

  // navigator is read-only in Node 26 — keep the real one, best-effort patch fields.
  if (typeof g.navigator === 'undefined') {
    safeSet(g, 'navigator', {
      userAgent: 'node-migaku-shim', language: 'en', languages: ['en'],
      hardwareConcurrency: 4, platform: 'MacIntel',
    });
  } else {
    for (const [k, v] of [['language', 'en'], ['languages', ['en']]]) {
      if (g.navigator[k] === undefined) { try { g.navigator[k] = v; } catch {} }
    }
  }

  // OPFS (navigator.storage) backed by disk: reads fall through to the ext core/
  // dir (mounted at root + at /core), writes go to a throwaway temp dir.
  const opfsRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'mig-opfs-'));
  const storage = makeStorage(opfsRoot, ext.core, { core: ext.core });
  try {
    Object.defineProperty(g.navigator, 'storage', { value: storage, configurable: true, writable: true });
  } catch {
    const rn = g.navigator || {};
    safeSet(g, 'navigator', {
      userAgent: rn.userAgent || 'node', language: 'en', languages: ['en'],
      hardwareConcurrency: rn.hardwareConcurrency || 4, platform: 'MacIntel', storage,
    });
  }

  const noop = () => {};

  // Event + misc browser globals that chunks touch at module-eval time.
  if (typeof g.addEventListener !== 'function') safeSet(g, 'addEventListener', noop);
  if (typeof g.removeEventListener !== 'function') safeSet(g, 'removeEventListener', noop);
  if (typeof g.dispatchEvent !== 'function') safeSet(g, 'dispatchEvent', () => true);
  if (typeof g.requestAnimationFrame !== 'function') safeSet(g, 'requestAnimationFrame', (cb) => setTimeout(cb, 0));
  if (typeof g.cancelAnimationFrame !== 'function') safeSet(g, 'cancelAnimationFrame', (id) => clearTimeout(id));
  if (typeof g.matchMedia !== 'function') safeSet(g, 'matchMedia', () => ({ matches: false, media: '', addEventListener: noop, removeEventListener: noop, addListener: noop, removeListener: noop }));
  if (typeof g.getComputedStyle !== 'function') safeSet(g, 'getComputedStyle', () => ({ getPropertyValue: () => '' }));
  const memStore = () => {
    const m = new Map();
    return {
      getItem: (k) => (m.has(k) ? m.get(k) : null), setItem: (k, v) => m.set(k, String(v)),
      removeItem: (k) => m.delete(k), clear: () => m.clear(),
      key: (i) => [...m.keys()][i] ?? null, get length() { return m.size; },
    };
  };
  if (typeof g.localStorage === 'undefined') safeSet(g, 'localStorage', memStore());
  if (typeof g.sessionStorage === 'undefined') safeSet(g, 'sessionStorage', memStore());

  // Web API base classes that bundle code does `class X extends Base` against at
  // eval time (e.g. window.VTTCue for subtitle cues). Stub any missing as empty
  // classes so subclassing doesn't throw.
  const BASE_CLASSES = [
    'HTMLElement', 'Element', 'Node', 'CharacterData', 'Text', 'Comment',
    'DocumentFragment', 'ShadowRoot', 'HTMLDivElement', 'HTMLSpanElement',
    'HTMLCanvasElement', 'OffscreenCanvas', 'VTTCue', 'VTTRegion', 'TextTrackCue',
    'Image', 'Audio',
  ];
  for (const c of BASE_CLASSES) if (typeof g[c] === 'undefined') safeSet(g, c, class {});

  // Functional XMLHttpRequest stub. Firestore's WebChannel transport constructs
  // XHRs; an empty `class {}` is missing .abort/.open/.send → "abort is not a
  // function" crashes the auth-processing flow. This fails fast (status 0) so the
  // online bits degrade to offline instead of throwing.
  if (typeof g.XMLHttpRequest === 'undefined' || !g.XMLHttpRequest.prototype.send) {
    safeSet(g, 'XMLHttpRequest', class {
      constructor() {
        this.readyState = 0; this.status = 0; this.responseText = ''; this.response = '';
        this.onreadystatechange = null; this.onload = null; this.onerror = null;
        this.onabort = null; this.ontimeout = null; this.withCredentials = false;
        this.upload = { addEventListener: noop, removeEventListener: noop };
      }
      open(m, u) { this._m = m; this._u = u; this.readyState = 1; }
      setRequestHeader() {}
      overrideMimeType() {}
      getAllResponseHeaders() { return ''; }
      getResponseHeader() { return null; }
      abort() { try { this.onabort && this.onabort({ type: 'abort' }); } catch {} }
      addEventListener(t, fn) { this['on' + t] = fn; }
      removeEventListener(t) { this['on' + t] = null; }
      send() {
        queueMicrotask(() => {
          this.readyState = 4; this.status = 0;
          try { this.onreadystatechange && this.onreadystatechange(); } catch {}
          try { this.onerror && this.onerror({ type: 'error' }); } catch {}
        });
      }
    });
  }

  // Minimal fake-DOM node: enough methods to survive eval-time DOM work in UI
  // chunks (style injection, etc.) without throwing. Tree pointers return null so
  // any parentNode/firstChild walks terminate instead of looping forever.
  const el = () => ({
    style: new Proxy({}, { get: () => '', set: () => true }),
    classList: { add: noop, remove: noop, toggle: noop, contains: () => false },
    dataset: {}, attributes: [], children: [], childNodes: [],
    parentNode: null, parentElement: null, firstChild: null, lastChild: null,
    nextSibling: null, previousSibling: null, ownerDocument: null,
    setAttribute: noop, getAttribute: () => null, removeAttribute: noop, hasAttribute: () => false,
    appendChild: (c) => c, removeChild: (c) => c, insertBefore: (c) => c, replaceChild: (c) => c,
    append: noop, prepend: noop, remove: noop, after: noop, before: noop,
    addEventListener: noop, removeEventListener: noop, dispatchEvent: () => true,
    cloneNode: () => el(), contains: () => false, getContext: () => null,
    querySelector: () => null, querySelectorAll: () => [],
    getBoundingClientRect: () => ({ x: 0, y: 0, width: 0, height: 0, top: 0, left: 0, right: 0, bottom: 0 }),
    focus: noop, blur: noop, click: noop, scrollIntoView: noop,
    get textContent() { return ''; }, set textContent(_) {},
    get innerHTML() { return ''; }, set innerHTML(_) {},
    get innerText() { return ''; }, set innerText(_) {},
  });
  safeSet(g, 'document', g.document || {
    readyState: 'complete', currentScript: { src: BASE_HREF }, cookie: '',
    documentElement: el(), head: el(), body: el(),
    createElement: () => el(), createElementNS: () => el(), createTextNode: () => el(),
    createComment: () => el(), createDocumentFragment: () => el(),
    getElementById: () => null, getElementsByTagName: () => [], getElementsByClassName: () => [],
    querySelector: () => null, querySelectorAll: () => [],
    addEventListener: noop, removeEventListener: noop, dispatchEvent: () => true,
  });

  safeSet(g, 'chrome', g.chrome || {});
  g.chrome.runtime = g.chrome.runtime || {};
  g.chrome.runtime.getURL = (p) => `${ORIGIN}/${String(p).replace(/^\/+/, '')}`;
  g.chrome.runtime.id = process.env.MIGAKU_EXT_ID || 'dmeppfcidcpcocleneopiblmpnbokhep';

  // route all network to disk
  safeSet(g, 'fetch', diskFetch);
  // Worker factory. By default everything is the no-op StubWorker (tokenization
  // never needs a real worker — per-line parse runs in-process). When the native
  // comprehension scorer is enabled (MIGAKU_NATIVE_SCORER=1), route the SQLite
  // worker URL to a real in-process sql.js worker so WordListMgr can read the
  // known-set; all other worker URLs stay stubbed.
  const nativeScorer = !!process.env.MIGAKU_NATIVE_SCORER;
  // Tell Core to skip the online force-pull-sync of the synced DB at startup — we
  // seed the DB locally and have no network. (Read by the bundle as
  // window._core_no_init_with_force_pull_sync during DatabaseAdapter init.)
  if (nativeScorer && g._core_no_init_with_force_pull_sync === undefined) {
    safeSet(g, '_core_no_init_with_force_pull_sync', true);
  }
  function WorkerFactory(url, opts) {
    if (nativeScorer && isSqljsWorkerUrl(url)) return new SqlJsWorker(url);
    return new StubWorker(url, opts);
  }
  safeSet(g, 'Worker', WorkerFactory);
  // importScripts must be undefined so "am I a worker?" checks resolve to false
  if ('importScripts' in g) try { delete g.importScripts; } catch {}

  return { fetchLog, workerLog, opfsLog, opfsRoot, errLog, ORIGIN, BASE_HREF };
}
