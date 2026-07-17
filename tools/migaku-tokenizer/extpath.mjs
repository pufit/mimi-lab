// Resolve the installed Migaku Early-Access extension on disk and expose its
// asset/core directories + the (hash-named) analyzer bundle.
//
// SNAPSHOTTING (the drift guard): Chrome auto-updates the extension and PRUNES
// old version dirs — under a long-running sidecar whose temp dir symlinks into
// the live install, that could break mid-run, and "pin a known-good version"
// was documented advice with no implementation. resolveExt() now copies the
// resolved version into data/migaku-ext-snapshot/<version>/ once and loads
// from the snapshot. Pinning: MIGAKU_EXT_VERSION=1.31.0.6 selects a specific
// (already snapshotted or installed) version; unset = highest installed.
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import { fileURLToPath } from 'node:url';

const EXT_ID = process.env.MIGAKU_EXT_ID || 'dmeppfcidcpcocleneopiblmpnbokhep';
const PROFILE = process.env.MIGAKU_CDP_PROFILE ||
  path.join(os.homedir(), '.migaku-cdp', 'Default', 'Extensions', EXT_ID);
const PIN = process.env.MIGAKU_EXT_VERSION || null;
const SNAP_ROOT = process.env.MIGAKU_EXT_SNAPSHOT_DIR ||
  path.join(path.dirname(fileURLToPath(import.meta.url)), '..', '..', 'data', 'migaku-ext-snapshot');

function cmpVersion(a, b) {
  const pa = a.split('.').map(Number), pb = b.split('.').map(Number);
  for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
    const d = (pb[i] || 0) - (pa[i] || 0);
    if (d) return d;
  }
  return 0;
}

function usable(dir) {
  return fs.existsSync(path.join(dir, 'assets')) && fs.existsSync(path.join(dir, 'core'));
}

function listVersions(root) {
  if (!fs.existsSync(root)) return [];
  return fs.readdirSync(root)
    .filter((d) => usable(path.join(root, d)))
    .sort((a, b) => cmpVersion(a.replace(/_0$/, ''), b.replace(/_0$/, '')));
}

// core/ ships per-language dictionary blobs (es.json alone is 334 MB); the JA
// analyzer only needs the kuromoji .bin files, models_light/, and ja.* — skip
// every other language's json/db so a snapshot is ~150 MB instead of 1 GB.
const SKIP_CORE_FILE = /^(?!ja[._])[a-z]{2,3}(_[a-z]+)?\.(json|db)$/i;

function snapshot(liveBase, version) {
  const dst = path.join(SNAP_ROOT, version);
  const marker = path.join(dst, '.complete');
  if (fs.existsSync(marker)) return dst;
  fs.rmSync(dst, { recursive: true, force: true });
  fs.mkdirSync(SNAP_ROOT, { recursive: true });
  fs.cpSync(path.join(liveBase, 'assets'), path.join(dst, 'assets'), { recursive: true });
  fs.cpSync(path.join(liveBase, 'core'), path.join(dst, 'core'), {
    recursive: true,
    filter: (src) => {
      const rel = path.basename(src);
      return !(fs.statSync(src).isFile() && SKIP_CORE_FILE.test(rel));
    },
  });
  for (const f of ['manifest.json']) {
    const src = path.join(liveBase, f);
    if (fs.existsSync(src)) fs.cpSync(src, path.join(dst, f));
  }
  fs.writeFileSync(marker, new Date().toISOString());
  // keep at most 2 snapshots (current + previous known-good)
  try {
    const snaps = listVersions(SNAP_ROOT);
    for (const old of snaps.slice(2)) {
      fs.rmSync(path.join(SNAP_ROOT, old), { recursive: true, force: true });
      console.error(`[extpath] pruned old snapshot ${old}`);
    }
  } catch {}
  console.error(`[extpath] snapshotted Migaku ${version} -> ${dst}`);
  return dst;
}

export function resolveExt() {
  const live = listVersions(PROFILE);
  const snapped = listVersions(SNAP_ROOT);

  let version = PIN;
  if (version) {
    if (!snapped.includes(version) && !live.includes(version)) {
      throw new Error(`MIGAKU_EXT_VERSION=${version} is neither snapshotted nor installed ` +
                      `(snapshots: ${snapped.join(', ') || 'none'}; installed: ${live.join(', ') || 'none'})`);
    }
  } else {
    version = live[0] || snapped[0];
    if (!version) throw new Error(`no usable Migaku extension under ${PROFILE} or ${SNAP_ROOT}`);
  }

  // prefer the snapshot; create it from the live install when possible
  let base = path.join(SNAP_ROOT, version);
  if (!fs.existsSync(path.join(base, '.complete'))) {
    const liveBase = path.join(PROFILE, version);
    if (usable(liveBase)) {
      try {
        base = snapshot(liveBase, version);
      } catch (e) {
        console.error(`[extpath] snapshot failed (${e.message}); using live dir`);
        base = liveBase;
      }
    } else if (!usable(base)) {
      throw new Error(`version ${version} not usable in live install or snapshot`);
    }
  }

  const assets = path.join(base, 'assets');
  const core = path.join(base, 'core');
  const bundle = fs.readdirSync(assets).find((f) => /^player-store-[0-9a-f]+\.js$/.test(f));
  if (!bundle) throw new Error(`player-store bundle not found in ${assets}`);
  return {
    version,
    base,
    assets,
    core,
    bundle: path.join(assets, bundle),
    snapshotted: base.startsWith(SNAP_ROOT),
  };
}

if (import.meta.url === `file://${process.argv[1]}`) {
  console.log(JSON.stringify(resolveExt(), null, 2));
}
