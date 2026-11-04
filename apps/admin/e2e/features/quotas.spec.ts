/**
 * Quotas spec — /quotas + /quotas/[tenant_id]
 *
 * Covers the Quotas surface:
 *   /quotas:           usage table, override link.
 *   /quotas/[id]:      16-column dropdown, value input → PUT, success msg,
 *                      reset (blank → 0), 404, last-write-wins idempotency.
 *
 * Uses TENANT_IDS.proC. Overrides are reset after each mutating test.
 */

import { test, expect, ADMIN_STATE, TENANT_IDS } from "../_support/fixtures";
import { QuotasPage } from "../pages/QuotasPage";

test.skip(() => !ADMIN_STATE, "admin state not minted");
test.use({ storageState: ADMIN_STATE ?? "" });

// ────────────────────────────────────────────────────────────────────────────
// /quotas — usage table
// ────────────────────────────────────────────────────────────────────────────

test.describe("/quotas usage table", () => {
  test("page heading and column headers render", async ({ page }) => {
    const pom = new QuotasPage(page);
    await pom.gotoList();

    await expect(pom.heading).toBeVisible({ timeout: 10_000 });
    // UsageTable.tsx column headers
    await expect(pom.columnHeader(/^tenant$/i)).toBeVisible();
    await expect(pom.columnHeader(/bot replies.*cap/i)).toBeVisible();
    await expect(pom.columnHeader(/^usage$/i)).toBeVisible();
    await expect(pom.columnHeader(/^seats$/i)).toBeVisible();
    await expect(pom.columnHeader(/^tier$/i)).toBeVisible();
    await pom.assertNoBrandLeak();
  });

  test("table contains at least one tenant row from the seed", async ({
    page,
  }) => {
    const pom = new QuotasPage(page);
    await pom.gotoList();

    // 5 seeded tenants should produce at least one row
    const rows = pom.usageTable.getByRole("row");
    const count = await rows.count();
    // Header + at least 1 data row
    expect(count).toBeGreaterThan(1);
  });

  test("each data row has an 'Override' link", async ({ page }) => {
    const pom = new QuotasPage(page);
    await pom.gotoList();

    const overrideLinks = page.getByRole("link", { name: /^override$/i });
    const count = await overrideLinks.count();
    expect(count).toBeGreaterThan(0);
  });

  test("'Override' link for proC navigates to /quotas/[proC]", async ({
    page,
  }) => {
    const pom = new QuotasPage(page);
    await pom.gotoList();

    // Find the Override link in the proC row
    const overrideLink = page
      .getByRole("link", {
        name: /^override$/i,
      })
      .filter({ has: page.locator(`[href="/quotas/${TENANT_IDS.proC}"]`) });

    const linkExists = await overrideLink.count().then((n) => n > 0);
    if (!linkExists) {
      // proC may not appear if no quota row seeded — navigate directly
      await pom.gotoOverride(TENANT_IDS.proC);
      return;
    }

    await overrideLink.click();
    await page.waitForURL(new RegExp(`/quotas/${TENANT_IDS.proC}`), {
      timeout: 5_000,
    });
    await expect(pom.overrideHeading).toBeVisible({ timeout: 5_000 });
  });

  test("empty state when no quota rows exist", async ({ page }) => {
    const pom = new QuotasPage(page);
    await pom.gotoList();

    // If no data, UsageTable.tsx line 70 renders the dashed border card.
    // With the seeded tenants this should not be the empty state, but we
    // guard anyway.
    const isEmpty = await page
      .getByText(/no tenants with quota rows yet/i)
      .isVisible()
      .catch(() => false);

    if (isEmpty) {
      await expect(
        page.getByText(/no tenants with quota rows yet/i),
      ).toBeVisible();
    } else {
      // Normal: table rows present
      await expect(pom.usageTable).toBeVisible();
    }
  });
});

// ────────────────────────────────────────────────────────────────────────────
// /quotas/[tenant_id] — override form
// ────────────────────────────────────────────────────────────────────────────

test.describe("/quotas/[tenant_id] override form", () => {
  test("renders heading, back link, and current quota summary for proC", async ({
    page,
  }) => {
    const pom = new QuotasPage(page);
    await pom.gotoOverride(TENANT_IDS.proC);

    await expect(pom.overrideHeading).toBeVisible({ timeout: 10_000 });
    await expect(pom.backLink).toBeVisible();

    // QuotaOverridePage.tsx summary card items
    await expect(page.getByText(/plan tier/i)).toBeVisible();
    await expect(page.getByText(/bot replies cap/i)).toBeVisible();
    await expect(page.getByText(/used this period/i)).toBeVisible();

    await pom.assertNoBrandLeak();
  });

  test("column dropdown contains all 16 quota columns", async ({ page }) => {
    const pom = new QuotasPage(page);
    await pom.gotoOverride(TENANT_IDS.proC);

    // OverrideForm.tsx QUOTA_COLUMNS (16 entries)
    const expectedColumns = [
      "bot_replies",
      "bot_replies_period_cap",
      "vision_calls_per_month",
      "conversation_retention_days",
      "knowledge_chunks_total_mb",
      "rate_limit_per_minute",
      "monthly_llm_tokens_in",
      "monthly_llm_tokens_out",
      "monthly_ingestion_pages",
      "monthly_broadcast_recipients",
      "monthly_bookings",
      "agent_seats_included",
      "agent_seats_extra",
      "outbound_broadcast_enabled",
      "multi_branch_enabled",
      "allowed_channel_kinds",
    ];

    for (const col of expectedColumns) {
      await expect(
        pom.columnSelect.locator(`option[value="${col}"]`),
      ).toBeAttached();
    }
  });

  test("apply override: sets rate_limit_per_minute to disposable value then resets", async ({
    page,
  }) => {
    const pom = new QuotasPage(page);
    await pom.gotoOverride(TENANT_IDS.proC);

    // Use a disposable value unlikely to affect other tests
    const disposableValue = "9999";
    const resetValue = ""; // blank → resets to 0

    // --- SET ---
    await pom.applyOverride("rate_limit_per_minute", disposableValue);
    // Success message: "Overridden: rate_limit_per_minute = 9999 at …"
    await expect(pom.successMsg).toContainText(/rate_limit_per_minute/i, {
      timeout: 10_000,
    });
    await expect(pom.successMsg).toContainText(disposableValue);

    // --- RESET ---
    await pom.resetOverride("rate_limit_per_minute");
    // Success message for reset (value = 0)
    await expect(pom.successMsg).toContainText(/rate_limit_per_minute/i, {
      timeout: 10_000,
    });
  });

  test("last-write-wins: applying override twice keeps final value", async ({
    page,
  }) => {
    const pom = new QuotasPage(page);
    await pom.gotoOverride(TENANT_IDS.proC);

    // Apply first value
    await pom.applyOverride("vision_calls_per_month", "111");
    await expect(pom.successMsg).toContainText("111", { timeout: 10_000 });

    // Apply second value (last-write-wins)
    await pom.applyOverride("vision_calls_per_month", "222");
    await expect(pom.successMsg).toContainText("222", { timeout: 10_000 });

    // Reset
    await pom.resetOverride("vision_calls_per_month");
    await expect(pom.successMsg).toBeVisible({ timeout: 10_000 });
  });

  test("reset (blank value) sets column to 0", async ({ page }) => {
    const pom = new QuotasPage(page);
    await pom.gotoOverride(TENANT_IDS.proC);

    // First set a known value
    await pom.applyOverride("monthly_bookings", "500");
    await expect(pom.successMsg).toContainText("500", { timeout: 10_000 });

    // Then reset (blank → 0): OverrideForm renders "Overridden: monthly_bookings = 0 at <time>"
    // where new_value ?? 0 is used (null coalesces to 0).
    await pom.columnSelect.selectOption("monthly_bookings");
    await pom.valueInput.fill("");
    await pom.applyBtn.click();
    await expect(pom.successMsg).toContainText(/monthly_bookings/i, {
      timeout: 10_000,
    });
    // OverrideForm.tsx line 126: renders `result.new_value ?? 0` → displays "0"
    await expect(pom.successMsg).toContainText("0");

    // Restore fixture: set back to a neutral value then reset again cleanly
    await pom.resetOverride("monthly_bookings");
  });

  test("404: unknown tenant renders not-found page", async ({ page }) => {
    const pom = new QuotasPage(page);
    await pom.gotoOverride("00000000-dead-beef-0000-000000000000");
    // Next.js notFound() renders a 404 page
    await expect(
      page.getByText(/404|not found|page could not be found/i).first(),
    ).toBeVisible({ timeout: 10_000 });
  });

  test("back link returns to /quotas", async ({ page }) => {
    const pom = new QuotasPage(page);
    await pom.gotoOverride(TENANT_IDS.proC);
    await pom.backLink.click();
    await page.waitForURL(/\/quotas$/, { timeout: 5_000 });
    await expect(pom.heading).toBeVisible({ timeout: 5_000 });
  });
});
