/**
 * DashboardPage POM — /dashboard (rebuilt suite).
 *
 * Selectors grounded in:
 *   - apps/portal/app/(portal)/dashboard/page.tsx
 *   - apps/portal/components/dashboard/widgets.tsx
 *
 * Key ARIA anchors from the markup:
 *   - QuotaDial:       role="progressbar" aria-label="Bot replies used: …"
 *   - SuspensionBanner: role="alert" (both variants)
 *   - Fetch-error:      role="status" (warning text)
 *   - RecentConvs card: CardTitle "Recent conversations"
 *   - Sparkline card:   role="img" aria-label="Reply count for the last 7 days…"
 *   - Stat tiles:       text "Active broadcasts" / "Active promotions" / "Ingestion in flight"
 */

import type { Page } from "@playwright/test";
import { BasePage } from "../_support/pom";

export class DashboardPage extends BasePage {
  constructor(page: Page) {
    super(page);
  }

  async goto() {
    await super.goto("/dashboard");
  }

  get heading() {
    return this.page.getByRole("heading", { name: /^dashboard$/i });
  }

  /** QuotaDial progressbar — aria-label contains "Bot replies used" */
  get quotaDial() {
    return this.page.getByRole("progressbar", {
      name: /bot replies used/i,
    });
  }

  /** SuspensionBanner — role="alert" when suspension_state is set.
   * Excludes the Next.js route-announcer (id="__next-route-announcer__"). */
  get suspensionBanner() {
    return this.page.locator('[role="alert"]:not(#__next-route-announcer__)');
  }

  /** Fetch-error banner — role="status" with warning text */
  get fetchErrorBanner() {
    return this.page.getByRole("status");
  }

  get recentConversationsCard() {
    return this.page.getByText(/recent conversations/i).first();
  }

  /** Sparkline SVG — role="img" aria-label starts with "Reply count" */
  get sparkline() {
    return this.page.getByRole("img", { name: /reply count/i });
  }

  /** Active broadcasts stat tile */
  get activeBroadcastsTile() {
    return this.page.getByText(/active broadcasts/i).first();
  }

  /** Active promotions stat tile */
  get activePromotionsTile() {
    return this.page.getByText(/active promotions/i).first();
  }

  /** Ingestion in flight stat tile */
  get ingestionInFlightTile() {
    return this.page.getByText(/ingestion in flight/i).first();
  }
}
