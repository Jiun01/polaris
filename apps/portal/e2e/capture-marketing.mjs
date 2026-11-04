// One-off marketing screenshot capture (CLIENT-SAFE: portal only, no admin).
// Logs into the portal as the ULTRA dogfood tenant via magic-link (MailHog),
// then screenshots key product screens at desktop + mobile widths IN THE SAME
// authenticated page (so the session cookie is guaranteed present).
//
// Run from apps/portal:  node e2e/capture-marketing.mjs
import { chromium } from "@playwright/test";
import * as fs from "fs";
import * as path from "path";

const BASE_URL = "http://portal.localhost"; // session cookie is scoped here (via Caddy)
const MAILHOG_API = "http://localhost:8025";
const EMAIL = "owner@getpolaris.my"; // ULTRA / polaris-internal dogfood tenant
const OUT_DIR = path.resolve(
  // Sibling "Polaris Website" repo — override via WEBSITE_DIR.
  process.env.WEBSITE_DIR ??
    path.resolve(
      path.dirname(fileURLToPath(import.meta.url)),
      "../../../Polaris Website",
    ),
  "",
);

// Portal, customer-facing screens only. NO admin/ops/financials.
// Routes resolved at runtime (IDs change on each re-seed) are filled by
// resolveDynamicRoutes() before capture. Placeholders use {{conv}} / {{flow0}} / {{flow1}}.
const DESKTOP = [
  { slug: "dashboard", route: "/dashboard" },
  { slug: "conversations", route: "/conversations" },
  { slug: "conversation-thread", route: "{{conv}}", wait: 2000 },
  { slug: "flows-list", route: "/flows" },
  { slug: "flow-canvas", route: "{{flow0}}", wait: 4000 },
  { slug: "flow-canvas-booking", route: "{{flow1}}", wait: 4000 },
  { slug: "knowledge", route: "/knowledge" },
  { slug: "bookings", route: "/bookings" },
  { slug: "promotions", route: "/promotions" },
  { slug: "audience", route: "/audience" },
  { slug: "channels", route: "/channels" },
  { slug: "branches", route: "/branches" },
  { slug: "billing", route: "/billing" },
  { slug: "broadcasts", route: "/broadcasts" },
  { slug: "templates", route: "/templates" },
];
const MOBILE = [
  { slug: "dashboard", route: "/dashboard" },
  { slug: "conversations", route: "/conversations" },
  { slug: "conversation-thread", route: "{{conv}}", wait: 2000 },
  { slug: "knowledge", route: "/knowledge" },
];

async function resolveDynamicRoutes(page) {
  const map = {};
  // First booking-intent conversation makes the nicest thread; else first row.
  await page.goto(`${BASE_URL}/conversations`, { waitUntil: "networkidle" });
  await page.waitForTimeout(1500);
  // Prefer the menu-rich "Wei Jie" pricing thread for the phone/thread shots.
  let href = await page
    .locator('a[href^="/conversations/"]', { hasText: "Wei Jie" })
    .first()
    .getAttribute("href")
    .catch(() => null);
  if (!href) {
    href = await page
      .locator('a[href^="/conversations/"]')
      .first()
      .getAttribute("href")
      .catch(() => null);
  }
  if (!href) {
    // rows may be clickable <tr> rather than <a>; click the Daniel Tan row
    const row = page.getByText("Daniel Tan", { exact: false }).first();
    if (await row.count()) {
      await row.click();
      await page
        .waitForURL(/\/conversations\/[0-9a-f-]{36}/, { timeout: 8000 })
        .catch(() => {});
      href = new URL(page.url()).pathname;
    }
  }
  map["{{conv}}"] = href || "/conversations";

  await page.goto(`${BASE_URL}/flows`, { waitUntil: "networkidle" });
  await page.waitForTimeout(1500);
  const flowHrefs = await page
    .locator('a[href^="/flows/"]')
    .evaluateAll((els) =>
      els.map((e) => e.getAttribute("href")).filter(Boolean),
    );
  const uniq = [...new Set(flowHrefs)].filter((h) =>
    /\/flows\/[0-9a-f-]{36}/.test(h),
  );
  map["{{flow0}}"] = uniq[0] || "/flows";
  map["{{flow1}}"] = uniq[1] || uniq[0] || "/flows";
  console.log("  resolved:", JSON.stringify(map));
  return map;
}

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

async function capture(page, screens, { width, height, suffix }) {
  await page.setViewportSize({ width, height });
  for (const s of screens) {
    try {
      await page.goto(`${BASE_URL}${s.route}`, {
        waitUntil: "networkidle",
        timeout: 30000,
      });
      await page.waitForTimeout(s.wait ?? 2500);
      // Flow canvas: zoom-to-fit so nodes are readable, not a tiny far-out graph.
      if (s.slug.startsWith("flow-canvas")) {
        for (const sel of [
          ".react-flow__controls-fitview",
          'button[aria-label="fit view" i]',
          'button[title="fit view" i]',
        ]) {
          const btn = page.locator(sel).first();
          if (await btn.count().catch(() => 0)) {
            await btn.click({ timeout: 1500 }).catch(() => {});
            break;
          }
        }
        await page.waitForTimeout(1200);
      }
      const landed = new URL(page.url()).pathname;
      const file = path.join(OUT_DIR, `${s.slug}${suffix}.png`);
      await page.screenshot({ path: file });
      // Rough emptiness signal: count visible text length in <main>.
      const textLen = await page
        .evaluate(
          () => (document.querySelector("main")?.innerText || "").length,
        )
        .catch(() => -1);
      const flag = landed !== s.route ? `  ⚠ landed ${landed}` : "";
      console.log(`  ✓ ${s.slug}${suffix} textLen=${textLen}${flag}`);
    } catch (e) {
      console.log(`  ✗ ${s.slug}${suffix}: ${e.message.split("\n")[0]}`);
    }
  }
}

const STATE_FILE = path.resolve("./e2e/.auth/mktg.json");
const STATE_TTL_MS = 45 * 60 * 1000;

(async () => {
  fs.mkdirSync(OUT_DIR, { recursive: true });
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
  const dyn = await resolveDynamicRoutes(page);
  const sub = (list) =>
    list.map((s) => ({ ...s, route: dyn[s.route] ?? s.route }));
  console.log("Capturing desktop...");
  await capture(page, sub(DESKTOP), { width: 1440, height: 900, suffix: "" });
  console.log("Capturing mobile...");
  await capture(page, sub(MOBILE), {
    width: 390,
    height: 844,
    suffix: "-mobile",
  });
  await context.close();
  await browser.close();
  console.log("Done →", OUT_DIR);
})();
