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
const url = process.argv[2] || "http://localhost:4322/";
const text = process.argv[3] || "The actual product";
const slug = process.argv[4] || "el";
(async () => {
  const b = await chromium.launch();
  const ctx = await b.newContext({
    deviceScaleFactor: 2,
    viewport: { width: 1440, height: 1000 },
  });
  // Suppress the timed demo popup + consent for clean marketing shots.
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
  const p = await ctx.newPage();
  await p.goto(url, { waitUntil: "networkidle", timeout: 45000 });
  // dismiss consent + timed demo popups
  await p.waitForTimeout(2500);
  for (const t of [
    "Accept",
    "Terima",
    "Got it",
    "Setuju",
    "Maybe later",
    "Close",
  ]) {
    const btn = p.getByRole("button", { name: new RegExp(t, "i") }).first();
    if (await btn.count().catch(() => 0)) {
      await btn.click().catch(() => {});
      await p.waitForTimeout(300);
    }
    const lnk = p.getByText(new RegExp(`^${t}$`, "i")).first();
    if (await lnk.count().catch(() => 0)) {
      await lnk.click().catch(() => {});
      await p.waitForTimeout(300);
    }
  }
  await p.keyboard.press("Escape").catch(() => {});
  await p.waitForTimeout(500);
  await p.evaluate(async () => {
    for (let y = 0; y < document.body.scrollHeight; y += 500) {
      window.scrollTo(0, y);
      await new Promise((r) => setTimeout(r, 90));
    }
  });
  const sec = p.locator(`section:has(h2:has-text("${text}"))`).first();
  await sec.scrollIntoViewIfNeeded();
  await p.waitForTimeout(900);
  await sec.screenshot({ path: path.join(OUT, `web-sec-${slug}.png`) });
  console.log("shot", slug);
  await ctx.close();
  await b.close();
})();
