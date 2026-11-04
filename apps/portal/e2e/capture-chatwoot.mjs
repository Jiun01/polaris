// Log into Chatwoot (the agent inbox) and screenshot it for the website.
import { chromium } from "@playwright/test";
import * as path from "path";

const BASE = "http://localhost:3002";
const EMAIL = "agent-polaris@e2e.polaris-local.dev";
const PASS = "E2e-agent-password!";
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
    viewport: { width: 1440, height: 900 },
  });
  const p = await ctx.newPage();
  await p.goto(`${BASE}`, { waitUntil: "networkidle", timeout: 30000 });
  console.log("landing url:", p.url());

  // Devise sign-in form
  try {
    await p.fill('input[name="email_address"]', EMAIL, { timeout: 10000 });
    await p.fill('input[name="password"]', PASS);
    await p.getByRole("button", { name: /login/i }).click();
    await p.waitForURL(/accounts\/\d+/, { timeout: 15000 }).catch(() => {});
    await p.waitForTimeout(4000);
  } catch (e) {
    console.log("login form issue:", e.message.split("\n")[0]);
  }
  console.log("after login url:", p.url());
  await p.waitForTimeout(2000);
  await p.screenshot({
    path: path.join(OUT, "chatwoot-after-login.png"),
    fullPage: false,
  });

  // Inbox list — switch to the "All" tab so every conversation shows
  await p.goto(`${BASE}/app/accounts/1/conversations`, {
    waitUntil: "networkidle",
    timeout: 20000,
  });
  await p.waitForTimeout(3000);
  const allTab = p.getByText(/^All\s*\d*$/).first();
  if (await allTab.count().catch(() => 0)) {
    await allTab.click().catch(() => {});
    await p.waitForTimeout(2000);
  }
  await p.screenshot({ path: path.join(OUT, "chatwoot-inbox.png") });
  console.log("  inbox shot");

  // Open the Wei Jie (pricing) conversation for a rich thread
  try {
    const convo = p.getByText("Wei Jie", { exact: false }).first();
    const target = (await convo.count().catch(() => 0))
      ? convo
      : p.getByText("Daniel Tan").first();
    await target.click({ timeout: 6000 });
    await p.waitForTimeout(3000);
    await p.screenshot({ path: path.join(OUT, "chatwoot-thread.png") });
    console.log("  thread shot:", p.url());
  } catch (e) {
    console.log("  thread click failed:", e.message.split("\n")[0]);
  }
  await ctx.close();
  await b.close();
})();
