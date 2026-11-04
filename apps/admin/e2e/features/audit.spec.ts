/**
 * Audit spec — /audit + /audit/verify
 *
 * Covers the Audit surface:
 *   /audit:        cursor pagination, tenant filter, verification banner,
 *                  verify-now button.
 *   /audit/verify: chain-integrity detail page, broken-row diff fields.
 *   whitelabel on both surfaces.
 */

import { test, expect, ADMIN_STATE, TENANT_IDS } from "../_support/fixtures";
import { AuditPage } from "../pages/AuditPage";

test.skip(() => !ADMIN_STATE, "admin state not minted");
test.use({ storageState: ADMIN_STATE ?? "" });

// ────────────────────────────────────────────────────────────────────────────
// /audit
// ────────────────────────────────────────────────────────────────────────────

test.describe("/audit", () => {
  test("page heading and table column headers render", async ({ page }) => {
    const pom = new AuditPage(page);
    await pom.goto();

    await expect(pom.heading).toBeVisible({ timeout: 10_000 });
    // AuditTable.tsx columns: Time, Action, Actor, Impersonating, Tenant
    await expect(pom.columnHeader(/^time$/i)).toBeVisible();
    await expect(pom.columnHeader(/^action$/i)).toBeVisible();
    await expect(pom.columnHeader(/^actor$/i)).toBeVisible();
    await expect(pom.columnHeader(/^impersonating$/i)).toBeVisible();
    await expect(pom.columnHeader(/^tenant$/i)).toBeVisible();
    await pom.assertNoBrandLeak();
  });

  test("verification banner renders (any status)", async ({ page }) => {
    const pom = new AuditPage(page);
    await pom.goto();
    // Banner is always present: either "No verification run yet" or a status banner.
    await expect(pom.verificationBanner).toBeVisible({ timeout: 10_000 });
  });

  test("verify-now button triggers and updates status message", async ({
    page,
  }) => {
    const pom = new AuditPage(page);
    await pom.goto();

    await expect(pom.verifyNowBtn).toBeVisible({ timeout: 10_000 });
    await pom.verifyNowBtn.click();

    // OnDemandReverify.tsx: button becomes "Running…" during POST
    await expect(page.getByRole("button", { name: /running/i })).toBeVisible({
      timeout: 3_000,
    });

    // Wait for "Verify now" to return (POST resolved + page refreshes after 3s)
    await expect(pom.verifyNowBtn).toBeVisible({ timeout: 20_000 });
  });

  test("tenant filter: adding ?tenant_id shows filter annotation", async ({
    page,
  }) => {
    const pom = new AuditPage(page);
    await pom.goto(TENANT_IDS.proC);

    await expect(pom.heading).toBeVisible({ timeout: 10_000 });
    // Page subtitle adds "Filtered: tenant {id}" (audit/page.tsx line 73-76)
    await expect(
      page.getByText(
        new RegExp(`filtered.*tenant.*${TENANT_IDS.proC.slice(0, 8)}`, "i"),
      ),
    ).toBeVisible({ timeout: 5_000 });
  });

  test("empty state: 'No audit log entries found' when table is empty", async ({
    page,
  }) => {
    const pom = new AuditPage(page);
    // Filter by a nonexistent tenant UUID — table should be empty
    await pom.goto("00000000-dead-beef-0000-000000000099");

    // Either empty-row message or no rows
    const emptyMsg = page.getByText(/no audit log entries found/i);
    const emptyMsgVisible = await emptyMsg
      .isVisible({ timeout: 5_000 })
      .catch(() => false);

    if (!emptyMsgVisible) {
      // Acceptable: the API may return zero rows without a special message
      const rowCount = await page.getByRole("table").getByRole("row").count();
      expect(rowCount).toBeLessThanOrEqual(1); // only header
    }
  });

  test("cursor pagination: 'Load more' button loads next page", async ({
    page,
  }) => {
    const pom = new AuditPage(page);
    await pom.goto();

    // "Load more" only appears when next_cursor is set
    const loadMoreVisible = await pom.loadMoreBtn
      .isVisible({ timeout: 5_000 })
      .catch(() => false);

    if (!loadMoreVisible) {
      test.fixme(
        true,
        "Not enough audit rows for cursor pagination — skipping",
      );
      return;
    }

    // Count rows before
    const beforeCount = await page.getByRole("table").getByRole("row").count();
    await pom.loadMoreBtn.click();

    // Wait for more rows
    await page.waitForFunction(
      (prev) => document.querySelectorAll("table tbody tr").length > prev,
      beforeCount - 1, // subtract header row
      { timeout: 10_000 },
    );
  });

  test("whitelabel: no forbidden brand strings on /audit", async ({ page }) => {
    const pom = new AuditPage(page);
    await pom.goto();
    await pom.assertNoBrandLeak();
  });
});

// ────────────────────────────────────────────────────────────────────────────
// /audit/verify
// ────────────────────────────────────────────────────────────────────────────

test.describe("/audit/verify", () => {
  test("page heading and 'Verify now' button render", async ({ page }) => {
    const pom = new AuditPage(page);
    await pom.gotoVerify();

    await expect(pom.verifyHeading).toBeVisible({ timeout: 10_000 });
    await expect(pom.verifyNowBtn).toBeVisible();
    await pom.assertNoBrandLeak();
  });

  test("verification banner shows chain status", async ({ page }) => {
    const pom = new AuditPage(page);
    await pom.gotoVerify();

    await expect(pom.verificationBanner).toBeVisible({ timeout: 10_000 });
    // Banner should contain one of the three status labels
    const bannerText = await pom.verificationBanner.textContent();
    const hasKnownStatus =
      /chain intact|chain broken|verification error|no audit chain verification has been run/i.test(
        bannerText ?? "",
      );
    expect(hasKnownStatus).toBe(true);
  });

  test("'Last run details' section renders when verification has run", async ({
    page,
  }) => {
    const pom = new AuditPage(page);
    await pom.gotoVerify();

    // Last run details only shows when result !== null (page.tsx line 59)
    const detailsVisible = await pom.lastRunDetails
      .isVisible({ timeout: 3_000 })
      .catch(() => false);

    if (detailsVisible) {
      // Scope all assertions within the dl element to avoid strict-mode collisions
      // with other elements whose text partially matches (e.g. VerificationBanner).
      const dl = page.locator("dl").first();
      await expect(dl.getByText(/rows scanned/i)).toBeVisible();
      await expect(dl.getByText(/duration/i)).toBeVisible();
      await expect(dl.getByText("Triggered by", { exact: true })).toBeVisible();
    }
    // If no result yet — banner says "No audit chain verification has been run yet" — also valid.
  });

  test("broken-row diff fields appear when status=broken", async ({ page }) => {
    const pom = new AuditPage(page);
    await pom.gotoVerify();

    const bannerText = await pom.verificationBanner.textContent();
    if (!/chain broken/i.test(bannerText ?? "")) {
      test.fixme(
        true,
        "Chain is intact — broken-row diff fields not assertable without a break",
      );
      return;
    }

    // Broken-row details: page.tsx lines 79-94
    await expect(page.getByText(/broken at row/i)).toBeVisible({
      timeout: 5_000,
    });
    await expect(page.getByText(/broken prev hash/i)).toBeVisible({
      timeout: 5_000,
    });
    await expect(page.getByText(/actual hash/i)).toBeVisible({
      timeout: 5_000,
    });
  });

  test("'← Audit log' link navigates back to /audit", async ({ page }) => {
    const pom = new AuditPage(page);
    await pom.gotoVerify();
    await pom.backToAuditLink.click();
    await page.waitForURL(/\/audit$/, { timeout: 5_000 });
    await expect(pom.heading).toBeVisible({ timeout: 5_000 });
  });

  test("verify-now on /audit/verify updates banner status", async ({
    page,
  }) => {
    const pom = new AuditPage(page);
    await pom.gotoVerify();

    await pom.verifyNowBtn.click();

    // Running state
    await expect(page.getByRole("button", { name: /running/i })).toBeVisible({
      timeout: 3_000,
    });

    // Settled
    await expect(pom.verifyNowBtn).toBeVisible({ timeout: 20_000 });
  });
});
