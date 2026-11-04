// Capture the EA public booking page (customer-facing scheduler) for a tenant.
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
const URL = "http://localhost:8090/book/provider-polaris";
(async () => {
  const b = await chromium.launch();
  const ctx = await b.newContext({
    deviceScaleFactor: 2,
    viewport: { width: 1440, height: 900 },
  });
  const p = await ctx.newPage();
  await p.goto(URL, { waitUntil: "networkidle", timeout: 30000 });
  await p.waitForTimeout(3500);
  await p.screenshot({ path: path.join(OUT, "ea-booking.png") });
  console.log("step1 shot");

  // Step 1 → pick a service (first real option), then Next
  try {
    const opts = await p.$$eval("#select-service option", (os) =>
      os.map((o) => ({ v: o.value, t: o.textContent.trim() })),
    );
    console.log("services:", JSON.stringify(opts));
    const real = opts.find((o) => o.v && o.v !== "");
    if (real) {
      await p.selectOption("#select-service", real.v);
      await p.waitForTimeout(1500);
    }
    await p.click("#button-next-1");
    await p.waitForTimeout(3500); // calendar + slots load via AJAX
    await p.screenshot({ path: path.join(OUT, "ea-booking-datetime.png") });
    console.log("step2 (datetime) shot");
  } catch (e) {
    console.log("wizard advance failed:", e.message.split("\n")[0]);
  }

  // mobile (step 1)
  await p.goto(URL, { waitUntil: "networkidle", timeout: 30000 });
  await p.setViewportSize({ width: 390, height: 844 });
  await p.waitForTimeout(2000);
  await p.screenshot({ path: path.join(OUT, "ea-booking-mobile.png") });
  console.log("mobile shot");
  await ctx.close();
  await b.close();
})();
