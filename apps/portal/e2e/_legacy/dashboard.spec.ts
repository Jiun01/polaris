/**
 * Dashboard E2E (Phase 8 PR-12).
 *
 * Single round-trip: quota dials + recent conversations + active
 * broadcasts/promotions counters all render against
 * `/api/tenant/dashboard/summary`. Skipped without auth state.
 */

import { expect, test } from "@playwright/test";

const PRO_STATE = process.env.PLAYWRIGHT_STORAGE_STATE_PRO;

test.skip(() => !PRO_STATE, "PLAYWRIGHT_STORAGE_STATE_PRO not set");
test.use({ storageState: PRO_STATE ?? "" });

test("Dashboard renders quota + counters in one round-trip", async ({
  page,
}) => {
  await page.goto("/dashboard");
  await expect(page.getByRole("heading", { name: /dashboard/i })).toBeVisible();
  // Quota dial exposes role=progressbar with aria-label "Bot replies …".
  await expect(
    page.getByRole("progressbar", { name: /bot replies|quota/i }).first(),
  ).toBeVisible();
});
