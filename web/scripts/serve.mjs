#!/usr/bin/env node
// Zero-dependency static file server for the exported Next.js site.
// Usage: node scripts/serve.mjs [port]
//   - Looks in ./out
//   - Adds trailing-slash and index.html resolution
//   - Falls back to 404.html on miss
//   - Picks a free port if the requested one is taken

import http from "node:http";
import { readFile, stat } from "node:fs/promises";
import { extname, join, normalize, resolve, sep } from "node:path";

const ROOT = resolve(process.cwd(), "out");
const REQUESTED_PORT = parseInt(process.argv[2] ?? process.env.PORT ?? "3000", 10);

const MIME = {
  ".html": "text/html; charset=utf-8",
  ".js": "application/javascript; charset=utf-8",
  ".mjs": "application/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".webp": "image/webp",
  ".ico": "image/x-icon",
  ".woff": "font/woff",
  ".woff2": "font/woff2",
  ".ttf": "font/ttf",
  ".txt": "text/plain; charset=utf-8",
  ".map": "application/json; charset=utf-8",
};

async function tryFile(path) {
  try {
    const s = await stat(path);
    if (s.isFile()) return path;
  } catch {
    /* miss */
  }
  return null;
}

async function resolvePath(urlPath) {
  const decoded = decodeURIComponent(urlPath.split("?")[0]);
  const safe = normalize(decoded).replace(/^([./\\])+/, "");
  const target = join(ROOT, safe);
  if (!target.startsWith(ROOT + sep) && target !== ROOT) return null;

  const direct = await tryFile(target);
  if (direct) return direct;

  const indexed = await tryFile(join(target, "index.html"));
  if (indexed) return indexed;

  const html = await tryFile(`${target}.html`);
  if (html) return html;

  return null;
}

const server = http.createServer(async (req, res) => {
  const file = await resolvePath(req.url ?? "/");
  if (!file) {
    const fallback = await tryFile(join(ROOT, "404.html"));
    if (fallback) {
      const body = await readFile(fallback);
      res.writeHead(404, { "content-type": "text/html; charset=utf-8" });
      return res.end(body);
    }
    res.writeHead(404);
    return res.end("Not found");
  }
  const body = await readFile(file);
  const type = MIME[extname(file).toLowerCase()] ?? "application/octet-stream";
  res.writeHead(200, {
    "content-type": type,
    "cache-control": "public, max-age=0, must-revalidate",
  });
  res.end(body);
});

function listen(port) {
  server.listen(port, "0.0.0.0", () => {
    const a = server.address();
    const real = typeof a === "object" && a ? a.port : port;
    console.log(`\n  VOLTFORGE preview ready`);
    console.log(`  http://localhost:${real}\n`);
  });
  server.on("error", (err) => {
    if (err.code === "EADDRINUSE") {
      const next = port + 1;
      console.warn(`  port ${port} busy — trying ${next}`);
      setTimeout(() => listen(next), 50);
    } else {
      console.error(err);
      process.exit(1);
    }
  });
}

stat(ROOT)
  .then(() => listen(REQUESTED_PORT))
  .catch(() => {
    console.error(`\n  No build found at ${ROOT}.`);
    console.error(`  Run "npm run build" first, then "npm run preview".\n`);
    process.exit(1);
  });
