/**
 * LoginPage POM — /login (admin magic-link sign-in).
 *
 * Selectors confirmed against:
 *   apps/admin/app/(auth)/login/page.tsx
 *   apps/admin/app/(auth)/verify-request/page.tsx
 */

import type { Locator, Page } from "@playwright/test";

import { BasePage } from "../_support/pom";

export class LoginPage extends BasePage {
  readonly emailInput: Locator;
  readonly submitButton: Locator;
  readonly errorAlert: Locator;
  readonly heading: Locator;

  constructor(page: Page) {
    super(page);
    // label text from login/page.tsx: <label htmlFor="email">Admin email</label>
    this.emailInput = page.getByLabel(/admin email/i);
    // button text from login/page.tsx: "Send sign-in link"
    this.submitButton = page.getByRole("button", {
      name: /send sign-in link/i,
    });
    // role="alert" div rendered when ?error= param is set.
    // Exclude the Next.js route announcer (#__next-route-announcer__)
    // which also carries role="alert" aria-live="assertive".
    this.errorAlert = page
      .getByRole("alert")
      .filter({ hasNot: page.locator("#__next-route-announcer__") })
      .first();
    // h1 from login/page.tsx: "Sign in to the admin console"
    this.heading = page.getByRole("heading", {
      name: /sign in to the admin console/i,
    });
  }

  async gotoLogin(): Promise<void> {
    // Prime CSRF cookie then navigate — mirrors legacy auth.spec.ts pattern
    await this.page.goto("/api/auth/csrf");
    await this.page.goto("/login");
  }

  async fillEmail(email: string): Promise<void> {
    await this.emailInput.fill(email);
  }

  async submit(): Promise<void> {
    await this.submitButton.click();
  }

  async signIn(email: string): Promise<void> {
    await this.gotoLogin();
    await this.fillEmail(email);
    await this.submit();
  }
}

export class VerifyRequestPage extends BasePage {
  readonly heading: Locator;
  readonly tryAgainLink: Locator;

  constructor(page: Page) {
    super(page);
    // h1 from verify-request/page.tsx: "Check your inbox"
    this.heading = page.getByRole("heading", { name: /check your inbox/i });
    // <a href="/login">try again</a>
    this.tryAgainLink = page.getByRole("link", { name: /try again/i });
  }
}
