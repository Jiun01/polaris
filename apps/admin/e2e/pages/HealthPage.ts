/**
 * Page Object: /health (+ /system-health redirect)
 *
 * Source: apps/admin/app/(admin)/health/page.tsx
 *         apps/admin/app/(admin)/system-health/page.tsx  (redirect → /health)
 *         apps/admin/components/health/HealthDashboard.tsx
 *         apps/admin/components/health/SupabaseStatusTile.tsx
 *         apps/admin/components/health/RedisStatusTile.tsx
 *         apps/admin/components/health/BroadcastQueueDepthTile.tsx
 *         apps/admin/components/health/MessagingVolumeTile.tsx
 *         apps/admin/components/health/LlmTokenBurnTile.tsx
 *         apps/admin/components/health/QuotaSaturationTile.tsx
 *         apps/admin/components/health/RecentFailuresTile.tsx
 *         apps/admin/components/health/MetaRateLimitTile.tsx
 */

import type { Locator, Page } from "@playwright/test";
import { BasePage } from "../_support/pom";

export class HealthPage extends BasePage {
  /** h1 "System Health" */
  readonly heading: Locator;

  /**
   * Last-refreshed timestamp (data-testid="last-refreshed-timestamp").
   * HealthDashboard.tsx line 113.
   */
  readonly lastRefreshedTimestamp: Locator;

  /**
   * "Refresh now" button in the toolbar.
   * Becomes "Refreshing…" while loading.
   */
  readonly refreshNowBtn: Locator;

  // ── tiles ──────────────────────────────────────────────────────────────
  readonly supabaseTile: Locator;
  readonly redisTile: Locator;
  readonly broadcastQueueTile: Locator;
  readonly messagingTile: Locator;
  readonly llmTile: Locator;
  readonly quotaTile: Locator;
  readonly failuresTile: Locator;
  readonly metaRateTile: Locator;
  readonly webhookEventErrorsTile: Locator;

  // ── error / loading ──────────────────────────────────────────────────────

  /** Non-fatal error banner that appears when a refresh fails */
  readonly errorBanner: Locator;

  constructor(page: Page) {
    super(page);
    this.heading = page.getByRole("heading", { name: /system health/i });
    this.lastRefreshedTimestamp = page.getByTestId("last-refreshed-timestamp");
    this.refreshNowBtn = page.getByRole("button", { name: /refresh now/i });

    // Tiles are CardTitle elements; match by text
    this.supabaseTile = page
      .getByText(/^supabase$/i)
      .filter({ has: page.locator("h3, [class*='CardTitle']") })
      .first();
    this.redisTile = page
      .getByText(/^redis$/i)
      .filter({ has: page.locator("h3, [class*='CardTitle']") })
      .first();
    this.broadcastQueueTile = page.getByText(/broadcast queue/i).first();
    this.messagingTile = page.getByText(/messaging volume/i).first();
    this.llmTile = page.getByText(/llm token burn/i).first();
    this.quotaTile = page.getByText(/quota saturation/i).first();
    this.failuresTile = page.getByText(/recent failures/i).first();
    this.metaRateTile = page.getByText(/meta rate limits/i).first();
    this.webhookEventErrorsTile = page
      .getByText(/webhook & event errors/i)
      .first();

    this.errorBanner = page.locator(".bg-destructive\\/10");
  }

  async goto(): Promise<void> {
    await super.goto("/health");
  }

  /** Navigate via the legacy /system-health alias (should redirect). */
  async gotoViaAlias(): Promise<void> {
    await this.page.goto("/system-health");
  }

  /**
   * Click "Refresh now" and wait for the timestamp to update.
   * Captures the timestamp text before clicking.
   */
  async refreshAndWaitForNewTimestamp(): Promise<void> {
    const before = await this.lastRefreshedTimestamp.textContent();
    await this.refreshNowBtn.click();
    // Wait until the timestamp differs from the captured value.
    await this.page.waitForFunction(
      ({ selector, prev }) => {
        const el = document.querySelector(`[data-testid="${selector}"]`);
        return el !== null && el.textContent !== prev;
      },
      { selector: "last-refreshed-timestamp", prev: before },
      { timeout: 10_000 },
    );
  }

  /**
   * Asserts all tile headings are visible within the given timeout.
   */
  async assertAllTilesVisible(timeout = 10_000): Promise<void> {
    for (const pattern of [
      /^supabase$/i,
      /^redis$/i,
      /broadcast queue/i,
      /messaging volume/i,
      /llm token burn/i,
      /quota saturation/i,
      /recent failures/i,
      /meta rate limits/i,
      /webhook & event errors/i,
    ]) {
      await this.page.getByText(pattern).first().waitFor({ timeout });
    }
  }
}
