import path from "node:path";
import { withSentryConfig } from "@sentry/nextjs";
import { fileURLToPath } from "node:url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

/** @type {import('next').NextConfig} */
const config = {
  output: "standalone",
  // Required when `output: 'standalone'` runs inside a pnpm monorepo — without
  // this, Next.js scopes its tracing to apps/portal/ and misses workspace deps
  // symlinked from ../../node_modules/. See
  // https://github.com/vercel/next.js/discussions/38435.
  outputFileTracingRoot: path.join(__dirname, "../../"),
  reactStrictMode: true,
  poweredByHeader: false,
  async headers() {
    return [
      {
        source: "/:path*",
        headers: [
          { key: "X-Frame-Options", value: "DENY" },
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
          {
            key: "Permissions-Policy",
            value: "camera=(), microphone=(), geolocation=()",
          },
          // frame-ancestors only: clickjacking protection with zero risk to
          // script/style loading. A full default-src CSP needs per-app testing.
          { key: "Content-Security-Policy", value: "frame-ancestors 'none'" },
        ],
      },
    ];
  },
};

export default withSentryConfig(config, {
  silent: true,
  hideSourceMaps: true,
  automaticVercelMonitors: false,
  disableLogger: true,
});
