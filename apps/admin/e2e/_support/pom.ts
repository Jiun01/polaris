/**
 * Base Page Object for admin specs (rebuilt suite).
 *
 * Page POMs under e2e/pages/*.ts extend BasePage. Keep locators + actions in
 * the POM, assertions in the spec. Prefer role/label/text + data-testid.
 */

import type { Locator, Page } from "@playwright/test";

import { FORBIDDEN_BRAND_STRINGS } from "./fixtures";

export class BasePage {
  readonly page: Page;
  readonly sidebar: Locator;
  readonly topbar: Locator;
  /** Impersonation banner (visible only while impersonating a tenant). */
  readonly impersonationBanner: Locator;

  constructor(page: Page) {
    this.page = page;
    this.sidebar = page.getByRole("navigation");
    this.topbar = page.getByRole("banner");
    this.impersonationBanner = page.getByText(/impersonating/i);
  }

  async goto(pathname: string): Promise<void> {
    await this.page.goto(pathname);
    await this.page.waitForLoadState("networkidle");
  }

  async nav(name: RegExp | string): Promise<void> {
    await this.sidebar.getByRole("link", { name }).click();
  }

  async assertNoBrandLeak(): Promise<void> {
    const body = (await this.page.locator("body").innerText()).toLowerCase();
    for (const s of FORBIDDEN_BRAND_STRINGS) {
      if (body.includes(s.toLowerCase())) {
        throw new Error(`Whitelabel leak: "${s}" found on ${this.page.url()}`);
      }
    }
  }

  toast(): Locator {
    return this.page.getByRole("alert");
  }
}
