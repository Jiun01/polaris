// Screenshot the Polaris marketing website (local dev) for visual verification.
import { chromium } from "@playwright/test";
import * as path from "path";

const OUT = path.resolve(
  // Sibling "Polaris Website" repo — override via WEBSITE_DIR.
  process.env.WEBSITE_DIR ??
    path.resolve(
      path.dirname(fileURLToPath(import.meta.url)),
      "../../../Polaris Website",
    ),
  "",
);
const pages = process.argv.slice(2);
const TARGETS = pages.length
  ? pages.map((p) => ({
      url: p,
      slug: p.replace(/[^a-z0-9]+/gi, "_") || "home",
    }))
  : [{ url: "http://localhost:4322/", slug: "home" }];

(async () => {
  const browser = await chromium.launch();
  const ctx = await browser.newContext({
    deviceScaleFactor: 1,
    viewport: { width: 1440, height: 1000 },
  });
  await ctx.addInitScript(() => {
    try {
      localStorage.setItem(
        "polaris_demo_popup",
        JSON.stringify({ dismissedUntil: Date.now() + 9e11 }),
      );
      localStorage.setItem(
        "welcome_session",
        JSON.stringify({ cookiesAccepted: "all", seen: true }),
      );
    } catch {}
  });
  const page = await ctx.newPage();
  for (const t of TARGETS) {
    await page.goto(t.url, { waitUntil: "networkidle", timeout: 45000 });
    await page.waitForTimeout(1500);
    // reveal scroll-triggered animations
    await page.evaluate(async () => {
      for (let y = 0; y < document.body.scrollHeight; y += 700) {
        window.scrollTo(0, y);
        await new Promise((r) => setTimeout(r, 120));
      }
      window.scrollTo(0, 0);
    });
    await page.waitForTimeout(800);
    await page.screenshot({
      path: path.join(OUT, `web-${t.slug}.png`),
      fullPage: true,
    });
    console.log("shot", t.slug);
  }
  await ctx.close();
  await browser.close();
})();
