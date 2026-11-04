/**
 * Page Object: /quotas and /quotas/[tenant_id]
 *
 * Source: apps/admin/app/(admin)/quotas/page.tsx
 *         apps/admin/app/(admin)/quotas/[tenant_id]/page.tsx
 *         apps/admin/components/quotas/UsageTable.tsx
 *         apps/admin/components/quotas/OverrideForm.tsx
 */

import type { Locator, Page } from "@playwright/test";
import { BasePage } from "../_support/pom";

export class QuotasPage extends BasePage {
  // ── /quotas (usage table) ───────────────────────────────────────────────

  /** h1 "Quotas" */
  readonly heading: Locator;

  /** The usage table */
  readonly usageTable: Locator;

  /** All data rows in the usage table */
  readonly usageRows: Locator;

  /** Error card (API load failure) */
  readonly errorCard: Locator;

  // ── /quotas/[tenant_id] (override form) ─────────────────────────────────

  /** h1 "Quota Override: {tenant_name}" */
  readonly overrideHeading: Locator;

  /** "Back to Quotas" link */
  readonly backLink: Locator;

  /**
   * Column select dropdown (htmlFor="quota-column").
   * 16 options from QUOTA_COLUMNS in OverrideForm.tsx.
   */
  readonly columnSelect: Locator;

  /**
   * New value input (htmlFor="quota-value", type="number").
   */
  readonly valueInput: Locator;

  /** "Apply override" submit button */
  readonly applyBtn: Locator;

  /** Success message after a successful override */
  readonly successMsg: Locator;

  /** Error message after a failed override */
  readonly overrideErrorMsg: Locator;

  constructor(page: Page) {
    super(page);

    // /quotas
    this.heading = page.getByRole("heading", { name: /^quotas$/i });
    this.usageTable = page.getByRole("table");
    this.usageRows = page
      .getByRole("table")
      .getByRole("row")
      // skip header row
      .filter({
        hasNot: page
          .getByRole("columnheader", { name: /tenant/i })
          .locator(".."),
      });
    this.errorCard = page.getByText(/failed to load quota usage/i);

    // /quotas/[tenant_id]
    this.overrideHeading = page.getByRole("heading", {
      name: /quota override:/i,
    });
    this.backLink = page.getByRole("link", { name: /back to quotas/i });
    this.columnSelect = page.getByLabel(/column/i);
    this.valueInput = page.getByLabel(/new value/i);
    this.applyBtn = page.getByRole("button", { name: /apply override/i });
    this.successMsg = page.getByText(/overridden:/i);
    this.overrideErrorMsg = page.locator(".text-destructive");
  }

  async gotoList(): Promise<void> {
    await super.goto("/quotas");
  }

  async gotoOverride(tenantId: string): Promise<void> {
    await super.goto(`/quotas/${tenantId}`);
  }

  /** Navigate to a tenant's override page via the "Override" link in the table. */
  async clickOverrideForTenant(tenantId: string): Promise<void> {
    await this.page
      .getByRole("link", { name: /^override$/i })
      .filter({ has: this.page.locator(`[href="/quotas/${tenantId}"]`) })
      .click();
  }

  /**
   * Apply an override value, then wait for the success message.
   * @param column - one of the 16 QUOTA_COLUMNS values
   * @param value  - numeric string; blank string resets to 0
   */
  async applyOverride(column: string, value: string): Promise<void> {
    await this.columnSelect.selectOption(column);
    await this.valueInput.fill(value);
    await this.applyBtn.click();
    await this.successMsg.waitFor({ timeout: 10_000 });
  }

  /**
   * Reset a quota column back to 0 (blank value resets).
   */
  async resetOverride(column: string): Promise<void> {
    await this.columnSelect.selectOption(column);
    await this.valueInput.fill("");
    await this.applyBtn.click();
    await this.successMsg.waitFor({ timeout: 10_000 });
  }

  /** Column headers in the usage table. */
  columnHeader(name: RegExp | string): Locator {
    return this.page.getByRole("columnheader", { name });
  }
}
