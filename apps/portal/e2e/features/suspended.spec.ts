/**
 * Suspended / payment_overdue E2E — rebuilt suite.
 *
 * Surfaces covered: suspended/pay.
 *   - (suspended)/pay page: heading, bank-transfer section, "I've paid" mailto CTA,
 *     outstanding invoices section, no whitelabel leak.
 *   - Middleware route-takeover: every portal route redirects to /pay when the
 *     tenant is payment_overdue.
 *
 * Fixture: TENANTS.overdue (tenant-e-overdue, status=suspended,
 * suspended_reason=payment_overdue). Its session is minted by global-setup
 * landing on /pay (the middleware redirects it there).
 *
 * Selectors confirmed from app/(suspended)/pay/page.tsx.
 */

import { test, expect, TENANTS, requireState } from "../_support/fixtures";

const overdue = TENANTS.overdue;

test.describe("Payment-overdue /pay page [overdue]", () => {
  test.skip(() => !overdue.state, "overdue storage state not minted");
  test.use({ storageState: requireState(overdue) });

  test("/pay heading renders", async ({ page }) => {
    await page.goto("/pay");
    await expect(
      page.getByRole("heading", { name: /suspended for an unpaid invoice/i }),
    ).toBeVisible();
  });

  test("/pay bank-transfer section is visible", async ({ page }) => {
    await page.goto("/pay");
    await expect(
      page.getByRole("heading", { name: /pay by bank transfer/i }),
    ).toBeVisible();
    await expect(page.getByText(/maybank/i)).toBeVisible();
  });

  test("/pay 'I've paid — notify support' CTA is a mailto link", async ({
    page,
  }) => {
    await page.goto("/pay");
    const cta = page.getByRole("link", { name: /i.?ve paid.*notify support/i });
    await expect(cta).toBeVisible();
    expect(await cta.getAttribute("href")).toMatch(/^mailto:/);
  });

  test("/pay outstanding invoices section is visible", async ({ page }) => {
    await page.goto("/pay");
    await expect(
      page.getByRole("heading", { name: /outstanding invoices/i }),
    ).toBeVisible();
  });

  test("no whitelabel brand strings on /pay", async ({ page }) => {
    await page.goto("/pay");
    await page.waitForLoadState("networkidle");
    const body = (await page.locator("body").innerText()).toLowerCase();
    for (const s of ["chatwoot", "easy!appointments", "easyappointments"]) {
      expect(body).not.toContain(s);
    }
  });
});

// ---------------------------------------------------------------------------
// Middleware route-takeover — every portal route → /pay when payment_overdue.
// ---------------------------------------------------------------------------

const LOCKED_ROUTES = [
  "/dashboard",
  "/conversations",
  "/knowledge",
  "/flows",
  "/templates",
  "/audience",
  "/broadcasts",
  "/channels",
  "/bookings",
  "/promotions",
  "/billing",
  "/settings",
];

test.describe("Payment-overdue route-takeover [overdue]", () => {
  test.skip(() => !overdue.state, "overdue storage state not minted");
  test.use({ storageState: requireState(overdue) });

  for (const route of LOCKED_ROUTES) {
    test(`${route} redirects to /pay`, async ({ page }) => {
      await page.goto(route);
      await expect(page).toHaveURL(/\/pay/, { timeout: 10_000 });
    });
  }
});
