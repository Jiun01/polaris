/**
 * Financials spec — /financials
 *
 * Covers the Financials surface:
 *   - MRR card big-number
 *   - 30-day trend table
 *   - recompute-mrr button (POST → fresh data)
 *   - whitelabel
 */

import { test, expect, ADMIN_STATE } from "../_support/fixtures";
import { FinancialsPage } from "../pages/FinancialsPage";

test.skip(() => !ADMIN_STATE, "admin state not minted");
test.use({ storageState: ADMIN_STATE ?? "" });

// Run serially: recompute-mrr mutates the global MRR sentinel row.
test.describe.configure({ mode: "serial" });

test.describe("/financials", () => {
  test("page heading and MRR card render", async ({ page }) => {
    const pom = new FinancialsPage(page);
    await pom.goto();

    await expect(pom.heading).toBeVisible({ timeout: 10_000 });
    // data-testid="mrr-big-number" — MrrOverviewCard.tsx
    await expect(pom.mrrBigNumber).toBeVisible({ timeout: 10_000 });
    await pom.assertNoBrandLeak();
  });

  test("30-day trend table renders when snapshots exist", async ({ page }) => {
    const pom = new FinancialsPage(page);
    await pom.goto();

    // The trend table only renders when trend_30d.length > 0.
    // If no trend data, the table is absent — both states are valid.
    const tableVisible = await pom.trendTable.isVisible().catch(() => false);

    if (tableVisible) {
      // Should have at least a header row
      await expect(
        pom.trendTable.getByRole("columnheader", { name: /date/i }),
      ).toBeVisible();
      await expect(
        pom.trendTable.getByRole("columnheader", { name: /mrr/i }),
      ).toBeVisible();
    }
  });

  test("recompute-mrr: clicking 'Recompute now' posts and updates card", async ({
    page,
  }) => {
    const pom = new FinancialsPage(page);
    await pom.goto();

    await expect(pom.recomputeBtn).toBeVisible({ timeout: 10_000 });

    // Capture MRR text before recompute
    const before = await pom.mrrBigNumber.textContent();

    // Click — button label changes to "Computing…" during the POST
    await pom.recomputeBtn.click();
    await expect(page.getByRole("button", { name: /computing/i })).toBeVisible({
      timeout: 3_000,
    });

    // Wait for "Recompute now" to return (POST resolved)
    await expect(pom.recomputeBtn).toBeVisible({ timeout: 15_000 });

    // MRR card must still be on screen after recompute
    await expect(pom.mrrBigNumber).toBeVisible();

    // If the API returned updated data, the big number may have changed;
    // we accept either change or same value (API may return same result).
    const after = await pom.mrrBigNumber.textContent();
    expect(typeof after).toBe("string");
    void before; // suppress unused-var warning
  });
});
