// One-time interactive auth capture for the prod-smoke Playwright suite (portal).
//
// Launches a FRESH, throwaway browser session using the real installed Chrome
// binary (channel: "chrome") rather than Playwright's bundled Chromium — same
// look/behavior as your everyday browser, but a brand-new empty context with
// zero access to your real profile, cookies, or any other logged-in site.
// Nothing persists here except what gets written out at the end.
//
// admin.getpolaris.my is handled separately, by you, per
// apps/admin/e2e/prod-smoke/README.md (it sits behind a Caddy Basic Auth gate
// that a scripted/automated browser can't click through).
//
// Run from apps/portal (so @playwright/test resolves from its node_modules):
//   node e2e/prod-smoke/_capture-auth.mjs
//
// Not a test file (underscore prefix) — Playwright's test runner ignores it.

import { chromium } from "@playwright/test";
import path from "path";
import { fileURLToPath } from "url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(__dirname, "../../../..");
const PORTAL_STATE_PATH = path.resolve(
  REPO_ROOT,
  "apps/portal/e2e/.auth/prod-tech-tenant.json",
);

const AUTH_WAIT_TIMEOUT_MS = 10 * 60 * 1000; // 10 minutes to check email + click

function log(msg) {
  console.log(`[capture-auth] ${msg}`);
}

async function main() {
  const browser = await chromium.launch({ channel: "chrome", headless: false });
  const context = await browser.newContext();

  // Orientation windows — "open up all the base pages" per request. No auth needed.
  for (const url of [
    "https://chat.getpolaris.my",
    "https://calendar.getpolaris.my",
  ]) {
    const p = await context.newPage();
    log(`orientation: opening ${url}`);
    await p
      .goto(url)
      .catch((e) => log(`orientation: ${url} failed to load — ${e.message}`));
  }

  const page = await context.newPage();
  const loginUrl = "https://portal.getpolaris.my/login";
  log(`portal: opening ${loginUrl}`);
  await page.goto(loginUrl);

  log(
    `portal: waiting up to ${AUTH_WAIT_TIMEOUT_MS / 60000} min for you to log in as the ` +
      `gateway tenant admin (enter your email, submit, check your real inbox, ` +
      `click the magic link in THIS window)…`,
  );
  try {
    await page.waitForURL(/\/dashboard/, { timeout: AUTH_WAIT_TIMEOUT_MS });
  } catch {
    log(
      "portal: TIMED OUT waiting for login. Leaving the window open — finish logging in, then re-run this script.",
    );
    return;
  }

  log("portal: authenticated.");
  await context.storageState({ path: PORTAL_STATE_PATH });
  log(`Saved storageState to: ${PORTAL_STATE_PATH}`);
  await browser.close();
  log("Done.");
  process.exit(0);
}

main().catch((err) => {
  console.error("[capture-auth] fatal:", err);
  process.exit(1);
});
