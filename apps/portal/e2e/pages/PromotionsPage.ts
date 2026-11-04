/**
 * PromotionsPage POM — /promotions (rebuilt suite).
 *
 * Selectors grounded in:
 *   - apps/portal/app/(portal)/promotions/page.tsx
 *   - apps/portal/app/(portal)/promotions/new/page.tsx
 *   - apps/portal/app/(portal)/promotions/[promo_id]/page.tsx
 *   - apps/portal/components/promotions/PromotionForm.tsx
 */

import type { Page } from "@playwright/test";
import { BasePage } from "../_support/pom";

export class PromotionsPage extends BasePage {
  constructor(page: Page) {
    super(page);
  }

  async goto() {
    await super.goto("/promotions");
  }

  async gotoNew() {
    await super.goto("/promotions/new");
  }

  async gotoEdit(promoId: string) {
    await super.goto(`/promotions/${promoId}`);
  }

  // ── List page ────────────────────────────────────────────────────────────────

  get heading() {
    return this.page.getByRole("heading", { name: /^promotions$/i });
  }

  get newPromotionButton() {
    return this.page.getByRole("link", { name: /new promotion/i });
  }

  get fetchErrorBanner() {
    return this.page.getByRole("status");
  }

  get emptyHeading() {
    return this.page.getByRole("heading", { name: /no promotions yet/i });
  }

  get createFirstPromotionLink() {
    return this.page.getByRole("link", {
      name: /create your first promotion/i,
    });
  }

  /** Card grid — each promotion is a link to /promotions/:id */
  promotionCard(title: string) {
    return this.page.getByRole("link", { name: new RegExp(title, "i") });
  }

  // ── Create / Edit form (PromotionForm) ────────────────────────────────────────

  get newPromotionHeading() {
    return this.page.getByRole("heading", { name: /^new promotion$/i });
  }

  get editHeading() {
    return this.page.getByRole("heading", { name: /^edit promotion$/i });
  }

  /** @deprecated alias kept for backward compat */
  get editPromotionHeading() {
    return this.editHeading;
  }

  get titleInput() {
    return this.page.locator("#p-title");
  }

  get discountInput() {
    return this.page.locator("#p-discount");
  }

  get descriptionTextarea() {
    return this.page.locator("#p-desc");
  }

  get validFromInput() {
    return this.page.locator("#p-from");
  }

  get validUntilInput() {
    return this.page.locator("#p-until");
  }

  get termsTextarea() {
    return this.page.locator("#p-terms");
  }

  /** Branch dropdown — only rendered when branches.length > 0 */
  get branchSelect() {
    return this.page.locator("#p-branch");
  }

  /** "Active" checkbox — no id, identified by label text */
  get isActiveCheckbox() {
    return this.page.getByRole("checkbox", {
      name: /active.*bot can surface/i,
    });
  }

  get createButton() {
    return this.page.getByRole("button", { name: /create promotion/i });
  }

  get saveChangesButton() {
    return this.page.getByRole("button", { name: /save changes/i });
  }

  get deleteButton() {
    return this.page.getByRole("button", { name: /^delete$/i });
  }

  get formErrorAlert() {
    return this.page.getByRole("alert");
  }
}
