/**
 * E2E server-side fault injection helper (admin).
 *
 * Admin Server Components fetch on the server, so Playwright page.route() can't
 * intercept them. adminServerFetch honors an `e2e_fault` cookie (gated by
 * POLARIS_E2E_INTERNAL_SECRET) to force a fault. Cookie domain is the admin
 * origin (admin.localhost via Caddy). Set BEFORE page.goto().
 *
 *   await setE2EFault(page, [{ match: "billing/invoices", status: 503 }]);
 *   await setE2EFault(page, [{ match: "quotes", body: { items: [] } }]);
 */

import type { Page } from "@playwright/test";

export interface E2EFault {
  match: string;
  status?: number;
  body?: unknown;
  message?: string;
}

function adminCookieDomain(): string {
  // Derive host from the admin base URL (admin.localhost via Caddy, or :3001).
  const base =
    process.env.PLAYWRIGHT_ADMIN_BASE_URL ?? "http://admin.localhost";
  try {
    return new URL(base).hostname;
  } catch {
    return "admin.localhost";
  }
}

export async function setE2EFault(
  page: Page,
  directives: E2EFault[],
): Promise<void> {
  await page.context().addCookies([
    {
      name: "e2e_fault",
      value: encodeURIComponent(JSON.stringify(directives)),
      domain: adminCookieDomain(),
      path: "/",
    },
  ]);
}

export async function clearE2EFault(page: Page): Promise<void> {
  await page.context().clearCookies({ name: "e2e_fault" });
}
