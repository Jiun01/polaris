/**
 * Portal global setup — mints NextAuth session cookies for all 5 tenant
 * admins via magic-link (MailHog). Runs once before the portal E2E suite.
 *
 * Required env var (set in .env or shell):
 *   MAILHOG_API_URL   http://localhost:8025  (also accepted without trailing slash)
 *
 * Output — process.env keys set for the test worker process:
 *   PLAYWRIGHT_STORAGE_STATE_ULTRA      ./e2e/.auth/ultra.json
 *   PLAYWRIGHT_STORAGE_STATE_STARTER    ./e2e/.auth/starter-a.json
 *   PLAYWRIGHT_STORAGE_STATE_STARTER_B  ./e2e/.auth/starter-b.json
 *   PLAYWRIGHT_STORAGE_STATE_PRO        ./e2e/.auth/pro-c.json
 *   PLAYWRIGHT_STORAGE_STATE_PRO_D      ./e2e/.auth/pro-d.json
 *
 * PLAYWRIGHT_STORAGE_STATE_STARTER and _PRO are also used by the
 * existing sidebar-tier-gating.spec.ts — set to the same files here.
 *
 * Gracefully skips when MAILHOG_API_URL is unset (local dev without MailHog).
 * State files are reused if < 1 h old to speed up repeated local runs.
 */

import * as fs from "fs";
import * as path from "path";

import { chromium, type FullConfig } from "@playwright/test";

const MAILHOG_API = process.env.MAILHOG_API_URL;
// Use portal.localhost (via Caddy) because NEXTAUTH_URL is portal.localhost —
// the session cookie is always minted for that origin. Tests must also use
// portal.localhost as baseURL (set PLAYWRIGHT_BASE_URL=http://portal.localhost
// or leave it unset to get this default).
// Must match playwright.config.ts baseURL — the magic-link redeems on the
// portal's NEXTAUTH_URL origin (base-dev: http://localhost:3000), so the saved
// session-token cookie is scoped to that origin. Minting against a different
// origin yields a cookie the tests can't use.
const BASE_URL =
  process.env.PLAYWRIGHT_PORTAL_BASE_URL ??
  process.env.PLAYWRIGHT_BASE_URL ??
  "http://localhost:3000";
const STATE_TTL_MS = 8 * 60 * 60 * 1_000; // 8 hours — NextAuth sessions outlive 1 h easily

const USERS: Array<{
  email: string;
  stateEnvKey: string;
  stateFile: string;
  /** Path the magic-link redeem lands on. Default /dashboard; a payment_overdue
   *  tenant is route-taken-over to /pay by the middleware. */
  landingUrl?: string;
}> = [
  {
    email: "owner@getpolaris.my",
    stateEnvKey: "PLAYWRIGHT_STORAGE_STATE_ULTRA",
    stateFile: "./e2e/.auth/ultra.json",
  },
  {
    email: "admin-a@e2e.polaris-local.dev",
    stateEnvKey: "PLAYWRIGHT_STORAGE_STATE_STARTER",
    stateFile: "./e2e/.auth/starter-a.json",
  },
  {
    email: "admin-b@e2e.polaris-local.dev",
    stateEnvKey: "PLAYWRIGHT_STORAGE_STATE_STARTER_B",
    stateFile: "./e2e/.auth/starter-b.json",
  },
  {
    email: "admin-c@e2e.polaris-local.dev",
    stateEnvKey: "PLAYWRIGHT_STORAGE_STATE_PRO",
    stateFile: "./e2e/.auth/pro-c.json",
  },
  {
    email: "admin-d@e2e.polaris-local.dev",
    stateEnvKey: "PLAYWRIGHT_STORAGE_STATE_PRO_D",
    stateFile: "./e2e/.auth/pro-d.json",
  },
  {
    // payment_overdue tenant — redeem lands on /pay (middleware route-takeover).
    email: "admin-e@e2e.polaris-local.dev",
    stateEnvKey: "PLAYWRIGHT_STORAGE_STATE_OVERDUE",
    stateFile: "./e2e/.auth/overdue.json",
    landingUrl: "/pay",
  },
];

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
      // Decode quoted-printable encoding (soft line breaks + hex escapes)
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
    `[portal-setup] No magic-link for ${email} within ${timeoutMs}ms`,
  );
}

async function mintAuthState(
  mailhogApi: string,
  email: string,
  stateFile: string,
  landingUrl = "/dashboard",
): Promise<void> {
  const statePath = path.resolve(stateFile);

  if (fs.existsSync(statePath)) {
    const ageMs = Date.now() - fs.statSync(statePath).mtimeMs;
    if (ageMs < STATE_TTL_MS) {
      console.log(
        `[portal-setup] ${email}: state ${Math.round(ageMs / 1_000)}s old — reusing`,
      );
      return;
    }
  }

  console.log(`[portal-setup] Minting auth state for ${email} ...`);

  // Flush MailHog before requesting a link to avoid picking up stale messages.
  await fetch(`${mailhogApi}/api/v1/messages`, { method: "DELETE" });

  const browser = await chromium.launch();
  const context = await browser.newContext();
  const page = await context.newPage();

  try {
    // Prime the CSRF cookie before hitting /login (mirrors admin global-setup).
    await page.goto(`${BASE_URL}/api/auth/csrf`);
    await page.goto(`${BASE_URL}/login`);

    await page.getByLabel(/email/i).fill(email);
    await page.getByRole("button", { name: /send.*link/i }).click();
    await page.waitForURL(/\/verify-request/, { timeout: 15_000 });

    const link = await pollForMagicLink(mailhogApi, email);
    console.log(`[portal-setup] Magic-link received for ${email}`);

    await page.goto(link, { waitUntil: "commit", timeout: 30_000 });
    await page.waitForURL(new RegExp(landingUrl.replace(/\//g, "\\/")), {
      timeout: 20_000,
      waitUntil: "commit",
    });

    const stateDir = path.dirname(statePath);
    if (!fs.existsSync(stateDir)) fs.mkdirSync(stateDir, { recursive: true });

    await context.storageState({ path: stateFile });
    console.log(`[portal-setup] Saved → ${stateFile}`);
  } finally {
    await context.close();
    await browser.close();
  }
}

export default async function globalSetup(_config: FullConfig): Promise<void> {
  if (!MAILHOG_API) {
    console.log(
      "[portal-setup] MAILHOG_API_URL not set — skipping auth-state generation.",
    );
    return;
  }

  for (const user of USERS) {
    try {
      await mintAuthState(
        MAILHOG_API,
        user.email,
        user.stateFile,
        user.landingUrl,
      );
      process.env[user.stateEnvKey] = user.stateFile;
    } catch (err) {
      // A single flaky magic-link mint must NOT abort the whole suite — the
      // affected tenant's specs guard on `!t.state` and self-skip. Log and move on.
      console.warn(
        `[portal-setup] WARN: could not mint state for ${user.email} — its specs will skip. ${(err as Error).message}`,
      );
    }
  }
}
