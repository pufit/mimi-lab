// Minimal OPFS (File System Access API subset) backed by disk, for running
// Migaku's Core in Node. Overlay semantics: reads fall through to a read-only
// base (the extension's core/ dir, mounted at /core); writes go to a writable
// temp root, so we never mutate the extension install. Every access is logged.
import fs from 'node:fs';
import fsp from 'node:fs/promises';
import path from 'node:path';

export const opfsLog = [];
const log = (op, p, extra) => { if (opfsLog.length < 5000) opfsLog.push(extra ? `${op} ${p} ${extra}` : `${op} ${p}`); };

function notFound(name) {
  const e = new Error(`OPFS: "${name}" not found`);
  e.name = 'NotFoundError';
  return e;
}
function toBuffer(chunk) {
  if (chunk == null) return Buffer.alloc(0);
  if (typeof chunk === 'string') return Buffer.from(chunk, 'utf8');
  if (chunk instanceof ArrayBuffer) return Buffer.from(new Uint8Array(chunk));
  if (ArrayBuffer.isView(chunk)) return Buffer.from(chunk.buffer, chunk.byteOffset, chunk.byteLength);
  if (typeof chunk.arrayBuffer === 'function') return null; // Blob — handled async by caller
  return Buffer.from(chunk);
}

class NodeFile {
  constructor(p, name) { this._p = p; this.name = name; }
  get size() { try { return fs.statSync(this._p).size; } catch { return 0; } }
  get lastModified() { try { return fs.statSync(this._p).mtimeMs; } catch { return 0; } }
  async arrayBuffer() { const b = await fsp.readFile(this._p); return b.buffer.slice(b.byteOffset, b.byteOffset + b.byteLength); }
  async text() { return fsp.readFile(this._p, 'utf8'); }
  async bytes() { return new Uint8Array(await fsp.readFile(this._p)); }
  stream() { return fs.createReadStream(this._p); }
  slice(start = 0, end) {
    const self = this;
    return { async arrayBuffer() { const b = await fsp.readFile(self._p); const u = b.subarray(start, end ?? b.length); return u.buffer.slice(u.byteOffset, u.byteOffset + u.byteLength); } };
  }
}

class Writable {
  constructor(p, keep) { this._p = p; this._keep = keep; this._fd = null; this._pos = 0; }
  async _ensure() {
    if (this._fd == null) {
      await fsp.mkdir(path.dirname(this._p), { recursive: true });
      const exists = fs.existsSync(this._p);
      this._fd = await fsp.open(this._p, this._keep && exists ? 'r+' : 'w');
    }
  }
  async write(data) {
    await this._ensure();
    if (data && typeof data === 'object' && !(data instanceof ArrayBuffer) && !ArrayBuffer.isView(data) && 'type' in data) {
      if (data.type === 'write') { if (data.position != null) this._pos = data.position; await this._chunk(data.data); }
      else if (data.type === 'seek') { this._pos = data.position; }
      else if (data.type === 'truncate') { await this._fd.truncate(data.size); }
      return;
    }
    await this._chunk(data);
  }
  async _chunk(chunk) {
    let buf = toBuffer(chunk);
    if (buf == null && chunk && typeof chunk.arrayBuffer === 'function') buf = Buffer.from(new Uint8Array(await chunk.arrayBuffer()));
    const { bytesWritten } = await this._fd.write(buf, 0, buf.length, this._pos);
    this._pos += bytesWritten;
  }
  async truncate(size) { await this._ensure(); await this._fd.truncate(size); }
  async seek(pos) { this._pos = pos; }
  async close() { if (this._fd) { await this._fd.close(); this._fd = null; } }
  async abort() { await this.close(); }
}

class SyncAccessHandle {
  constructor(p) { fs.mkdirSync(path.dirname(p), { recursive: true }); this._fd = fs.openSync(p, fs.existsSync(p) ? 'r+' : 'w+'); }
  read(buf, opts = {}) { const v = ArrayBuffer.isView(buf) ? buf : new Uint8Array(buf); const b = Buffer.from(v.buffer, v.byteOffset, v.byteLength); return fs.readSync(this._fd, b, 0, b.length, opts.at ?? 0); }
  write(buf, opts = {}) { const v = ArrayBuffer.isView(buf) ? buf : new Uint8Array(buf); const b = Buffer.from(v.buffer, v.byteOffset, v.byteLength); return fs.writeSync(this._fd, b, 0, b.length, opts.at ?? 0); }
  getSize() { return fs.fstatSync(this._fd).size; }
  truncate(n) { fs.ftruncateSync(this._fd, n); }
  flush() { try { fs.fsyncSync(this._fd); } catch {} }
  close() { try { fs.closeSync(this._fd); } catch {} }
}

class FileHandle {
  constructor(p, name) { this.kind = 'file'; this._p = p; this.name = name; }
  async getFile() { return new NodeFile(this._p, this.name); }
  async createWritable(opts = {}) { return new Writable(this._p, !!opts.keepExistingData); }
  async createSyncAccessHandle() { return new SyncAccessHandle(this._p); }
  async isSameEntry(o) { return !!o && o._p === this._p; }
}

class DirHandle {
  // writePath: writable base; readPath: read-only fallback (or null); mounts: name->readPath at this level
  constructor(writePath, name, readPath = null, mounts = null) {
    this.kind = 'directory'; this.name = name;
    this._w = writePath; this._r = readPath; this._mounts = mounts;
  }
  _readChild(name) {
    if (this._mounts && this._mounts[name]) return this._mounts[name];
    if (this._r) return path.join(this._r, name);
    return null;
  }
  async getDirectoryHandle(name, opts = {}) {
    const w = path.join(this._w, name);
    const r = this._readChild(name);
    const wExists = fs.existsSync(w);
    const rExists = r && fs.existsSync(r);
    log('getDir', name, `w:${wExists} r:${rExists} create:${!!opts.create}`);
    if (!wExists && !rExists) {
      if (opts.create) { await fsp.mkdir(w, { recursive: true }); return new DirHandle(w, name, null); }
      throw notFound(name);
    }
    return new DirHandle(w, name, rExists ? r : null);
  }
  async getFileHandle(name, opts = {}) {
    const w = path.join(this._w, name);
    const r = this._readChild(name);
    if (fs.existsSync(w)) { log('getFile', name, 'w'); return new FileHandle(w, name); }
    if (r && fs.existsSync(r)) { log('getFile', name, 'r'); return new FileHandle(r, name); }
    if (opts.create) { log('getFile', name, 'create'); await fsp.mkdir(path.dirname(w), { recursive: true }); await fsp.writeFile(w, ''); return new FileHandle(w, name); }
    log('getFile', name, 'MISS'); throw notFound(name);
  }
  async removeEntry(name, opts = {}) { log('rm', name); try { await fsp.rm(path.join(this._w, name), { recursive: !!opts.recursive, force: true }); } catch {} }
  async *entries() {
    const seen = new Set();
    for (const base of [this._w, this._r].filter(Boolean)) {
      let ents = [];
      try { ents = await fsp.readdir(base, { withFileTypes: true }); } catch {}
      for (const e of ents) {
        if (seen.has(e.name)) continue; seen.add(e.name);
        const child = path.join(base, e.name);
        yield [e.name, e.isDirectory() ? new DirHandle(path.join(this._w, e.name), e.name, this._readChild(e.name)) : new FileHandle(child, e.name)];
      }
    }
  }
  async *keys() { for await (const [k] of this.entries()) yield k; }
  async *values() { for await (const [, v] of this.entries()) yield v; }
  [Symbol.asyncIterator]() { return this.entries(); }
  async resolve() { return null; }
  async isSameEntry(o) { return !!o && o._w === this._w; }
}

// Build navigator.storage. rootWrite = writable temp dir; readBase = read-only
// fallback for the whole root (the ext core/ dir); mounts = {name: readPath}.
export function makeStorage(rootWrite, readBase = null, mounts = null) {
  fs.mkdirSync(rootWrite, { recursive: true });
  const root = new DirHandle(rootWrite, '', readBase, mounts);
  return {
    getDirectory: async () => root,
    estimate: async () => ({ usage: 0, quota: 1e12 }),
    persist: async () => true,
    persisted: async () => true,
  };
}
