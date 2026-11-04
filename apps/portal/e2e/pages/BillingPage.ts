/**
 * BillingPage POM — /billing (rebuilt suite).
 *
 * Selectors grounded in:
 *   - apps/portal/app/(portal)/billing/page.tsx
 *   - apps/portal/app/(portal)/billing/plan-change/page.tsx
 *   - apps/portal/components/billing/PlanChangeForm.tsx
 */

import type { Page } from "@playwright/test";
import { BasePage } from "../_support/pom";

export class BillingPage extends BasePage {
  constructor(page: Page) {
    super(page);
  }

  async goto() {
    await super.goto("/billing");
  }

  async gotoPlanChange(target?: string) {
    const qs = target ? `?target=${target}` : "";
    await super.goto(`/billing/plan-change${qs}`);
  }

  // ── Billing overview ──────────────────────────────────────────────────────────

  get heading() {
    return this.page.getByRole("heading", { name: /billing & usage/i });
  }

  /** Payment-overdue alert — role="alert" scoped to avoid Next.js route announcer */
  get paymentOverdueAlert() {
    return this.page.getByRole("alert").filter({ hasText: /payment overdue/i });
  }

  /** Fetch / summary error — role="status" */
  get fetchErrorBanner() {
    return this.page.getByRole("status");
  }

  get changePlanButton() {
    return this.page.getByRole("link", { name: /change plan/i });
  }

  get payLink() {
    return this.page.getByRole("link", { name: /payment page/i });
  }

  /** Usage progressbars — aria-label matches the stat label */
  usageDial(label: RegExp | string) {
    return this.page.getByRole("progressbar", { name: label });
  }

  get botRepliesDial() {
    return this.usageDial(/bot replies this month/i);
  }

  get knowledgeStorageDial() {
    return this.usageDial(/knowledge storage/i);
  }

  get invoicesSection() {
    return this.page.getByRole("heading", { name: /^invoices$/i });
  }

  // ── Plan-change page ──────────────────────────────────────────────────────────

  get planChangeHeading() {
    return this.page.getByRole("heading", { name: /^change plan$/i });
  }

  get planChangeBillingBackLink() {
    return this.page.getByRole("link", { name: /^billing$/i });
  }

  /** Tier tile button — aria-pressed="true/false" */
  tierButton(tier: "Starter" | "Pro" | "Ultra") {
    return this.page
      .getByRole("button", { name: new RegExp(tier, "i") })
      .and(this.page.locator("[aria-pressed]"));
  }

  get reasonTextarea() {
    return this.page.locator("#reason");
  }

  get submitPlanChangeButton() {
    return this.page.getByRole("button", { name: /^request .* plan$/i });
  }

  get planChangeSubmittedStatus() {
    return this.page.getByRole("status");
  }

  get planChangeErrorAlert() {
    return this.page.getByRole("alert");
  }
}
