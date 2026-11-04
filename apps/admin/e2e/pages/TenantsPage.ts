/**
 * TenantsPage + TenantCreateWizardPage POMs.
 *
 * Selectors confirmed against:
 *   apps/admin/app/(admin)/tenants/page.tsx
 *   apps/admin/components/tenants/CreateWizard.tsx
 */

import type { Locator, Page } from "@playwright/test";

import { BasePage } from "../_support/pom";

export class TenantsPage extends BasePage {
  readonly heading: Locator;
  readonly newTenantButton: Locator;

  constructor(page: Page) {
    super(page);
    // h1 from tenants/page.tsx: "Tenants"
    this.heading = page.getByRole("heading", { name: /^tenants$/i });
    // Button/link text: "New tenant"
    this.newTenantButton = page.getByRole("link", { name: /new tenant/i });
  }

  async goto(): Promise<void> {
    await this.page.goto("/tenants");
    await this.page.waitForLoadState("networkidle");
  }

  /** Click a status-filter tab by its text (all | active | suspended | deleted). */
  statusTab(name: "all" | "active" | "suspended" | "deleted"): Locator {
    return this.page.getByRole("link", { name: new RegExp(`^${name}$`, "i") });
  }

  /** Locate a table row containing a given slug or display name substring. */
  rowByText(text: string): Locator {
    return this.page.getByRole("row").filter({ hasText: text });
  }

  /** Click the first link in a row that matches the given text to navigate to detail. */
  async openRowDetail(text: string): Promise<void> {
    await this.rowByText(text).getByRole("link").first().click();
  }
}

export class TenantCreateWizardPage extends BasePage {
  // Step 1 — Business profile
  // Input ids from CreateWizard.tsx: id="display_name", id="slug", etc.
  readonly displayNameInput: Locator;
  readonly slugInput: Locator;
  readonly contactEmailInput: Locator;
  readonly contactPhoneInput: Locator;
  readonly verticalInput: Locator;

  // Step 3 — Admin user
  readonly adminEmailInput: Locator;
  readonly adminDisplayNameInput: Locator;

  // Navigation buttons
  readonly nextButton: Locator;
  readonly backButton: Locator;
  readonly submitButton: Locator;

  // Validation hint — role="alert" paragraph in wizard
  readonly validationHint: Locator;

  constructor(page: Page) {
    super(page);
    // Labels from CreateWizard.tsx Field components
    this.displayNameInput = page.getByLabel(/business name/i);
    this.slugInput = page.getByLabel(/^slug$/i);
    this.contactEmailInput = page.getByLabel(/contact email/i);
    this.contactPhoneInput = page.getByLabel(/contact phone/i);
    this.verticalInput = page.getByLabel(/^vertical$/i);
    this.adminEmailInput = page.getByLabel(/^admin email$/i);
    this.adminDisplayNameInput = page.getByLabel(/admin display name/i);

    // Button texts from CreateWizard.tsx
    this.nextButton = page.getByRole("button", { name: /^next$/i });
    this.backButton = page.getByRole("button", { name: /^back$/i });
    // Final step CTA: "Create tenant & send link"
    this.submitButton = page.getByRole("button", {
      name: /create tenant.*send link/i,
    });
    this.validationHint = page.getByRole("alert");
  }

  async goto(): Promise<void> {
    await this.page.goto("/tenants/new");
    await this.page.waitForLoadState("networkidle");
  }

  /** Step 2: click a tier option by name (starter | pro | ultra | enterprise).
   * The tier labels are rendered inside <label> wrappers in a radiogroup with
   * aria-label="Tier". */
  async selectTier(
    tier: "starter" | "pro" | "ultra" | "enterprise",
  ): Promise<void> {
    await this.page
      .getByRole("radiogroup", { name: /^tier$/i })
      .getByText(tier, { exact: true })
      .click();
  }

  /** Step 2: click a billing-cycle button (monthly | annual). */
  async selectBillingCycle(cycle: "monthly" | "annual"): Promise<void> {
    await this.page
      .getByRole("button", { name: new RegExp(`^${cycle}$`, "i") })
      .click();
  }
}
