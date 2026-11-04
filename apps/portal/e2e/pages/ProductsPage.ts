/**
 * ProductsPage POM — /products (Phase 15 PR-8).
 *
 * Selectors grounded in:
 *   - apps/portal/app/(portal)/products/page.tsx
 *   - apps/portal/app/(portal)/products/new/page.tsx
 *   - apps/portal/app/(portal)/products/[product_id]/page.tsx
 *   - apps/portal/components/products/ProductForm.tsx
 */

import type { Page } from "@playwright/test";
import { BasePage } from "../_support/pom";

export class ProductsPage extends BasePage {
  constructor(page: Page) {
    super(page);
  }

  async goto() {
    await super.goto("/products");
  }

  async gotoNew() {
    await super.goto("/products/new");
  }

  async gotoEdit(productId: string) {
    await super.goto(`/products/${productId}`);
  }

  // ── List page ──────────────────────────────────────────────────────────────

  get heading() {
    return this.page.getByRole("heading", { name: /^products$/i });
  }

  get newProductButton() {
    return this.page.getByRole("link", { name: /new product/i });
  }

  get fetchErrorBanner() {
    return this.page.getByRole("status");
  }

  get emptyHeading() {
    return this.page.getByRole("heading", { name: /no products yet/i });
  }

  get createFirstProductLink() {
    return this.page.getByRole("link", { name: /add your first product/i });
  }

  productCard(name: string) {
    return this.page.getByRole("link", { name: new RegExp(name, "i") });
  }

  // ── Create / Edit form (ProductForm) ────────────────────────────────────────

  get newProductHeading() {
    return this.page.getByRole("heading", { name: /^new product$/i });
  }

  get editHeading() {
    return this.page.getByRole("heading", { name: /^edit product$/i });
  }

  get nameInput() {
    return this.page.locator("#p-name");
  }

  get skuInput() {
    return this.page.locator("#p-sku");
  }

  get priceInput() {
    return this.page.locator("#p-price");
  }

  get descriptionTextarea() {
    return this.page.locator("#p-desc");
  }

  /** Branch dropdown — only rendered when branches.length > 0 */
  get branchSelect() {
    return this.page.locator("#p-branch");
  }

  get isActiveCheckbox() {
    return this.page.getByRole("checkbox", {
      name: /active.*bot can surface/i,
    });
  }

  get createButton() {
    return this.page.getByRole("button", { name: /create product/i });
  }

  get saveChangesButton() {
    return this.page.getByRole("button", { name: /save changes/i });
  }

  get deleteButton() {
    return this.page.getByRole("button", { name: /^delete$/i });
  }

  /** "Add photo" file-input label (edit mode only) */
  get addPhotoLabel() {
    return this.page.getByText(/add photo/i);
  }

  get formErrorAlert() {
    return this.page.getByRole("alert");
  }
}
