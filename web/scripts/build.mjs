#!/usr/bin/env node
// Build wrapper that handles two deploy targets:
//
//   server (default) — full Next.js build with API routes
//   static           — output: "export" for GitHub Pages; API routes
//                      are temporarily moved out of the source tree so
//                      Next doesn't refuse the build.
//
// The static path is restore-on-exit safe: SIGINT, SIGTERM, and uncaught
// errors all trigger the cleanup that puts app/api back where it was.

import { spawnSync } from "node:child_process";
import { existsSync, renameSync } from "node:fs";
import { resolve } from "node:path";

const target = process.env.NEXT_PUBLIC_DEPLOY_TARGET ?? "server";
const ROOT = resolve(import.meta.dirname, "..");
const API_SRC = resolve(ROOT, "app/api");
const API_TMP = resolve(ROOT, ".api.hidden");

let restored = false;
function restore() {
  if (restored) return;
  if (existsSync(API_TMP) && !existsSync(API_SRC)) {
    renameSync(API_TMP, API_SRC);
  }
  restored = true;
}

function hide() {
  if (!existsSync(API_SRC)) return;
  if (existsSync(API_TMP)) {
    // Stale leftover from a prior killed build — recover it first.
    if (!existsSync(API_SRC)) renameSync(API_TMP, API_SRC);
    else throw new Error(`Both ${API_SRC} and ${API_TMP} exist. Resolve manually.`);
  }
  renameSync(API_SRC, API_TMP);
}

process.on("SIGINT", () => { restore(); process.exit(130); });
process.on("SIGTERM", () => { restore(); process.exit(143); });
process.on("exit", restore);

if (target === "static") {
  console.log("[build] target=static — hiding app/api for export build");
  hide();
}

const r = spawnSync(process.execPath, [resolve(ROOT, "node_modules/next/dist/bin/next"), "build"], {
  stdio: "inherit",
  env: process.env,
  cwd: ROOT,
});

restore();
process.exit(r.status ?? 1);
