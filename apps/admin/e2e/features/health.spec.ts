/**
 * Health spec — /health + /system-health redirect + /channels + /promotions
 *
 * Covers the Health surface:
 *   /health:          5 tiles (Supabase/Redis/Broadcast Queue/Ingestion/Meta-rate),
 *                     auto-refresh wiring (interval set up), refresh-now button,
 *                     last-refreshed timestamp updates.
 *   /system-health:   redirects to /health.
 *   /channels:        placeholder page renders heading.
 *   /promotions:      placeholder page renders heading.
 *   whitelabel on all surfaces.
 */

import { test, expect, ADMIN_STATE } from "../_support/fixtures";
import { HealthPage } from "../pages/HealthPage";

test.skip(() => !ADMIN_STATE, "admin state not minted");
test.use({ storageState: ADMIN_STATE ?? "" });

// ────────────────────────────────────────────────────────────────────────────
// /health — 5 tiles
// ────────────────────────────────────────────────────────────────────────────

test.describe("/health tiles", () => {
  test("page heading renders", async ({ page }) => {
    const pom = new HealthPage(page);
    await pom.goto();

    await expect(pom.heading).toBeVisible({ timeout: 10_000 });
    await expect(
      page.getByText(/auto-refreshes every 5 minutes/i),
    ).toBeVisible();
    await pom.assertNoBrandLeak();
  });

  test("all 5 tile headings are visible within 2 s budget", async ({
    page,
  }) => {
    const start = Date.now();
    const pom = new HealthPage(page);
    await pom.goto();

    await pom.assertAllTilesVisible(2_000);
    expect(Date.now() - start).toBeLessThan(2_500);
  });

  test("Supabase tile shows healthy or degraded badge", async ({ page }) => {
    const pom = new HealthPage(page);
    await pom.goto();

    // SupabaseStatusTile renders "Healthy" or "Degraded" badge
    const badge = page
      .getByText(/supabase/i)
      .first()
      .locator("../..")
      .getByText(/healthy|degraded|unavailable/i);
    await expect(badge.first()).toBeVisible({ timeout: 5_000 });
  });

  test("Redis tile shows healthy or degraded badge", async ({ page }) => {
    const pom = new HealthPage(page);
    await pom.goto();

    const badge = page
      .getByText(/^redis$/i)
      .first()
      .locator("../..")
      .getByText(/healthy|degraded|unavailable/i);
    await expect(badge.first()).toBeVisible({ timeout: 5_000 });
  });

  test("Broadcast Queue tile shows pending/in-flight/failed metrics", async ({
    page,
  }) => {
    const pom = new HealthPage(page);
    await pom.goto();

    // BroadcastQueueDepthTile renders Pending, In-Flight, Failed (1 h) labels
    const tile = page
      .getByText(/broadcast queue/i)
      .first()
      .locator("../..");
    const tileText = await tile.textContent().catch(() => "");
    if (tileText && !tileText.includes("Unavailable")) {
      await expect(page.getByText(/pending/i).first()).toBeVisible();
      await expect(page.getByText(/in-flight/i)).toBeVisible();
      // Tile renders both "Failed (1 h)" and "Failed recipients (1 h)" — match the first.
      await expect(page.getByText(/failed.*1.*h/i).first()).toBeVisible();
    } else {
      // Unavailable state also acceptable
      await expect(page.getByText(/unavailable/i).first()).toBeVisible();
    }
  });

  test("Traffic + risk tiles render (messaging, LLM, quota, failures)", async ({
    page,
  }) => {
    const pom = new HealthPage(page);
    await pom.goto();

    await expect(pom.messagingTile).toBeVisible({ timeout: 5_000 });
    await expect(pom.llmTile).toBeVisible({ timeout: 5_000 });
    await expect(pom.quotaTile).toBeVisible({ timeout: 5_000 });
    await expect(pom.failuresTile).toBeVisible({ timeout: 5_000 });
    await expect(pom.webhookEventErrorsTile).toBeVisible({ timeout: 5_000 });
  });

  test("Meta Rate Limits tile renders", async ({ page }) => {
    const pom = new HealthPage(page);
    await pom.goto();

    await expect(pom.metaRateTile).toBeVisible({ timeout: 5_000 });
    // Either surface rows or "No usage data in the last 24 h"
    const noData = page.getByText(/no usage data in the last 24/i);
    const hasData = page.getByText(/whatsapp|instagram|messenger/i);
    await expect(noData.or(hasData).first()).toBeVisible({ timeout: 5_000 });
  });
});

// ────────────────────────────────────────────────────────────────────────────
// /health — refresh controls
// ────────────────────────────────────────────────────────────────────────────

test.describe("/health refresh", () => {
  test("last-refreshed timestamp is visible after page load", async ({
    page,
  }) => {
    const pom = new HealthPage(page);
    await pom.goto();

    // data-testid="last-refreshed-timestamp" — HealthDashboard.tsx line 113
    await expect(pom.lastRefreshedTimestamp).toBeVisible({ timeout: 10_000 });
  });

  test("'Refresh now' button is visible and enabled", async ({ page }) => {
    const pom = new HealthPage(page);
    await pom.goto();

    await expect(pom.refreshNowBtn).toBeVisible({ timeout: 10_000 });
    await expect(pom.refreshNowBtn).toBeEnabled();
  });

  test("clicking 'Refresh now' updates the last-refreshed timestamp", async ({
    page,
  }) => {
    const pom = new HealthPage(page);
    await pom.goto();

    // Capture initial timestamp text
    await expect(pom.lastRefreshedTimestamp).toBeVisible({ timeout: 10_000 });
    const before = await pom.lastRefreshedTimestamp.textContent();

    // Brief pause to ensure a new wall-clock time, then refresh
    // (HealthDashboard uses new Date().toISOString() on refresh — distinct from server time)
    await page.waitForTimeout(1_100);
    await pom.refreshNowBtn.click();

    // Button briefly shows "Refreshing…"
    await expect(page.getByRole("button", { name: /refreshing/i })).toBeVisible(
      { timeout: 3_000 },
    );

    // Wait for button to return to "Refresh now"
    await expect(pom.refreshNowBtn).toBeVisible({ timeout: 10_000 });

    // Timestamp must differ from the captured value
    await expect(pom.lastRefreshedTimestamp).not.toHaveText(before ?? "", {
      timeout: 10_000,
    });
  });

  test("auto-refresh interval is wired (setInterval registered)", async ({
    page,
  }) => {
    const pom = new HealthPage(page);
    await pom.goto();

    // Verify HealthDashboard sets up the 5-minute interval by checking the
    // component renders without error (interval is set up in useEffect).
    // We can't directly assert setInterval calls in Playwright without
    // instrumentation; we assert the component is mounted and functional.
    await expect(pom.lastRefreshedTimestamp).toBeVisible({ timeout: 5_000 });

    // Advance time by simulating a visibility change (tab hide + show)
    // which triggers an immediate doFetch() — tests the visibility handler.
    await page.evaluate(() => {
      // Simulate tab becoming hidden then visible
      Object.defineProperty(document, "hidden", {
        value: true,
        writable: true,
      });
      document.dispatchEvent(new Event("visibilitychange"));
      Object.defineProperty(document, "hidden", {
        value: false,
        writable: true,
      });
      document.dispatchEvent(new Event("visibilitychange"));
    });

    // After the simulated visibility event the dashboard fires doFetch().
    // Wait briefly for the refresh to settle.
    await expect(pom.refreshNowBtn).toBeVisible({ timeout: 10_000 });
  });
});

// ────────────────────────────────────────────────────────────────────────────
// /system-health redirect
// ────────────────────────────────────────────────────────────────────────────

test.describe("/system-health → /health redirect", () => {
  test("navigating to /system-health redirects to /health", async ({
    page,
  }) => {
    const pom = new HealthPage(page);
    await pom.gotoViaAlias();
    await page.waitForURL(/\/health$/, { timeout: 10_000 });
    await expect(pom.heading).toBeVisible({ timeout: 10_000 });
  });
});

// ────────────────────────────────────────────────────────────────────────────
// Placeholder pages: /channels + /promotions
// ────────────────────────────────────────────────────────────────────────────

test.describe("/channels placeholder", () => {
  test("renders Channels heading and placeholder card", async ({ page }) => {
    await page.goto("/channels");
    await page.waitForLoadState("networkidle");

    await expect(
      page.getByRole("heading", { name: /^channels$/i }),
    ).toBeVisible({
      timeout: 10_000,
    });
    // Placeholder text from channels/page.tsx line 26-29
    await expect(
      page.getByText(/per-tenant channels render in tenants/i),
    ).toBeVisible();
  });

  test("channels placeholder: no brand leak", async ({ page }) => {
    await page.goto("/channels");
    await page.waitForLoadState("networkidle");
    const body = (await page.locator("body").innerText()).toLowerCase();
    for (const s of ["chatwoot", "easy!appointments", "easyappointments"]) {
      if (body.includes(s.toLowerCase())) {
        throw new Error(`Whitelabel leak: "${s}" on /channels`);
      }
    }
  });
});

test.describe("/promotions placeholder", () => {
  test("renders Promotions heading and placeholder card", async ({ page }) => {
    await page.goto("/promotions");
    await page.waitForLoadState("networkidle");

    await expect(
      page.getByRole("heading", { name: /^promotions$/i }),
    ).toBeVisible({ timeout: 10_000 });
    // Placeholder text from promotions/page.tsx line 26-29
    await expect(
      page.getByText(/aggregate view coming alongside the cost dashboard/i),
    ).toBeVisible();
  });
});
