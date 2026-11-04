/**
 * Base Page Object for portal specs (rebuilt suite).
 *
 * Page POMs under e2e/pages/*.ts extend BasePage. Keep locators + actions in
 * the POM; keep assertions in the spec. Prefer role/label/text locators and
 * data-testid; avoid brittle CSS/nth-child.
 */

import type { Locator, Page } from "@playwright/test";

import { FORBIDDEN_BRAND_STRINGS } from "./fixtures";

export class BasePage {
  readonly page: Page;
  /** Primary left-nav (role=navigation name=/primary/i). */
  readonly sidebar: Locator;
  /** Top bar containing the notification bell + account menu. */
  readonly topbar: Locator;

  constructor(page: Page) {
    this.page = page;
    this.sidebar = page.getByRole("navigation", { name: /primary/i });
    this.topbar = page.getByRole("banner");
  }

  /** Navigate to a portal path and settle network. */
  async goto(pathname: string): Promise<void> {
    await this.page.goto(pathname);
    await this.page.waitForLoadState("networkidle");
  }

  /** Click a primary-nav link by accessible name. */
  async nav(name: RegExp | string): Promise<void> {
    await this.sidebar.getByRole("link", { name }).click();
  }

  /** Assert no whitelabel leak (Chatwoot / Easy!Appointments) in body text. */
  async assertNoBrandLeak(): Promise<void> {
    const body = (await this.page.locator("body").innerText()).toLowerCase();
    for (const s of FORBIDDEN_BRAND_STRINGS) {
      if (body.includes(s.toLowerCase())) {
        throw new Error(`Whitelabel leak: "${s}" found on ${this.page.url()}`);
      }
    }
  }

  /** A visible toast/alert region, if the page renders one. */
  toast(): Locator {
    return this.page.getByRole("alert");
  }
}
