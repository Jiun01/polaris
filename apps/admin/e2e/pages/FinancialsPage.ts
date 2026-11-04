/**
 * Page Object: /financials
 *
 * Source: apps/admin/app/(admin)/financials/page.tsx
 *         apps/admin/components/financials/MrrOverviewCard.tsx
 */

import type { Locator, Page } from "@playwright/test";
import { BasePage } from "../_support/pom";

export class FinancialsPage extends BasePage {
  /** h1 "Financials" */
  readonly heading: Locator;

  /** MRR big-number display (data-testid="mrr-big-number") */
  readonly mrrBigNumber: Locator;

  /** "Recompute now" button inside MrrOverviewCard */
  readonly recomputeBtn: Locator;

  /** 30-day trend table container */
  readonly trendTable: Locator;

  /** Error card (API load failure) */
  readonly errorCard: Locator;

  constructor(page: Page) {
    super(page);
    this.heading = page.getByRole("heading", { name: /^financials$/i });
    this.mrrBigNumber = page.getByTestId("mrr-big-number");
    this.recomputeBtn = page.getByRole("button", { name: /recompute now/i });
    this.trendTable = page.getByRole("table");
    this.errorCard = page.getByText(/failed to load mrr data/i);
  }

  async goto(): Promise<void> {
    await super.goto("/financials");
  }

  /** Wait for the MRR big number to contain a specific value pattern. */
  async waitForMrrValue(pattern: RegExp): Promise<void> {
    await this.mrrBigNumber.filter({ hasText: pattern }).waitFor({
      timeout: 10_000,
    });
  }

  /** Click "Recompute now" and wait for the loading state to resolve. */
  async recompute(): Promise<void> {
    await this.recomputeBtn.click();
    // Button text toggles to "Computing…" while in flight; wait for it to
    // return to "Recompute now" (or an error) before proceeding.
    await this.page
      .getByRole("button", { name: /recompute now/i })
      .waitFor({ timeout: 15_000 });
  }
}
