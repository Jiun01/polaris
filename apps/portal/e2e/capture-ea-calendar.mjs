// Capture the EA backend calendar (provider timetable) filled with appointments.
// Trick: visit /book/{slug} first to plant booking_tenant_id in the session, then
// the backend login resolves that tenant and lets the provider log in.
import { chromium } from "@playwright/test";
import * as path from "path";

const BASE = "http://localhost:8090";
const SLUG = "provider-polaris";
const USER = "provider-polaris";
const PASS = "admin123";
const OUT = path.resolve(
  // Sibling "Polaris Website" repo — override via WEBSITE_DIR.
  process.env.WEBSITE_DIR ??
    path.resolve(
      path.dirname(fileURLToPath(import.meta.url)),
      "../../../Polaris Website",
    ),
  "",
);

(async () => {
  const b = await chromium.launch();
  const ctx = await b.newContext({
    deviceScaleFactor: 2,
    viewport: { width: 1600, height: 900 },
  });
  const p = await ctx.newPage();

  // 1. Plant tenant context in session
  await p.goto(`${BASE}/book/${SLUG}`, {
    waitUntil: "networkidle",
    timeout: 30000,
  });
  await p.waitForTimeout(1500);

  // 2. Backend login
  await p.goto(`${BASE}/index.php/login`, {
    waitUntil: "networkidle",
    timeout: 30000,
  });
  await p.fill("#username", USER);
  await p.fill("#password", PASS);
  const [resp] = await Promise.all([
    p
      .waitForResponse((r) => /login\/validate/.test(r.url()), {
        timeout: 9000,
      })
      .catch(() => null),
    p.click("#login"),
  ]);
  if (resp) console.log("login:", resp.status());
  await p.waitForTimeout(4000);
  console.log("after login url:", p.url());

  // 3. Calendar
  await p.goto(`${BASE}/index.php/calendar`, {
    waitUntil: "networkidle",
    timeout: 30000,
  });
  await p.waitForTimeout(4000);
  console.log("calendar url:", p.url());
  await p.screenshot({ path: path.join(OUT, "ea-calendar.png") });

  // Try to force week view for a fuller timetable
  for (const sel of [
    'button:has-text("Week")',
    '[data-view="agendaWeek"]',
    ".fc-agendaWeek-button",
    "#select-filter-item",
  ]) {
    const el = p.locator(sel).first();
    if (await el.count().catch(() => 0)) {
      await el.click().catch(() => {});
      await p.waitForTimeout(2500);
      break;
    }
  }
  await p.waitForTimeout(2000);
  await p.screenshot({ path: path.join(OUT, "ea-calendar-week.png") });
  const len = await p
    .evaluate(() => document.body.innerText.length)
    .catch(() => -1);
  console.log("calendar bodyLen:", len);
  await ctx.close();
  await b.close();
})();
