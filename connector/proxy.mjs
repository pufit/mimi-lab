// proxy.mjs — loopback media proxy.
//
// Migaku's Player is a secure context (chrome-extension://). It will NOT fetch
// plain http:// from a non-loopback host (mixed content) — but http://127.0.0.1
// IS exempt. So instead of having the Player fetch the server directly (which
// would force HTTPS for any remote/LAN server), the Connector runs this tiny
// loopback server: the Player fetches 127.0.0.1, and WE relay the bytes from the
// real server over the LAN (Node has no mixed-content restriction).
//
// Net effect: plain http on the LAN just works — no TLS, no Tailscale, no certs.
// Range requests are forwarded transparently (so big files + seeking work).
import http from 'node:http';
import { Readable } from 'node:stream';

const COPY_HEADERS = [
  'content-type', 'content-length', 'content-range',
  'accept-ranges', 'content-disposition', 'cache-control',
];

/** True iff `target` has the exact same origin (scheme://host:port) as `base`. */
function sameOrigin(target, base) {
  try {
    const a = new URL(target), b = new URL(base);
    return a.protocol === b.protocol && a.host === b.host;  // host includes port
  } catch {
    return false;
  }
}

/** Start the loopback proxy on 127.0.0.1:cfg.proxyPort. Resolves to the server. */
export function startMediaProxy(cfg) {
  const server = http.createServer(async (req, res) => {
    try {
      const url = new URL(req.url, 'http://127.0.0.1');
      if (url.pathname !== '/m') { res.writeHead(404); res.end('not found'); return; }
      const enc = url.searchParams.get('u');
      if (!enc) { res.writeHead(400); res.end('missing u'); return; }
      const target = Buffer.from(enc, 'base64url').toString('utf8');
      // SSRF guard: only ever relay to the configured Lab server. Compare the
      // parsed ORIGIN (scheme+host+port), not a string prefix — a prefix check
      // is trivially bypassed by `http://server:8000@evil.com/…` (userinfo) or
      // `http://server:8000.evil.com/…` (subdomain), turning this into an open
      // relay to arbitrary hosts.
      if (!sameOrigin(target, cfg.serverUrl)) { res.writeHead(403); res.end('forbidden'); return; }

      const headers = {};
      if (req.headers.range) headers.range = req.headers.range;
      const up = await fetch(target, { method: req.method === 'HEAD' ? 'HEAD' : 'GET', headers });

      const out = {};
      for (const h of COPY_HEADERS) { const v = up.headers.get(h); if (v) out[h] = v; }
      res.writeHead(up.status, out);
      if (req.method === 'HEAD' || !up.body) { res.end(); return; }
      Readable.fromWeb(up.body).pipe(res);
    } catch (e) {
      if (!res.headersSent) res.writeHead(502);
      res.end('proxy error: ' + (e && e.message ? e.message : String(e)));
    }
  });
  return new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(cfg.proxyPort, '127.0.0.1', () => resolve(server));
  });
}

/** Rewrite a server media URL to go through the loopback proxy (or null). */
export function proxify(cfg, originalUrl) {
  if (!originalUrl) return null;
  const enc = Buffer.from(originalUrl, 'utf8').toString('base64url');
  return `http://127.0.0.1:${cfg.proxyPort}/m?u=${enc}`;
}
