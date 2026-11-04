/**
 * Page Object: /audit and /audit/verify
 *
 * Source: apps/admin/app/(admin)/audit/page.tsx
 *         apps/admin/app/(admin)/audit/verify/page.tsx
 *         apps/admin/components/audit/AuditTable.tsx
 *         apps/admin/components/audit/VerificationBanner.tsx
 *         apps/admin/components/audit/OnDemandReverify.tsx
 */

import type { Locator, Page } from "@playwright/test";
import { BasePage } from "../_support/pom";

export class AuditPage extends BasePage {
  // ── /audit ──────────────────────────────────────────────────────────────

  /** h1 "Audit Log" */
  readonly heading: Locator;

  /**
   * VerificationBanner (data-testid="verification-banner").
   * role="status" (intact/errored) or role="alert" (broken).
   */
  readonly verificationBanner: Locator;

  /** "Verify now" button (OnDemandReverify component). */
  readonly verifyNowBtn: Locator;

  /** Live-region status message next to the verify button. */
  readonly verifyStatusMsg: Locator;

  /** The audit log table */
  readonly auditTable: Locator;

  /** All data rows in the audit table (excludes header row) */
  readonly auditRows: Locator;

  /**
   * "Load more" cursor-pagination button.
   * Only visible when next_cursor is non-null.
   */
  readonly loadMoreBtn: Locator;

  // ── /audit/verify ───────────────────────────────────────────────────────

  /** h1 "Chain Integrity Verification" (on /audit/verify) */
  readonly verifyHeading: Locator;

  /** "← Audit log" back link */
  readonly backToAuditLink: Locator;

  /** "Last run details" dl section */
  readonly lastRunDetails: Locator;

  constructor(page: Page) {
    super(page);

    // /audit
    this.heading = page.getByRole("heading", { name: /^audit log$/i });
    this.verificationBanner = page.getByTestId("verification-banner");
    this.verifyNowBtn = page.getByRole("button", { name: /verify now/i });
    this.verifyStatusMsg = page.getByRole("status").filter({
      has: page.locator('[aria-live="polite"]'),
    });
    this.auditTable = page.getByRole("table");
    this.auditRows = page
      .getByRole("table")
      .getByRole("row")
      .filter({
        hasNot: page.getByRole("columnheader", { name: /time/i }).locator(".."),
      });
    this.loadMoreBtn = page.getByRole("button", { name: /load more/i });

    // /audit/verify
    this.verifyHeading = page.getByRole("heading", {
      name: /chain integrity verification/i,
    });
    // Scope to the <main> element to avoid collision with the sidebar nav link
    // "Audit Log" which also has href="/audit". The back link lives in <main>.
    this.backToAuditLink = page
      .getByRole("main")
      .getByRole("link", { name: /audit log/i });
    // The "Last run details" h2 lives inside a conditional div; scope to the
    // containing section so strict-mode doesn't collide with other headings.
    this.lastRunDetails = page
      .locator("div")
      .filter({
        has: page.getByRole("heading", { name: /^last run details$/i }),
      })
      .getByRole("heading", { name: /^last run details$/i });
  }

  async goto(tenantId?: string): Promise<void> {
    const url = tenantId ? `/audit?tenant_id=${tenantId}` : "/audit";
    await super.goto(url);
  }

  async gotoVerify(): Promise<void> {
    await super.goto("/audit/verify");
  }

  /** Click "Verify now" and wait for the status message to update. */
  async triggerVerify(): Promise<void> {
    await this.verifyNowBtn.click();
    // OnDemandReverify sets msg after the POST resolves (typically <5s).
    // We also rely on the page.refresh() that fires after 3s.
    await this.page.waitForLoadState("networkidle", { timeout: 20_000 });
  }

  /** Wait for the VerificationBanner to reach a specific status text. */
  async waitForBannerStatus(pattern: RegExp): Promise<void> {
    await this.verificationBanner
      .filter({ hasText: pattern })
      .waitFor({ timeout: 15_000 });
  }

  /** Click "Load more" and wait for the new rows to appear. */
  async loadMore(): Promise<void> {
    const prevCount = await this.page.getByRole("row").count();
    await this.loadMoreBtn.click();
    // Wait until at least one new row is appended.
    await this.page.waitForFunction(
      (prev) => document.querySelectorAll("tbody tr").length > prev,
      prevCount,
      { timeout: 10_000 },
    );
  }

  /** Column headers for asserting table structure. */
  columnHeader(name: RegExp | string): Locator {
    return this.page.getByRole("columnheader", { name });
  }
}
