// Seed Node's (fake) IndexedDB with Migaku's synced SQLite DB blob + the Firebase
// auth record, exactly as Core's sqljs.worker expects to read them:
//   srs / data / { path:"core_<uid>.db", data:<gzipped SQLite Uint8Array> }
//   firebaseLocalStorageDb / firebaseLocalStorage / firebase:authUser:<apiKey>:[DEFAULT] -> { value:{uid} }
// MIGAKU_SRS_GZ must point to the user-provided core_<uid>.db blob extracted for
// known-words sync. This is the offline equivalent of "synced from Migaku".
import fs from 'node:fs';

// Personal identifiers come from the environment (not committed). This whole
// native-scorer seeding path is dormant (only runs under MIGAKU_NATIVE_SCORER),
// so empty defaults are fine; set MIGAKU_UID/MIGAKU_FB_APIKEY/MIGAKU_EMAIL if you
// ever revive it.
const UID = process.env.MIGAKU_UID || '';
const API_KEY = process.env.MIGAKU_FB_APIKEY || '';
const DB_PATH = `core_${UID}.db`;
const GZ = process.env.MIGAKU_SRS_GZ;

function openCreate(name, stores) {
  return new Promise((res, rej) => {
    const req = indexedDB.open(name, 1);
    req.onupgradeneeded = () => {
      const db = req.result;
      for (const s of stores) if (!db.objectStoreNames.contains(s.name)) db.createObjectStore(s.name, { keyPath: s.keyPath });
    };
    req.onsuccess = () => res(req.result);
    req.onerror = () => rej(req.error);
  });
}
function put(db, store, value) {
  return new Promise((res, rej) => {
    const tx = db.transaction(store, 'readwrite');
    tx.objectStore(store).put(value);
    tx.oncomplete = () => res();
    tx.onerror = () => rej(tx.error);
  });
}

const EMAIL = process.env.MIGAKU_EMAIL || '';

function b64url(obj) {
  return Buffer.from(JSON.stringify(obj)).toString('base64')
    .replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

// A structurally-valid (unsigned) Firebase ID token. The web SDK does NOT verify
// the signature client-side — it only base64-decodes the payload for claims/exp —
// so a fake-signature JWT restores currentUser fine. expSec is far-future so the
// SDK never schedules a proactive refresh (which would hit the network we block).
function fakeIdToken(uid, expSec) {
  const nowSec = Math.floor(Date.now() / 1000);
  const header = { alg: 'RS256', kid: 'offline', typ: 'JWT' };
  const payload = {
    iss: `https://securetoken.google.com/${PROJECT_ID}`,
    aud: PROJECT_ID, auth_time: nowSec, user_id: uid, sub: uid,
    iat: nowSec, exp: expSec, email: EMAIL, email_verified: true,
    firebase: { identities: { email: [EMAIL] }, sign_in_provider: 'password' },
  };
  return `${b64url(header)}.${b64url(payload)}.offline-sig`;
}

const PROJECT_ID = process.env.MIGAKU_FB_PROJECT || 'migaku-controller';

// Build the COMPLETE persisted-user record Firebase's User._fromJSON expects.
// Minimal `{uid,email,emailVerified}` fails its `Hr(uid && stsTokenManager,...)`
// assertion → auth/internal-error. We supply a full record with a token manager
// whose expirationTime is far in the future so no refresh is attempted.
export function buildAuthRecord(uid = UID, apiKey = API_KEY) {
  // ~20 days out: NOT expired (so no refresh-on-use), but under setTimeout's
  // 2^31 ms (~24.8 day) ceiling so the SDK's proactive-refresh timer doesn't
  // overflow → fire immediately → hit the network (offline) and throw.
  const expMs = Date.now() + 20 * 24 * 3600 * 1000;
  return {
    fbase_key: `firebase:authUser:${apiKey}:[DEFAULT]`,
    value: {
      uid,
      email: EMAIL,
      emailVerified: true,
      displayName: null,
      isAnonymous: false,
      photoURL: null,
      providerData: [{
        providerId: 'password', uid: EMAIL, displayName: null,
        email: EMAIL, phoneNumber: null, photoURL: null,
      }],
      stsTokenManager: {
        refreshToken: 'offline-refresh-token',
        accessToken: fakeIdToken(uid, Math.floor(expMs / 1000)),
        expirationTime: expMs,
      },
      createdAt: String(Date.now()),
      lastLoginAt: String(Date.now()),
      apiKey,
      appName: '[DEFAULT]',
    },
  };
}

export async function seedMigakuIdb() {
  if (!GZ) {
    throw new Error('MIGAKU_SRS_GZ is required; set it to a user-provided database path');
  }
  if (!fs.existsSync(GZ)) throw new Error(`MIGAKU_SRS_GZ path not found: ${GZ}`);
  const u8 = new Uint8Array(fs.readFileSync(GZ));
  const srs = await openCreate('srs', [{ name: 'data', keyPath: 'path' }]);
  await put(srs, 'data', { path: DB_PATH, data: u8 });
  const fb = await openCreate('firebaseLocalStorageDb', [{ name: 'firebaseLocalStorage', keyPath: 'fbase_key' }]);
  await put(fb, 'firebaseLocalStorage', buildAuthRecord());
  return { uid: UID, dbPath: DB_PATH, apiKey: API_KEY, gzBytes: u8.length, gzPath: GZ };
}
