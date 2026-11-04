/**
 * Suspended lockdown E2E (Phase 8 PR-12).
 *
 * When `tenants.suspended_reason='payment_overdue'`, every portal
 * route must 302 to /pay per ADR-049. Test fixture: a dedicated
 * tenant with the column pre-set.
 */

import { expect, test } from "@playwright/test";

const SUSPENDED_STATE = process.env.PLAYWRIGHT_STORAGE_STATE_SUSPENDED;

test.skip(() => !SUSPENDED_STATE, "PLAYWRIGHT_STORAGE_STATE_SUSPENDED not set");
test.use({ storageState: SUSPENDED_STATE ?? "" });

const PORTAL_ROUTES = [
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
  "/branches",
  "/billing",
  "/settings",
];

for (const path of PORTAL_ROUTES) {
  test(`${path} redirects to /pay when suspended`, async ({ page }) => {
    const res = await page.goto(path);
    expect(res?.status()).toBeLessThan(400);
    await expect(page).toHaveURL(/\/pay/);
  });
}
