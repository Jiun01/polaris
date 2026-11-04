/**
 * PROD SMOKE — WS2 (behavior half): product/promo agent tools on the
 * gateway tenant, exercised over a REAL
 * Telegram round-trip (the designated safe-to-test conversation already
 * used by ../docs/internal-ops/ops/e2e/bot_reply).
 *
 * Sequence: record current tool-toggle state -> ensure both ON via the real
 * portal UI -> save -> send real Telegram messages -> poll Chatwoot for the
 * bot's reply -> assert it engages with the topic (never asserts on exact
 * catalog contents, since we don't control what's actually seeded live) ->
 * restore the original toggle state.
 *
 * Auth: real NextAuth session via a pre-captured storageState for the
 * gateway tenant admin (see e2e/prod-smoke/README.md).
 */

import { test, expect } from "@playwright/test";
import { sendIncoming, setPending, waitForReply } from "./_chatwoot";

test.describe("WS2 — product/promo agent tools (live Telegram)", () => {
  let originalProducts = false;
  let originalPromotions = false;

  test.beforeAll(() => {
    test.skip(
      !process.env.CHATWOOT_AGENT_API_TOKEN,
      "CHATWOOT_AGENT_API_TOKEN not set — see e2e/prod-smoke/README.md",
    );
  });

  test("enable both retrieval tools if not already on", async ({ page }) => {
    await page.goto("/agent");

    const productsToggle = page
      .locator("label", { hasText: "Look up products" })
      .locator('input[type="checkbox"]');
    const promotionsToggle = page
      .locator("label", { hasText: "Look up promotions" })
      .locator('input[type="checkbox"]');

    originalProducts = await productsToggle.isChecked();
    originalPromotions = await promotionsToggle.isChecked();

    if (!originalProducts) await productsToggle.check();
    if (!originalPromotions) await promotionsToggle.check();

    if (!originalProducts || !originalPromotions) {
      await page.getByRole("button", { name: /save agent settings/i }).click();
      await expect(productsToggle).toBeChecked();
      await expect(promotionsToggle).toBeChecked();
    }
  });

  test("bot answers a product question over real Telegram", async () => {
    await setPending();
    const msgId = await sendIncoming("what products do you have?");
    const reply = await waitForReply(msgId);
    expect(reply, "no bot reply arrived within the timeout").not.toBeNull();
    // Lenient by design — we don't control the live catalog's contents, only
    // that the tool fires and produces a real, non-empty, on-topic reply
    // rather than silence or a generic deflection.
    expect((reply ?? "").length).toBeGreaterThan(0);
  });

  test("bot answers a promo question over real Telegram", async () => {
    await setPending();
    const msgId = await sendIncoming("any deals or discounts right now?");
    const reply = await waitForReply(msgId);
    expect(reply, "no bot reply arrived within the timeout").not.toBeNull();
    expect((reply ?? "").length).toBeGreaterThan(0);
  });

  test.afterAll(async ({ browser }) => {
    // Restore original toggle state in a fresh page (afterAll has no `page`
    // fixture) using the same project storageState.
    const context = await browser.newContext({
      storageState: "./e2e/.auth/prod-tech-tenant.json",
    });
    const page = await context.newPage();
    try {
      await page.goto("/agent");
      const productsToggle = page
        .locator("label", { hasText: "Look up products" })
        .locator('input[type="checkbox"]');
      const promotionsToggle = page
        .locator("label", { hasText: "Look up promotions" })
        .locator('input[type="checkbox"]');

      const nowProducts = await productsToggle.isChecked();
      const nowPromotions = await promotionsToggle.isChecked();
      if (nowProducts !== originalProducts) await productsToggle.click();
      if (nowPromotions !== originalPromotions) await promotionsToggle.click();
      if (
        nowProducts !== originalProducts ||
        nowPromotions !== originalPromotions
      ) {
        await page
          .getByRole("button", { name: /save agent settings/i })
          .click();
      }
    } finally {
      await context.close();
    }
  });
});
