/**
 * Playwright config for the Polaris admin E2E suite (Phase 9 PR-12).
 *
 * Mirrors apps/portal/playwright.config.ts with admin-specific defaults:
 *   - baseURL defaults to http://localhost:3001 (admin port)
 *   - storageState fixtures for platform_admin and synthetic tenant sessions
 *   - MailHog-gated auth spec (ADMIN_MAILHOG_API_URL)
 *
 * The admin dev server is assumed to be running externally. In CI the
 * workflow does `pnpm --filter @polaris/admin dev &` and waits for 3001.
 * Locally `pnpm e2e` uses whatever is at PLAYWRIGHT_ADMIN_BASE_URL.
 *
 * VPS-mimic mode (make dev-vps):
 *   PLAYWRIGHT_ADMIN_BASE_URL=http://admin.localhost
 *   PLAYWRIGHT_ADMIN_HTTP_USER=admin          ← Caddy basic-auth username
 *   PLAYWRIGHT_ADMIN_HTTP_PASS=polaris-local-dev
 *   ADMIN_MAILHOG_API_URL=http://localhost:8025
 *   PLAYWRIGHT_ADMIN_STORAGE_STATE=./e2e/.auth/admin.json
 *   E2E_ADMIN_EMAIL=e2e-admin@polaris-local.dev
 *
 * global-setup.ts fires on every `pnpm e2e` run and signs in once via
 * the magic-link flow to generate the storage-state file. It is a no-op
 * when ADMIN_MAILHOG_API_URL or PLAYWRIGHT_ADMIN_STORAGE_STATE is unset.
 */

import * as fs from "fs";
import * as path from "path";

// Load root .env for VPS-mimic E2E vars (PLAYWRIGHT_*, ADMIN_MAILHOG_API_URL,
// E2E_ADMIN_EMAIL) without adding a dotenv dependency. Only sets vars that are
// not already set so explicit environment variables always take precedence.
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
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 1,
  workers: process.env.CI ? 1 : undefined,
  reporter: process.env.CI ? "github" : "list",

  // Runs once before the test suite; generates admin auth storage state via
  // MailHog. Skipped entirely for prod-smoke (PROD_SMOKE=1) — that project
  // reuses a pre-captured real-login storageState instead (no MailHog on prod).
  globalSetup: process.env.PROD_SMOKE ? undefined : "./e2e/global-setup.ts",

  use: {
    baseURL: process.env.PLAYWRIGHT_ADMIN_BASE_URL ?? "http://localhost:3001",

    // Caddy basic-auth pass-through in VPS-mimic mode.
    // Playwright responds to WWW-Authenticate: Basic challenges automatically
    // when httpCredentials are set.
    ...(process.env.PLAYWRIGHT_ADMIN_HTTP_USER
      ? {
          httpCredentials: {
            username: process.env.PLAYWRIGHT_ADMIN_HTTP_USER,
            password: process.env.PLAYWRIGHT_ADMIN_HTTP_PASS ?? "",
          },
        }
      : {}),

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
      // Run with: PROD_SMOKE=1 PLAYWRIGHT_ADMIN_BASE_URL=https://admin.getpolaris.my
      //   npx playwright test --project=prod-smoke
      name: "prod-smoke",
      testDir: "./e2e/prod-smoke",
      // Override the inherited top-level exclusion — it targets THIS dir.
      testIgnore: [],
      use: {
        ...devices["Desktop Chrome"],
        storageState: "./e2e/.auth/prod-admin.json",
      },
    },
  ],
});
