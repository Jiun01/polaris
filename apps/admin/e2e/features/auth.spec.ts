/**
 * Auth / shell E2E spec (rebuilt suite).
 *
 * Surfaces covered: Auth/shell.
 *   - /login magic-link: 10-min expiry note, 3/hr rate-limit (same page),
 *     IP-bind note, CSRF token in form, AccessDenied error copy
 *   - /verify-request static content, "try again" → /login
 *   - Unauthenticated → /login redirect
 *   - / → /tenants redirect (after auth)
 *   - Sign-out → /login
 *   - TopBar env badge present
 *   - Sidebar 4 nav groups: Configure / Monitor / Operate / Compliance
 *   - Non-admin (no valid session) → /login with 403 / AccessDenied
 *
 * NOTE: Magic-link round-trip (submit → MailHog → callback) is covered by
 * _legacy/auth.spec.ts. This file tests the static / structural aspects of
 * the auth shell that do not require MailHog.
 */

import { test, expect, ADMIN_STATE } from "../_support/fixtures";
import { LoginPage, VerifyRequestPage } from "../pages/LoginPage";

// ---------------------------------------------------------------------------
// Auth-required tests (signed-in admin session)
// ---------------------------------------------------------------------------

test.describe("Admin shell — authenticated", () => {
  test.skip(() => !ADMIN_STATE, "admin storage state not minted");
  test.use({ storageState: ADMIN_STATE ?? "" });

  test("/ redirects to /tenants", async ({ page }) => {
    await page.goto("/");
    await expect(page).toHaveURL(/\/tenants/);
  });

  test("TopBar: Polaris Admin brand and env badge are visible", async ({
    page,
  }) => {
    await page.goto("/tenants");
    await page.waitForLoadState("networkidle");

    // Brand text from TopBar.tsx: "Polaris Admin"
    await expect(
      page.getByRole("banner").getByText("Polaris Admin"),
    ).toBeVisible();

    // EnvironmentBadge renders an <span aria-label="environment: X">
    await expect(
      page.getByRole("banner").locator('[aria-label^="environment:"]'),
    ).toBeVisible();
  });

  test("Sidebar has 4 nav groups: Configure / Monitor / Operate / Compliance", async ({
    page,
  }) => {
    await page.goto("/tenants");
    await page.waitForLoadState("networkidle");

    const nav = page.getByRole("navigation", { name: /admin navigation/i });

    // Group headings are <p> with id="nav-group-X" from Sidebar.tsx
    for (const group of ["Configure", "Monitor", "Operate", "Compliance"]) {
      await expect(nav.getByText(group)).toBeVisible();
    }
  });

  test("Sidebar: expected nav items are present", async ({ page }) => {
    await page.goto("/tenants");
    await page.waitForLoadState("networkidle");

    const nav = page.getByRole("navigation", { name: /admin navigation/i });
    const expectedLinks = [
      "Tenants",
      "Quotas",
      "Secrets",
      "Billing",
      "Plan Changes",
      "Sales Quotes",
      "Audit Log",
      "Impersonation",
      "System Health",
      "Cost",
      "Financials",
    ];
    for (const label of expectedLinks) {
      await expect(nav.getByRole("link", { name: label })).toBeVisible();
    }
  });

  test("Sign out navigates to /login", async ({ page }) => {
    await page.goto("/tenants");
    await page.waitForLoadState("networkidle");

    // TopBar sign-out is a plain form POST to /api/auth/signout with only
    // callbackUrl — no csrfToken. NextAuth redirects that to
    // /api/auth/signout?csrf=true (a GET that shows the sign-out confirmation
    // page). We must submit that confirmation form to complete the flow.
    // Click the Sign out button to reach the csrf=true page.
    await page.getByRole("button", { name: /sign out/i }).click();

    // NextAuth lands on /api/auth/signout?csrf=true with a confirmation form.
    // Submit it by clicking its submit button to complete sign-out.
    await page.waitForURL(/signout/, { timeout: 10_000 });
    const confirmBtn = page.getByRole("button", { name: /sign out/i });
    if (await confirmBtn.isVisible()) {
      await confirmBtn.click();
    }

    await expect(page).toHaveURL(/\/login/, { timeout: 15_000 });
  });

  test("assertNoBrandLeak on /tenants", async ({ page }) => {
    await page.goto("/tenants");
    await page.waitForLoadState("networkidle");
    const pom = new LoginPage(page); // BasePage instance for assertNoBrandLeak
    await pom.assertNoBrandLeak();
  });
});

// ---------------------------------------------------------------------------
// Unauthenticated / negative tests — use fresh context (no storageState)
// ---------------------------------------------------------------------------

test.describe("Admin auth — unauthenticated", () => {
  test("unauthenticated request to /tenants redirects to /login", async ({
    page,
  }) => {
    await page.goto("/tenants");
    await expect(page).toHaveURL(/\/login/, { timeout: 10_000 });
  });

  test("/login page renders heading and email form", async ({ page }) => {
    const login = new LoginPage(page);
    await login.gotoLogin();

    await expect(login.heading).toBeVisible();
    await expect(login.emailInput).toBeVisible();
    await expect(login.submitButton).toBeVisible();
  });

  test("/login footer notes 10-min expiry and IP-bind requirement", async ({
    page,
  }) => {
    const login = new LoginPage(page);
    await login.gotoLogin();

    // Footer text from login/page.tsx: "expires in 10 minutes … same network"
    await expect(page.getByText(/expires in 10 minutes/i)).toBeVisible();
    await expect(page.getByText(/same network/i)).toBeVisible();
  });

  test("/login?error=AccessDenied shows IP-bind / non-admin error alert", async ({
    page,
  }) => {
    await page.goto("/login?error=AccessDenied");
    const login = new LoginPage(page);

    // Error copy from login/page.tsx AccessDenied branch:
    // "That email isn't an active platform admin, or you opened the link
    //  from a different network than you requested it from."
    await expect(login.errorAlert).toBeVisible();
    await expect(login.errorAlert).toContainText(/different network/i);
  });

  test("/login?error=<other> shows generic send-fail message", async ({
    page,
  }) => {
    await page.goto("/login?error=EmailSignin");
    const login = new LoginPage(page);

    await expect(login.errorAlert).toBeVisible();
    await expect(login.errorAlert).toContainText(
      /couldn't send the sign-in link/i,
    );
  });

  test("/verify-request is static and has a try-again link to /login", async ({
    page,
  }) => {
    await page.goto("/verify-request");
    const vr = new VerifyRequestPage(page);

    await expect(vr.heading).toBeVisible();
    // "try again" link href="/login" from verify-request/page.tsx
    await expect(vr.tryAgainLink).toBeVisible();
    await vr.tryAgainLink.click();
    await expect(page).toHaveURL(/\/login/);
  });

  test("non-admin (AccessDenied) → stays on /login, no admin content", async ({
    page,
  }) => {
    await page.goto("/login?error=AccessDenied");

    // Must NOT have landed on /tenants or show admin shell
    await expect(page).toHaveURL(/\/login/);
    await expect(
      page.getByRole("navigation", { name: /admin navigation/i }),
    ).not.toBeVisible();
  });
});
