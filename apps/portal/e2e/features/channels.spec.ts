/**
 * Channels E2E — rebuilt suite.
 *
 * Surfaces covered: Channels.
 *   - /channels: heading, WA/IG/MSG tiles with status counts (read-only)
 *   - Fetch-error banner
 *   - Whitelabel check
 */

import { test, expect, TENANTS, requireState } from "../_support/fixtures";
import { ChannelsPage } from "../pages/ChannelsPage";
import { setE2EFault } from "../_support/e2e-fault";

const proC = TENANTS.proC;

function makeChannelsResponse(overrides: Partial<Record<string, number>> = {}) {
  return {
    items: [],
    per_kind: {
      whatsapp: 1,
      instagram: 0,
      messenger: 0,
      ...overrides,
    },
  };
}

// ---------------------------------------------------------------------------
// Happy path
// ---------------------------------------------------------------------------

test.describe("Channels page [pro]", () => {
  test.skip(() => !proC.state, "pro-c storage state not minted");
  test.use({ storageState: requireState(proC) });

  test("heading and channel tiles render", async ({ page }) => {
    await page.route("**/api/tenant/channels", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(makeChannelsResponse()),
      });
    });

    const cp = new ChannelsPage(page);
    await cp.goto();
    await expect(cp.heading).toBeVisible();
    await expect(cp.whatsAppTile).toBeVisible();
    await expect(cp.instagramTile).toBeVisible();
    await expect(cp.messengerTile).toBeVisible();
  });

  test("no whitelabel brand strings on channels page", async ({ page }) => {
    const cp = new ChannelsPage(page);
    await cp.goto();
    await cp.assertNoBrandLeak();
  });
});

// ---------------------------------------------------------------------------
// Fetch-error banner
// ---------------------------------------------------------------------------

test.describe("Channels fetch-error [pro]", () => {
  test.skip(() => !proC.state, "pro-c storage state not minted");
  test.use({ storageState: requireState(proC) });

  test("503 on channels API shows error banner while heading stays visible", async ({
    page,
  }) => {
    await setE2EFault(page, [{ match: "tenant/channels", status: 503 }]);

    const cp = new ChannelsPage(page);
    await cp.goto();
    await expect(cp.fetchErrorBanner).toBeVisible();
    await expect(cp.fetchErrorBanner).toContainText(
      /having trouble loading your channels/i,
    );
    await expect(cp.heading).toBeVisible();
  });
});
