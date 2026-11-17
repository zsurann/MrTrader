#!/usr/bin/env python3
"""Pack web/out (Next.js static export) into a single Cloudflare Worker module + the MCP deploy snippet."""
import base64, json, mimetypes, os, pathlib
ROOT = pathlib.Path(__file__).resolve().parent
OUT = ROOT.parent / "web" / "out"
MIME = {".js": "application/javascript; charset=utf-8", ".css": "text/css; charset=utf-8", ".html": "text/html; charset=utf-8",
        ".txt": "text/plain; charset=utf-8", ".svg": "image/svg+xml", ".woff2": "font/woff2", ".json": "application/json", ".ico": "image/x-icon"}
files = {}
for p in sorted(OUT.rglob("*")):
    if p.is_file():
        rel = "/" + p.relative_to(OUT).as_posix()
        files[rel] = [MIME.get(p.suffix, mimetypes.guess_type(p.name)[0] or "application/octet-stream"), base64.b64encode(p.read_bytes()).decode()]
worker = ("// MemeDesk dashboard (static Next.js export) served by a Worker. Read-only UI; data comes from Supabase Realtime.\n"
          "const FILES = " + json.dumps(files) + ";\n" + r'''const bin = (b64) => Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
const CACHE = new Map();
export default {
  async fetch(req) {
    const url = new URL(req.url);
    let p = url.pathname;
    if (p.endsWith("/")) p += "index.html";
    if (!FILES[p] && FILES[p + ".html"]) p += ".html";
    const f = FILES[p] || FILES["/404.html"];
    if (!f) return new Response("not found", { status: 404 });
    let body = CACHE.get(p); if (!body) { body = bin(f[1]); CACHE.set(p, body); }
    const immutable = p.startsWith("/_next/static/");
    return new Response(body, { status: FILES[p] ? 200 : 404, headers: {
      "content-type": f[0],
      "cache-control": immutable ? "public, max-age=31536000, immutable" : "no-cache",
      "x-content-type-options": "nosniff", "referrer-policy": "strict-origin-when-cross-origin",
    }});
  },
};
''')
(ROOT / "worker.js").write_text(worker)
deploy = '''async () => {
  const name = "memedesk-dashboard";
  const worker = ''' + json.dumps(worker) + ''';
  const metadata = { main_module: "worker.js", compatibility_date: "2025-09-01" };
  const form = new FormData();
  form.append("metadata", new Blob([JSON.stringify(metadata)], { type: "application/json" }));
  form.append("worker.js", new Blob([worker], { type: "application/javascript+module" }), "worker.js");
  const up = await cloudflare.request({ method: "PUT", path: `/accounts/${accountId}/workers/scripts/${name}`, body: form });
  return { ok: up.success, id: up.result && up.result.id, etag: up.result && up.result.etag, modified: up.result && up.result.modified_on };
}
'''
(ROOT / "cf_deploy_code.js").write_text(deploy)
print(len(files), "files;", round(len(worker) / 1024), "KB worker")
