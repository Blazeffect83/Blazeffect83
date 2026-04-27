/** @type {import('next').NextConfig} */
// VOLTFORGE supports two build modes via NEXT_PUBLIC_DEPLOY_TARGET:
//   "static"  — output: "export" for GitHub Pages / any static host (no API routes, localStorage only)
//   default   — full Next.js server with API routes (Pi / Vercel / any Node host)

const target = process.env.NEXT_PUBLIC_DEPLOY_TARGET ?? "server";
const basePath = process.env.NEXT_PUBLIC_BASE_PATH ?? "";
const isStatic = target === "static";

const nextConfig = {
  reactStrictMode: true,
  ...(isStatic ? { output: "export", trailingSlash: true } : {}),
  basePath,
  assetPrefix: basePath || undefined,
  images: { unoptimized: true },
};

export default nextConfig;
