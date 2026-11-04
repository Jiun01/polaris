/**
 * Playwright config for the Polaris portal E2E suite (Phase 8 PR-12).
 *
 * Conservative defaults — small worker count, retries on CI only,
 * artifact upload on failure (trace + video). The portal dev server
 * is assumed to be running externally (the CI workflow does
 * `pnpm dev &`); locally `pnpm e2e` will use whatever's at
 * `PLAYWRIGHT_BASE_URL` (defaults to http://portal.localhost).
 *
 * VPS-mimic env vars (loaded from root ../../.env):
 *   PLAYWRIGHT_BASE_URL    http://portal.localhost
 *   MAILHOG_API_URL        http://localhost:8025
 */

import * as fs from "fs";
import * as path from "path";

// Load root .env for VPS-mimic E2E vars without adding a dotenv dependency.
// Only sets vars not already set so explicit env always wins.
const rootEnv = path.resolve(__dirname, "../../.env");
if (fs.existsSync(rootEnv)) {
  for (const line of fs.readFileSync(rootEnv, "utf-8").split("\n")) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#")) continue;
    const eq = trimmed.indexOf("=");
    if (eq < 1) continue;
    const key = trimmed.slice(0, eq).trim();
    const val = trimmed.slice(eq + 1).trim();
    if (!(key in process.env)) process.env[key] = val;
  }
}

import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  // prod-smoke has its own testDir below and must never be picked up by the
  // default "chromium" project (which inherits this testIgnore) — it targets
  // real prod URLs with real auth, not the local/CI fixture stack.
  testIgnore: ["**/_legacy/**", "**/prod-smoke/**"],
  // Skipped entirely for prod-smoke (PROD_SMOKE=1) — that project reuses a
  // pre-captured real-login storageState instead (no MailHog on prod).
  globalSetup: process.env.PROD_SMOKE ? undefined : "./e2e/global-setup.ts",
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  workers: process.env.CI ? 1 : undefined,
  reporter: process.env.CI ? "github" : "list",
  use: {
    // Portal auth (NextAuth) lives wherever NEXTAUTH_URL points. In base-dev
    // docker mode that is http://localhost:3000 (session cookie domain), so the
    // session-token cookie is only sent to that origin. Prefer the dedicated
    // PLAYWRIGHT_PORTAL_BASE_URL (set in root .env), then the generic override,
    // then the base-dev default. Set PLAYWRIGHT_PORTAL_BASE_URL=http://portal.localhost
    // for VPS-mimic (Caddy) runs.
    baseURL:
      process.env.PLAYWRIGHT_PORTAL_BASE_URL ??
      process.env.PLAYWRIGHT_BASE_URL ??
      "http://localhost:3000",
    trace: "on-first-retry",
    video: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
    {
      // Real-prod verification suite — see e2e/prod-smoke/README.md.
      // Run with: PROD_SMOKE=1 PLAYWRIGHT_PORTAL_BASE_URL=https://portal.getpolaris.my
      //   npx playwright test --project=prod-smoke
      name: "prod-smoke",
      testDir: "./e2e/prod-smoke",
      // Override the inherited top-level exclusion — it targets THIS dir.
      testIgnore: [],
      use: {
        ...devices["Desktop Chrome"],
        storageState: "./e2e/.auth/prod-tech-tenant.json",
      },
    },
  ],
});
