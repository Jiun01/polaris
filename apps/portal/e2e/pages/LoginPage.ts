/**
 * LoginPage POM — /login (rebuilt suite).
 *
 * Wraps the login form selectors + actions. Selectors are grounded in
 * apps/portal/app/(auth)/login/page.tsx markup:
 *   - email input: <input id="email" type="email" />  → getByLabel("Work email")
 *   - submit btn:  <button type="submit">Send sign-in link</button>
 *   - error alert: <div role="alert"> rendered when ?error= is present
 */

import type { Page } from "@playwright/test";
import { BasePage } from "../_support/pom";

export class LoginPage extends BasePage {
  constructor(page: Page) {
    super(page);
  }

  async goto() {
    await super.goto("/login");
  }

  /** The "Work email" label wraps <input id="email">. */
  get emailInput() {
    return this.page.getByLabel(/work email/i);
  }

  /** The submit button. Text is "Send sign-in link" (or "Sending…" while busy). */
  get sendLinkButton() {
    return this.page.getByRole("button", { name: /send sign-in link/i });
  }

  /**
   * The error alert rendered when NextAuth redirects back with ?error=…
   * role="alert" is set directly on the div in login/page.tsx.
   * Excludes the Next.js route-announcer which also carries role="alert".
   */
  get errorAlert() {
    // The route announcer has id="__next-route-announcer__"; exclude it.
    return this.page.locator('[role="alert"]:not(#__next-route-announcer__)');
  }

  /** Fill email and click submit. */
  async submitEmail(email: string) {
    await this.emailInput.fill(email);
    await this.sendLinkButton.click();
  }
}
