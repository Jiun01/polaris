/**
 * PlanChangesPage POM — /plan-changes, /plan-changes/[request_id]
 *
 * Selectors confirmed against:
 *   apps/admin/app/(admin)/plan-changes/page.tsx
 *   apps/admin/app/(admin)/plan-changes/[request_id]/page.tsx
 *   apps/admin/components/plan-changes/ProrationRecomputeForm.tsx
 */

import type { Locator, Page } from "@playwright/test";

import { BasePage } from "../_support/pom";

export class PlanChangesPage extends BasePage {
  readonly heading: Locator;

  constructor(page: Page) {
    super(page);
    // h1 from plan-changes/page.tsx: "Plan Changes"
    this.heading = page.getByRole("heading", { name: /^plan changes$/i });
  }

  async goto(): Promise<void> {
    await this.page.goto("/plan-changes");
    await this.page.waitForLoadState("networkidle");
  }

  /** Locate a row in the pending queue by request-id prefix or tenant name. */
  rowByText(text: string): Locator {
    return this.page.getByRole("row").filter({ hasText: text });
  }
}

export class PlanChangeDetailPage extends BasePage {
  // ProrationRecomputeForm.tsx — step-1 button: "Recompute proration"
  readonly recomputeButton: Locator;
  // Step-2 action buttons
  readonly approveButton: Locator;
  readonly rejectButton: Locator;
  // Reject form
  readonly decisionNoteTextarea: Locator;
  readonly confirmRejectButton: Locator;
  readonly cancelRejectButton: Locator;
  // Success / decided cards — role="status" from ProrationRecomputeForm.tsx
  readonly resultCard: Locator;
  // Already-decided notice (non-pending state message)
  readonly alreadyDecidedNotice: Locator;
  // Inline error — role="alert" aria-live="assertive"
  readonly errorAlert: Locator;

  constructor(page: Page) {
    super(page);
    this.recomputeButton = page.getByRole("button", {
      name: /recompute proration/i,
    });
    this.approveButton = page.getByRole("button", { name: /^approve$/i });
    this.rejectButton = page.getByRole("button", { name: /^reject$/i });
    // id="decision-note" from ProrationRecomputeForm.tsx
    this.decisionNoteTextarea = page.locator("#decision-note");
    this.confirmRejectButton = page.getByRole("button", {
      name: /confirm reject/i,
    });
    this.cancelRejectButton = page.getByRole("button", { name: /^cancel$/i });
    // role="status" card after approve or reject
    this.resultCard = page.getByRole("status");
    // Non-pending text: "This request has already been"
    this.alreadyDecidedNotice = page.getByText(
      /this request has already been/i,
    );
    // role="alert" aria-live="assertive" for API errors (from ProrationRecomputeForm).
    // Exclude the Next.js route announcer (#__next-route-announcer__) which also
    // carries role="alert" aria-live="assertive" but is always empty.
    this.errorAlert = page
      .locator('[role="alert"][aria-live="assertive"]')
      .filter({ hasNot: page.locator("#__next-route-announcer__") })
      .first();
  }

  async goto(requestId: string): Promise<void> {
    await this.page.goto(`/plan-changes/${requestId}`);
    await this.page.waitForLoadState("networkidle");
  }

  /** Proration snapshot section header text */
  prorationSnapshotCard(): Locator {
    return this.page.getByText(/proration snapshot/i);
  }
}
