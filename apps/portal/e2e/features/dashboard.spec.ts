/**
 * Dashboard E2E — rebuilt suite.
 *
 * Surfaces covered: Dashboard.
 *   - Quota dial renders as progressbar
 *   - 3 stat tiles: active broadcasts, active promotions, ingestion in flight
 *   - Recent conversations card
 *   - Sparkline SVG
 *   - Suspension banner (quota_exhausted variant)
 *   - Fetch-error banner (mocked 503)
 *   - Empty state (recent conversations empty list)
 *   - Fresh-tenant → /onboarding redirect (cookie-controlled)
 *   - No whitelabel leak
 */

import { test, expect, TENANTS, requireState } from "../_support/fixtures";
import { DashboardPage } from "../pages/DashboardPage";
import { setE2EFault } from "../_support/e2e-fault";

const proC = TENANTS.proC;
const ultra = TENANTS.ultra;

// ---------------------------------------------------------------------------
// Happy path — pro tenant
// ---------------------------------------------------------------------------

test.describe("Dashboard happy path [pro]", () => {
  test.skip(() => !proC.state, "pro-c storage state not minted");
  test.use({ storageState: requireState(proC) });

  test("heading renders", async ({ page }) => {
    const dash = new DashboardPage(page);
    await dash.goto();
    await expect(dash.heading).toBeVisible();
  });

  test("quota dial is present as a progressbar", async ({ page }) => {
    const dash = new DashboardPage(page);
    await dash.goto();
    await expect(dash.quotaDial).toBeVisible();
  });

  test("three stat tiles are visible", async ({ page }) => {
    const dash = new DashboardPage(page);
    await dash.goto();
    await expect(dash.activeBroadcastsTile).toBeVisible();
    await expect(dash.activePromotionsTile).toBeVisible();
    await expect(dash.ingestionInFlightTile).toBeVisible();
  });

  test("recent conversations card is visible", async ({ page }) => {
    const dash = new DashboardPage(page);
    await dash.goto();
    await expect(dash.recentConversationsCard).toBeVisible();
  });

  test("reply sparkline SVG is visible", async ({ page }) => {
    const dash = new DashboardPage(page);
    await dash.goto();
    await expect(dash.sparkline).toBeVisible();
  });

  test("no whitelabel brand strings present", async ({ page }) => {
    const dash = new DashboardPage(page);
    await dash.goto();
    await dash.assertNoBrandLeak();
  });

  test("no suspension banner on a healthy tenant", async ({ page }) => {
    const dash = new DashboardPage(page);
    await dash.goto();
    // proC is a healthy pro tenant — SuspensionBanner renders null,
    // so role="alert" (excluding route-announcer) must be absent.
    await expect(dash.suspensionBanner).not.toBeVisible();
  });
});

// ---------------------------------------------------------------------------
// Empty state — recent conversations list empty
// ---------------------------------------------------------------------------

test.describe("Dashboard empty recent conversations [pro]", () => {
  test.skip(() => !proC.state, "pro-c storage state not minted");
  test.use({ storageState: requireState(proC) });

  test("empty recent conversations shows connect-a-channel link", async ({
    page,
  }) => {
    await setE2EFault(page, [
      {
        match: "dashboard/summary",
        body: {
          quota: {
            bot_replies_used: 0,
            bot_replies_cap: 1000,
            knowledge_chunks_used_mb: 0,
            knowledge_chunks_cap_mb: 100,
            broadcasts_sent_this_period: 0,
            bookings_made_this_period: 0,
          },
          recent_conversations: [],
          active_broadcasts_count: 0,
          active_promotions_count: 0,
          ingestion_jobs_in_flight: 0,
          quota_replies_7d_sparkline: [0, 0, 0, 0, 0, 0, 0],
          suspension_state: null,
        },
      },
    ]);

    const dash = new DashboardPage(page);
    await dash.goto();
    await expect(page.getByText(/no conversations yet/i)).toBeVisible();
    await expect(
      page.getByRole("link", { name: /connect a channel/i }),
    ).toBeVisible();
  });
});

// ---------------------------------------------------------------------------
// Suspension banner — quota_exhausted / payment_overdue
// ---------------------------------------------------------------------------

test.describe("Dashboard suspension banner [pro]", () => {
  test.skip(() => !proC.state, "pro-c storage state not minted");
  test.use({ storageState: requireState(proC) });

  test("quota_exhausted suspension banner is visible", async ({ page }) => {
    await setE2EFault(page, [
      {
        match: "dashboard/summary",
        body: {
          quota: {
            bot_replies_used: 1000,
            bot_replies_cap: 1000,
            knowledge_chunks_used_mb: 0,
            knowledge_chunks_cap_mb: 100,
            broadcasts_sent_this_period: 0,
            bookings_made_this_period: 0,
          },
          recent_conversations: [],
          active_broadcasts_count: 0,
          active_promotions_count: 0,
          ingestion_jobs_in_flight: 0,
          quota_replies_7d_sparkline: [0, 0, 0, 0, 0, 0, 0],
          suspension_state: "quota_exhausted",
        },
      },
    ]);

    const dash = new DashboardPage(page);
    await dash.goto();
    await expect(dash.suspensionBanner).toBeVisible();
    await expect(dash.suspensionBanner).toContainText(/bot quota exhausted/i);
    // Upgrade link present
    await expect(
      page.getByRole("link", { name: /upgrade plan/i }),
    ).toBeVisible();
  });

  test("payment_overdue banner shows settle payment link", async ({ page }) => {
    await setE2EFault(page, [
      {
        match: "dashboard/summary",
        body: {
          quota: {
            bot_replies_used: 0,
            bot_replies_cap: 1000,
            knowledge_chunks_used_mb: 0,
            knowledge_chunks_cap_mb: 100,
            broadcasts_sent_this_period: 0,
            bookings_made_this_period: 0,
          },
          recent_conversations: [],
          active_broadcasts_count: 0,
          active_promotions_count: 0,
          ingestion_jobs_in_flight: 0,
          quota_replies_7d_sparkline: [0, 0, 0, 0, 0, 0, 0],
          suspension_state: "payment_overdue",
        },
      },
    ]);

    const dash = new DashboardPage(page);
    await dash.goto();
    await expect(dash.suspensionBanner).toBeVisible();
    await expect(
      page.getByRole("link", { name: /settle payment/i }),
    ).toBeVisible();
  });
});

// ---------------------------------------------------------------------------
// Fetch-error banner
// ---------------------------------------------------------------------------

test.describe("Dashboard fetch-error banner [pro]", () => {
  test.skip(() => !proC.state, "pro-c storage state not minted");
  test.use({ storageState: requireState(proC) });

  test("503 on summary API shows error banner, shell stays usable", async ({
    page,
  }) => {
    await setE2EFault(page, [{ match: "dashboard/summary", status: 503 }]);

    const dash = new DashboardPage(page);
    await dash.goto();
    await expect(dash.fetchErrorBanner).toBeVisible();
    await expect(dash.fetchErrorBanner).toContainText(
      /having trouble loading your dashboard/i,
    );
    // Shell (heading) still renders
    await expect(dash.heading).toBeVisible();
  });
});

// ---------------------------------------------------------------------------
// Fresh tenant → /onboarding redirect
// ---------------------------------------------------------------------------

test.describe("Dashboard fresh-tenant onboarding redirect [ultra]", () => {
  test.skip(() => !ultra.state, "ultra storage state not minted");
  test.use({ storageState: requireState(ultra) });

  test("tenant with zero channels and no skip cookie is redirected to /onboarding", async ({
    page,
  }) => {
    // /dashboard checks channels server-side (shouldRedirectToOnboarding).
    // Inject zero channels via the e2e_fault hook so the SSR fetch returns an
    // empty per_kind. We deliberately do NOT clearCookies() — that would drop
    // the NextAuth session and bounce to /login. A fresh context has no
    // onboarding-skip cookie, so zero channels → redirect to /onboarding.
    await setE2EFault(page, [
      {
        match: "tenant/channels",
        body: { per_kind: { whatsapp: 0, instagram: 0, messenger: 0 } },
      },
    ]);

    await page.goto("/dashboard");
    await expect(page).toHaveURL(/\/onboarding/);
  });
});
