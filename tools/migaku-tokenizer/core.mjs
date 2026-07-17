// Load Migaku's in-process Kotlin Core in pure Node and expose a JSON-RPC client.
//
// The analyzer bundle wires an in-process Core client at import (const <client> =
// <Anr>(send, recv) talking to Tnr.get().jsonApi). That client is module-internal,
// so we build a patched copy of the bundle that re-exports it, with sibling chunks
// symlinked in a temp dir so relative imports resolve. Mangled names are derived
// per-build by regex, so this survives extension updates.
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { resolveExt } from './extpath.mjs';
import { installShims } from './shims.mjs';
import { seedMigakuIdb } from './seed.mjs';

function deriveNames(src) {
  // const <client>=<Anr>(<send>,<recv>),<enum>={PING:
  const m = src.match(/(?:const|let|var)\s+([A-Za-z_$][\w$]*)=([A-Za-z_$][\w$]*)\(([A-Za-z_$][\w$]*),([A-Za-z_$][\w$]*)\),([A-Za-z_$][\w$]*)=\{PING:/);
  // <Tnr>.get().jsonApi
  const t = src.match(/([A-Za-z_$][\w$]*)\.get\(\)\.jsonApi/);
  if (!m) throw new Error('could not locate the wired Core client (const X=Anr(a,b),E={PING:)');
  if (!t) throw new Error('could not locate the Core singleton (X.get().jsonApi)');
  return { client: m[1], anr: m[2], coreEnum: m[5], tnr: t[1] };
}

// Derive the mangled names needed to force Core's auth-processing to "Ready"
// offline (per-build, like deriveNames). Falls back to the 1.31.0.6 build's names.
function deriveNativeScorerNames(src) {
  // "Ready" state ctor: right after the set-Ready log, the 2nd `.a1u(X())` sets
  // authProcessingState = Ready (e.g. `.gdy_1.a1u(Mue())`).
  const readyM = src.match(/setting auth processing state to Ready[^"]*"\)[^;]*?\.a1u\([^)]*\)[^;]*?\.a1u\(([A-Za-z0-9_$]+)\(\)\)/);
  const ready = readyM ? readyM[1] : 'Mue';
  // The auth manager is referenced by >=1 coroutine that logs "Current auth
  // state: "+this.<field>. (Two such coroutines exist; the manager instance is
  // the same.) Capture it from EVERY such coroutine's ctor so whichever gets
  // constructed first stamps it on the global.
  const mgrFields = [...src.matchAll(/Current auth state: "\+this\.([A-Za-z0-9_$]+)\./g)].map((m) => m[1]);
  const ctors = [];
  for (const f of new Set(mgrFields)) {
    const m = src.match(new RegExp('y\\.call\\(this,[a-z]\\),this\\.' + f + '=([a-z]),this\\.[A-Za-z0-9_$]+=[a-z]'));
    if (m) ctors.push({ assign: m[0], arg: m[1] });
  }
  // Online startup awaits that hang offline, found by their adjacent log string:
  //   "<log>"...,this.z8_1=N,t=<awaitedCall>,t===m())return t
  // We replace <awaitedCall> with `g` (the coroutine Unit value) so it doesn't
  // suspend. Neither is needed for offline WordList reads.
  const skips = [];
  for (const lg of ['Setting up serverDatabaseChangeListener', 'setupCollectDownloadMedia']) {
    const m = src.match(new RegExp(lg + '[\\s\\S]{0,80}?,t=([A-Za-z0-9_$.]+\\([^)]*\\)),t===m\\(\\)\\)return t'));
    if (m && src.split(m[1]).length === 2) skips.push([m[1], 'g', lg]); // only if unique
  }
  // The two DB-init awaits right after "assigned db" (post-assignment sync /
  // migration) block setting the DB-ready flag (iek_1.a1u(!0)) offline because
  // they touch Firestore. The seeded DB is already a complete Migaku DB, so skip
  // them (→ `g`) to let the coroutine reach the ready-flag set + return.
  const dbm = src.match(/"assigned db"\)[\s\S]{0,40}?,t=(.+?),t===m\(\)\)return t;continue t;case \d+:if\(this\.z8_1=\d+,t=(.+?),t===m\(\)\)return t/);
  if (dbm) {
    for (const call of [dbm[1], dbm[2]]) {
      if (src.split(call).length === 2) skips.push([call, 'g', 'db-init:' + call.slice(0, 28)]);
    }
  }
  return { ready, mgrFields, ctors, skips };
}

let _cached = null;

export async function loadCore({ seedDb = false } = {}) {
  if (_cached) return _cached;
  const ext = resolveExt();
  const logs = installShims(ext);
  // Seed IndexedDB with Migaku's synced SQLite blob BEFORE the bundle inits its
  // DB driver (needed for WordListMgr / the native comprehension scorer).
  let seed = null;
  if (seedDb) seed = await seedMigakuIdb();
  let src = fs.readFileSync(ext.bundle, 'utf8');
  const names = deriveNames(src);

  // Native-scorer unlock (offline comprehension): Core won't serve WordListMgr
  // reads until its auth-processing StateFlow reaches "Ready", which normally
  // needs an online Firestore handshake. We force that transition offline by
  // (a) capturing the FirebaseAuthProcessing manager instance and (b) exporting
  // the "Ready" state constructor, so the loader can poke the StateFlows.
  let nativeNames = null;
  if (process.env.MIGAKU_NATIVE_SCORER) {
    nativeNames = deriveNativeScorerNames(src);
    // Stamp the auth manager on a global from each holder-coroutine's ctor.
    for (const c of nativeNames.ctors) {
      const cap = `${c.assign},globalThis.__migAuthMgr=${c.arg}`;
      if (src.includes(c.assign)) src = src.replace(c.assign, cap);
    }
    // Skip the two online startup awaits that hang offline so the DB-init
    // coroutine reaches its set-Ready step (which signals DB-ready, unblocking
    // WordListMgr reads). Anchored on the adjacent log strings for per-build
    // robustness; both are no-ops we don't need offline (real-time sync + media
    // collection). Detail in notes/NATIVE_SCORER_DESIGN.md.
    const skips = nativeNames.skips || [];
    for (const [from, to, why] of skips) {
      if (src.includes(from)) { src = src.replace(from, to); }
      else log.warn?.(`native-scorer: skip pattern not found (${why})`);
    }
  }

  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'mig-core-'));
  // symlink every asset chunk so the patched module's relative imports resolve
  for (const f of fs.readdirSync(ext.assets)) {
    try { fs.symlinkSync(path.join(ext.assets, f), path.join(tmp, f)); } catch {}
  }
  const patched = path.join(tmp, '__mig_patched.mjs');
  let exportNames = `${names.client} as __mig_client, ${names.tnr} as __mig_core, ${names.anr} as __mig_Anr`;
  if (nativeNames) exportNames += `, ${nativeNames.ready} as __mig_Ready`;
  const exportLine = `\n;export { ${exportNames} };\n`;
  fs.writeFileSync(patched, src + exportLine);

  const mod = await import(pathToFileURL(patched).href);
  const client = mod.__mig_client;
  if (!client) throw new Error('patched export __mig_client missing');

  // RPC call: client[<Mgr>_<method>](params) -> "<Mgr>.<method>" over the in-process bus.
  const call = (method, params = {}) => {
    const prop = method.replace('.', '_');
    if (typeof client[prop] !== 'function') throw new Error(`no client method for ${method}`);
    return client[prop](params);
  };

  _cached = { ext, names, nativeNames, mod, client, core: mod.__mig_core, call, logs, tmp, seed,
              ready: mod.__mig_Ready };
  return _cached;
}

// Force Core's auth-processing to "Ready" offline. The manager (captured on
// globalThis.__migAuthMgr when the waitForAuthProcessing coroutine is first
// constructed) holds the auth StateFlows. We push every "not-ready" flow to its
// ready value — string states "Initializing"/"Processing" → the Ready singleton;
// boolean false (firebaseAuthState) → true — replicating fO's effect without the
// online Firestore handshake that gates the normal transition.
export function forceAuthReady() {
  const mgr = globalThis.__migAuthMgr;
  if (!_cached) return { ok: false, reason: 'core not loaded' };
  if (!mgr) return { ok: false, reason: 'manager not captured (call waitForAuthProcessing/getKnownNumber once first)' };
  const Ready = _cached.ready;
  const readyVal = typeof Ready === 'function' ? Ready() : Ready;
  const changed = {};
  for (const k of Object.getOwnPropertyNames(mgr)) {
    let fl; try { fl = mgr[k]; } catch { continue; }
    if (!fl || typeof fl.a1u !== 'function' || typeof fl.g1 !== 'function') continue;
    let cur; try { cur = fl.g1(); } catch { continue; }
    const s = (() => { try { return String(cur); } catch { return ''; } })();
    if (cur === false) { try { fl.a1u(true); changed[k] = `${s}->true`; } catch {} }
    else if (/Initializing|Processing/.test(s) && readyVal != null) {
      try { fl.a1u(readyVal); changed[k] = `${s}->${String(fl.g1())}`; } catch {}
    }
  }
  return { ok: true, changed };
}

if (import.meta.url === `file://${process.argv[1]}`) {
  const c = await loadCore();
  console.log('derived names:', c.names);
  console.log('core members:', Object.getOwnPropertyNames(c.core || {}).slice(0, 20).join(','));
  process.exit(0);
}
