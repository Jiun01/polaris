// Focused re-capture of the /broadcasts screen for the marketing site.
// Reuses the magic-link login flow from capture-marketing.mjs.
// Run from apps/portal:  node e2e/capture-broadcasts.mjs
import { chromium } from "@playwright/test";
import * as fs from "fs";
import * as path from "path";

const BASE_URL = "http://portal.localhost";
const MAILHOG_API = "http://localhost:8025";
const EMAIL = "owner@getpolaris.my";
const OUT = path.resolve("./e2e/.shots");
const STATE_FILE = path.resolve("./e2e/.auth/mktg.json");
const STATE_TTL_MS = 45 * 60 * 1000;

async function pollForMagicLink(email, timeoutMs = 30000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, 1500));
    const res = await fetch(
      `${MAILHOG_API}/api/v2/search?kind=to&query=${encodeURIComponent(email)}`,
    );
    const body = await res.json();
    if (body.items && body.items.length > 0) {
      const raw = body.items[0].Content.Body;
      const decoded = raw
        .replace(/=\r\n/g, "")
        .replace(/=\n/g, "")
        .replace(/=([0-9A-Fa-f]{2})/g, (_, h) =>
          String.fromCharCode(parseInt(h, 16)),
        );
      const m = decoded.match(
        /https?:\/\/[^\s"<>]+api\/auth\/callback\/email[^\s"<>]*/,
      );
      if (m) return m[0];
    }
  }
  throw new Error(`No magic-link for ${email} within ${timeoutMs}ms`);
}

async function login(page) {
  await fetch(`${MAILHOG_API}/api/v1/messages`, { method: "DELETE" });
  await page.goto(`${BASE_URL}/api/auth/csrf`);
  await page.goto(`${BASE_URL}/login`);
  await page.getByLabel(/email/i).fill(EMAIL);
  await page.getByRole("button", { name: /send.*link/i }).click();
  await page.waitForURL(/\/verify-request/, { timeout: 15000 });
  const link = await pollForMagicLink(EMAIL);
  await page.goto(link, { waitUntil: "networkidle", timeout: 30000 });
  await page.waitForTimeout(1500);
  console.log("  after redeem, url =", page.url());
}

(async () => {
  fs.mkdirSync(OUT, { recursive: true });
  const browser = await chromium.launch();
  const hasFreshState =
    fs.existsSync(STATE_FILE) &&
    Date.now() - fs.statSync(STATE_FILE).mtimeMs < STATE_TTL_MS;
  const context = await browser.newContext({
    deviceScaleFactor: 2,
    ...(hasFreshState ? { storageState: STATE_FILE } : {}),
  });
  const page = await context.newPage();
  if (hasFreshState) {
    console.log("Reusing saved session:", STATE_FILE);
  } else {
    console.log("Logging in as", EMAIL);
    await login(page);
    await context.storageState({ path: STATE_FILE });
    console.log("Saved session →", STATE_FILE);
  }
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto(`${BASE_URL}/broadcasts`, {
    waitUntil: "networkidle",
    timeout: 30000,
  });
  await page.waitForTimeout(2500);
  const landed = new URL(page.url()).pathname;
  const file = path.join(OUT, "broadcasts.png");
  await page.screenshot({ path: file });
  const textLen = await page
    .evaluate(() => (document.querySelector("main")?.innerText || "").length)
    .catch(() => -1);
  console.log(`  ✓ broadcasts textLen=${textLen} landed=${landed} -> ${file}`);
  await context.close();
  await browser.close();
})();
