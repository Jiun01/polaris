/**
 * Playwright global setup — admin auth state (Phase 9 VPS-mimic E2E).
 *
 * Signs in once via the admin magic-link flow and persists the NextAuth
 * session cookie to PLAYWRIGHT_ADMIN_STORAGE_STATE. Specs that need an
 * authenticated session load it with:
 *
 *   import { ADMIN_STATE } from "./helpers/admin-auth";
 *   test.use({ storageState: ADMIN_STATE ?? "" });
 *
 * Prerequisites before running:
 *   1. Stack up:     make dev-vps
 *   2. Seed admin:   uv run python apps/api/scripts/seed_e2e_admin.py
 *   3. Set env vars  (see playwright.config.ts docblock or .env)
 *
 * Gracefully skips when ADMIN_MAILHOG_API_URL or
 * PLAYWRIGHT_ADMIN_STORAGE_STATE is unset (plain direct-port dev without
 * MailHog).
 *
 * VPS-mimic env vars:
 *   PLAYWRIGHT_ADMIN_BASE_URL      http://admin.localhost
 *   PLAYWRIGHT_ADMIN_HTTP_USER     admin
 *   PLAYWRIGHT_ADMIN_HTTP_PASS     polaris-local-dev
 *   ADMIN_MAILHOG_API_URL          http://localhost:8025
 *   PLAYWRIGHT_ADMIN_STORAGE_STATE ./e2e/.auth/admin.json
 *   E2E_ADMIN_EMAIL                e2e-admin@polaris-local.dev
 */

import * as fs from "fs";
import * as path from "path";
import { spawnSync } from "child_process";

import { chromium, type FullConfig } from "@playwright/test";

/**
 * Reset EA test state before each Playwright run.
 * - Clears file-based rate-limit counters (limit=10/hr; accumulates across runs)
 * - Deletes all appointments for provider-polaris (id=39) so the booking flow
 *   test never hits a "slot already taken" failure on repeated runs.
 * Failures are non-fatal — if Docker is unavailable the tests degrade gracefully.
 */
function cleanupEATestData(): void {
  const containers = {
    phpfpm: "polaris-ea-fork-php-fpm-1",
    mysql: "polaris-ea-fork-mysql-1",
  };
  // Use spawnSync with explicit arg arrays to avoid Windows cmd.exe quoting issues.
  const rl = spawnSync(
    "docker",
    [
      "exec",
      containers.phpfpm,
      "sh",
      "-c",
      "rm -f /var/www/html/storage/cache/rate_limit_key_* /var/www/html/storage/cache/rate_limit_tmp_*",
    ],
    { stdio: "pipe" },
  );
  if (rl.status === 0) {
    console.log("[global-setup] EA rate-limit counters cleared.");
  } else {
    console.log(
      "[global-setup] EA rate-limit reset skipped (Docker unavailable or error).",
    );
  }

  const db = spawnSync(
    "docker",
    [
      "exec",
      containers.mysql,
      "sh",
      "-c",
      "mysql -u user -ppassword easyappointments -e 'DELETE FROM ea_appointments WHERE id_users_provider=39'",
    ],
    { stdio: "pipe" },
  );
  if (db.status === 0) {
    console.log("[global-setup] EA provider-39 appointments deleted.");
  } else {
    console.log(
      "[global-setup] EA appointment cleanup skipped (Docker unavailable or error).",
    );
  }
}

const MAILHOG_API = process.env.ADMIN_MAILHOG_API_URL;
const STORAGE_STATE = process.env.PLAYWRIGHT_ADMIN_STORAGE_STATE;
const E2E_ADMIN_EMAIL =
  process.env.E2E_ADMIN_EMAIL ?? "e2e-admin@polaris-local.dev";

/**
 * Poll MailHog until a NextAuth v4 magic-link email arrives for `email`.
 *
 * NextAuth v4 callback URL shape:
 *   http://admin.localhost/api/auth/callback/email?token=…&email=…
 *
 * MailHog's Content.Body contains the raw MIME body — the URL appears both
 * in the text/plain section (bare URL) and the text/html section (inside
 * href="…"). The regex terminates on whitespace and HTML delimiter chars so
 * it extracts the full query-string from both positions correctly.
 */
async function pollForMagicLink(
  mailhogApi: string,
  email: string,
  timeoutMs = 30_000,
): Promise<string> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    await new Promise<void>((r) => setTimeout(r, 1_500));
    const res = await fetch(
      `${mailhogApi}/api/v2/search?kind=to&query=${encodeURIComponent(email)}`,
    );
    const body = (await res.json()) as {
      items: { Content: { Body: string } }[];
    };
    if (body.items.length > 0) {
      // The email body uses Content-Transfer-Encoding: quoted-printable.
      // QP wraps long lines with `=\r\n` soft line breaks and encodes `=`
      // as `=3D`. Decode both before running the URL regex so the full
      // magic-link URL (token + email params) is captured in one pass.
      const rawBody = body.items[0].Content.Body;
      const decoded = rawBody
        .replace(/=\r\n/g, "")
        .replace(/=\n/g, "")
        .replace(/=([0-9A-Fa-f]{2})/g, (_: string, hex: string) =>
          String.fromCharCode(parseInt(hex, 16)),
        );
      const match = decoded.match(
        /https?:\/\/[^\s"<>]+api\/auth\/callback\/email[^\s"<>]*/,
      );
      if (match) return match[0];
    }
  }
  throw new Error(
    `[global-setup] No admin magic-link arrived for ${email} within ${timeoutMs}ms`,
  );
}

export default async function globalSetup(_config: FullConfig): Promise<void> {
  cleanupEATestData();

  if (!MAILHOG_API || !STORAGE_STATE) {
    // Not a failure — just direct-port mode or CI without MailHog.
    console.log(
      "[global-setup] ADMIN_MAILHOG_API_URL or PLAYWRIGHT_ADMIN_STORAGE_STATE not set" +
        " — skipping auth-state generation.",
    );
    return;
  }

  const baseURL =
    process.env.PLAYWRIGHT_ADMIN_BASE_URL ?? "http://localhost:3001";
  const httpUser = process.env.PLAYWRIGHT_ADMIN_HTTP_USER;
  const httpPass = process.env.PLAYWRIGHT_ADMIN_HTTP_PASS ?? "";

  // Ensure the storage-state directory exists (e2e/.auth/ is gitignored).
  const stateDir = path.dirname(path.resolve(STORAGE_STATE));
  if (!fs.existsSync(stateDir)) {
    fs.mkdirSync(stateDir, { recursive: true });
  }

  // Re-use a fresh auth-state file (< 1 hour old) to avoid intermittent
  // CSRF-cookie race conditions on back-to-back runs and to speed up
  // `pnpm e2e --grep ...` invocations that just want a subset of tests.
  const statePath = path.resolve(STORAGE_STATE);
  const STATE_TTL_MS = 60 * 60 * 1_000; // 1 hour
  if (fs.existsSync(statePath)) {
    const ageMsRaw = Date.now() - fs.statSync(statePath).mtimeMs;
    if (ageMsRaw < STATE_TTL_MS) {
      console.log(
        `[global-setup] Auth state file is ${Math.round(ageMsRaw / 1_000)}s old — reusing (TTL ${STATE_TTL_MS / 60_000} min).`,
      );
      return;
    }
  }

  console.log(
    `[global-setup] Generating admin auth state for ${E2E_ADMIN_EMAIL} via ${baseURL} ...`,
  );

  // Flush MailHog so we don't pick up a stale link from a previous run.
  await fetch(`${MAILHOG_API}/api/v1/messages`, { method: "DELETE" });

  const browser = await chromium.launch();
  const contextOptions: Parameters<typeof browser.newContext>[0] = {};
  if (httpUser) {
    // Caddy basic-auth: Playwright auto-responds to WWW-Authenticate: Basic.
    contextOptions.httpCredentials = { username: httpUser, password: httpPass };
  }
  const context = await browser.newContext(contextOptions);
  const page = await context.newPage();

  try {
    // 1a. Prime the next-auth.csrf-token cookie in the browser by hitting
    //     the CSRF endpoint directly. Without this, the browser has no cookie
    //     on the first visit to /login. The login page's SSR forwards the
    //     browser's cookie to its internal CSRF fetch and returns the matching
    //     token — but only if that cookie already exists. This request sets it.
    await page.goto(`${baseURL}/api/auth/csrf`);

    // 1b. Load the login page (through Caddy basic-auth if in VPS mode).
    //     SSR now forwards the primed cookie → returns the matching csrfToken.
    await page.goto(`${baseURL}/login`);

    // 2. Submit the E2E admin email.
    await page.getByLabel(/admin email/i).fill(E2E_ADMIN_EMAIL);
    await page.getByRole("button", { name: /send sign-in link/i }).click();

    // 3. Wait for NextAuth's verify-request page.
    await page.waitForURL(/\/verify-request/, { timeout: 15_000 });

    // 4. Poll MailHog for the one-shot magic link.
    const link = await pollForMagicLink(MAILHOG_API, E2E_ADMIN_EMAIL);
    console.log("[global-setup] Magic-link received, navigating...");

    // 5. Click the magic link.  The link URL already contains admin.localhost
    //    so the same httpCredentials pass Caddy basic-auth automatically.
    //    Caddy injects X-Polaris-Caddy-Token + X-Real-IP; NextAuth IP-bind
    //    passes because both the sign-in request and the callback come from
    //    the same host-machine docker-gateway IP.
    await page.goto(link, { waitUntil: "commit", timeout: 30_000 });
    console.log(`[global-setup] After magic-link goto, URL: ${page.url()}`);
    // Wait only until the navigation commits (headers received, URL set).
    // "load" would wait for the entire /tenants page to SSR + fetch API data
    // which can take many seconds. The session cookie is already set by the
    // time the callback redirects to /tenants, so committing is sufficient.
    try {
      await page.waitForURL(/\/tenants/, {
        timeout: 20_000,
        waitUntil: "commit",
      });
    } catch {
      console.error(
        `[global-setup] waitForURL(/tenants/) timed out. Current URL: ${page.url()}`,
      );
      throw new Error(`[global-setup] Expected /tenants, got: ${page.url()}`);
    }

    // 6. Persist cookies so specs can load them via test.use({ storageState }).
    await context.storageState({ path: STORAGE_STATE });
    console.log(`[global-setup] Auth state saved to ${STORAGE_STATE}`);
  } finally {
    await context.close();
    await browser.close();
  }
}
