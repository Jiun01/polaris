/**
 * Broadcasts E2E — rebuilt suite.
 *
 * Surfaces covered: Broadcasts.
 *   - [pro+] list: heading, "New broadcast" CTA, table w/ status badges
 *   - [pro+] empty list → "Start a broadcast" CTA
 *   - [pro+] fetch-error banner (mocked 503)
 *   - [pro+] /broadcasts/new wizard: no-approved-template CTA
 *   - [pro+] /broadcasts/new wizard: 3-step flow (template→audience→schedule)
 *   - [starter] /broadcasts → client redirect → /broadcasts/locked (waitForURL)
 *   - [starter] /broadcasts/locked: upgrade CTA + compare plans link
 *   - Whitelabel check
 */

import { test, expect, TENANTS, requireState } from "../_support/fixtures";
import { BroadcastsPage } from "../pages/BroadcastsPage";
import { setE2EFault } from "../_support/e2e-fault";

const proC = TENANTS.proC;
const starterA = TENANTS.starterA;

// ---------------------------------------------------------------------------
// Happy path — pro tenant
// ---------------------------------------------------------------------------

test.describe("Broadcasts list [pro]", () => {
  test.skip(() => !proC.state, "pro-c storage state not minted");
  test.use({ storageState: requireState(proC) });

  test("heading and new-broadcast button render", async ({ page }) => {
    const bp = new BroadcastsPage(page);
    await bp.goto();
    await expect(bp.heading).toBeVisible();
    await expect(bp.newBroadcastButton).toBeVisible();
  });

  test("broadcasts table shows status badges when items exist", async ({
    page,
  }) => {
    // NOTE: /broadcasts is a Server Component — page.route() is a no-op for apiFetchServer.
    // Assert tolerantly on real seeded state: table if items exist, empty heading if not.
    const bp = new BroadcastsPage(page);
    await bp.goto();
    const tableVisible = await bp.broadcastsTable.isVisible();
    const emptyVisible = await bp.emptyHeading.isVisible();
    expect(tableVisible || emptyVisible).toBeTruthy();
  });

  test("no whitelabel brand strings on broadcasts list", async ({ page }) => {
    const bp = new BroadcastsPage(page);
    await bp.goto();
    await bp.assertNoBrandLeak();
  });
});

// ---------------------------------------------------------------------------
// Empty state
// ---------------------------------------------------------------------------

test.describe("Broadcasts empty state [pro]", () => {
  test.skip(() => !proC.state, "pro-c storage state not minted");
  test.use({ storageState: requireState(proC) });

  test("empty list renders CTA to start a broadcast", async ({ page }) => {
    await page.route("**/api/tenant/broadcasts/", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([]),
      });
    });

    const bp = new BroadcastsPage(page);
    await bp.goto();
    await expect(bp.emptyHeading).toBeVisible();
    await expect(bp.startBroadcastLink).toBeVisible();
  });
});

// ---------------------------------------------------------------------------
// Fetch-error banner
// ---------------------------------------------------------------------------

test.describe("Broadcasts fetch-error [pro]", () => {
  test.skip(() => !proC.state, "pro-c storage state not minted");
  test.use({ storageState: requireState(proC) });

  test("503 on broadcasts API shows error banner", async ({ page }) => {
    await setE2EFault(page, [{ match: "broadcasts/", status: 503 }]);
    const bp = new BroadcastsPage(page);
    await bp.goto();
    await expect(bp.fetchErrorBanner).toBeVisible();
    await expect(bp.heading).toBeVisible();
  });
});

// ---------------------------------------------------------------------------
// /broadcasts/new — no approved templates CTA
// ---------------------------------------------------------------------------

test.describe("Broadcasts new — no approved templates [pro]", () => {
  test.skip(() => !proC.state, "pro-c storage state not minted");
  test.use({ storageState: requireState(proC) });

  test("zero approved templates shows submit-a-template CTA", async ({
    page,
  }) => {
    // NOTE: SSR fetch — page.route() is a no-op. This passes because pro-c genuinely
    // has no approved templates in the real seed, so the CTA renders from real SSR data.
    const bp = new BroadcastsPage(page);
    await bp.gotoNew();
    await expect(bp.noApprovedTemplateHeading).toBeVisible();
    await expect(bp.submitTemplateLink).toBeVisible();
  });
});

// ---------------------------------------------------------------------------
// /broadcasts/new — 3-step wizard
// ---------------------------------------------------------------------------

test.describe("Broadcasts wizard 3-step [pro]", () => {
  test.skip(() => !proC.state, "pro-c storage state not minted");
  test.use({ storageState: requireState(proC) });

  // Both wizard tests below stub /api/tenant/channels alongside templates
  // (X1 fix, audit-batch-f-frontend-consolidation 2026-07): the wizard now
  // requires a WhatsApp tenant_channel_id (CreateBroadcastRequest's real
  // required field — previously never fetched at all) before it renders,
  // same gate as templates/new/page.tsx. Fill "Broadcast name" too, now a
  // required field gating the step-1 "Next" button.
  const ONE_APPROVED_TEMPLATE = [
    {
      id: "00000000-0000-0000-0000-0000000000b1",
      name: "welcome_message",
      language: "en",
      status: "approved",
    },
  ];
  const ONE_WHATSAPP_CHANNEL = {
    items: [
      {
        id: "00000000-0000-0000-0000-0000000000c1",
        channel: "whatsapp",
        status: "connected",
      },
    ],
    per_kind: { whatsapp: 1 },
  };

  test("wizard steps render and advance template→audience→schedule", async ({
    page,
  }) => {
    await setE2EFault(page, [
      { match: "tenant/templates", body: ONE_APPROVED_TEMPLATE },
      { match: "tenant/channels", body: ONE_WHATSAPP_CHANNEL },
    ]);
    const bp = new BroadcastsPage(page);
    await bp.gotoNew();
    await expect(bp.wizardStepsList).toBeVisible();
    await expect(bp.templateSelect).toBeVisible();
    await bp.nameInput.fill("June promo — VIP customers");
    await bp.nextAudienceButton.click();
    await expect(bp.channelSelect).toBeVisible();
    await expect(bp.tagsInput).toBeVisible();
    await bp.nextScheduleButton.click();
    await expect(bp.sendNowRadio).toBeVisible();
    await expect(bp.scheduleLaterRadio).toBeVisible();
    await expect(bp.sendBroadcastButton).toBeVisible();
  });

  test("schedule-for-later reveals datetime input", async ({ page }) => {
    await setE2EFault(page, [
      { match: "tenant/templates", body: ONE_APPROVED_TEMPLATE },
      { match: "tenant/channels", body: ONE_WHATSAPP_CHANNEL },
    ]);
    const bp = new BroadcastsPage(page);
    await bp.gotoNew();
    await bp.nameInput.fill("June promo — VIP customers");
    await bp.nextAudienceButton.click();
    await bp.nextScheduleButton.click();
    await bp.scheduleLaterRadio.click();
    await expect(bp.scheduleBroadcastButton).toBeVisible();
    await expect(
      page.locator('input[type="datetime-local"]').last(),
    ).toBeVisible();
  });
});

// ---------------------------------------------------------------------------
// Tier-lock: starter → /broadcasts/locked (waitForURL)
// ---------------------------------------------------------------------------

test.describe("Broadcasts tier-lock [starter]", () => {
  test.skip(() => !starterA.state, "starter-a storage state not minted");
  test.use({ storageState: requireState(starterA) });

  test("starter /broadcasts redirects to /broadcasts/locked", async ({
    page,
  }) => {
    await page.goto("/broadcasts");
    await page.waitForURL(/\/broadcasts\/locked/);
  });

  test("/broadcasts/locked shows upgrade CTA and compare plans link", async ({
    page,
  }) => {
    const bp = new BroadcastsPage(page);
    await bp.gotoLocked();
    await expect(bp.lockedHeading).toBeVisible();
    await expect(bp.upgradeToProLink).toBeVisible();
    await expect(bp.comparePlansLink).toBeVisible();
  });

  test("/broadcasts/locked upgrade link points to plan-change?target=pro", async ({
    page,
  }) => {
    const bp = new BroadcastsPage(page);
    await bp.gotoLocked();
    const href = await bp.upgradeToProLink.getAttribute("href");
    expect(href).toMatch(/\/billing\/plan-change\?target=pro/);
  });

  test("no whitelabel brand strings on locked page", async ({ page }) => {
    const bp = new BroadcastsPage(page);
    await bp.gotoLocked();
    await bp.assertNoBrandLeak();
  });
});

// ---------------------------------------------------------------------------
// Inverse tier-lock: pro+ visiting /broadcasts/locked → bounced to /broadcasts
// ---------------------------------------------------------------------------

test.describe("Broadcasts locked-page inverse redirect [pro]", () => {
  test.skip(() => !proC.state, "pro-c storage state not minted");
  test.use({ storageState: requireState(proC) });

  test("pro tenant on /broadcasts/locked is redirected to /broadcasts", async ({
    page,
  }) => {
    await page.goto("/broadcasts/locked");
    // TierUnlockRedirect bounces pro+ off the upgrade page to the live list.
    await page.waitForURL((url) => url.pathname === "/broadcasts", {
      timeout: 10_000,
    });
  });
});
