/**
 * E2E server-side fault injection helper.
 *
 * Next.js app-router pages fetch on the SERVER, so Playwright's page.route()
 * can't intercept them. The portal's apiFetchServer honors an `e2e_fault`
 * cookie (gated by POLARIS_E2E_INTERNAL_SECRET) to force a fault. Use this to
 * test 503 error banners, empty states, and populated states on SSR pages.
 *
 * Each directive matches an API path by substring and either forces an HTTP
 * error (`status` >= 400) or returns a canned `body` (empty/populated).
 *
 *   await setE2EFault(page, [{ match: "dashboard/summary", status: 503 }]);
 *   await setE2EFault(page, [{ match: "audience", body: { items: [] } }]);
 *
 * Set the cookie BEFORE page.goto(). Cookies persist on the context, so clear
 * between tests (or rely on per-test fresh contexts).
 *
 * NOTE: avoid a literal "%" in `body`/`message` strings — Next.js pre-decodes
 * the cookie value once, so a bare "%" survives into decodeURIComponent and
 * throws (the directive is then ignored). Use words ("percent") instead.
 */

import type { Page } from "@playwright/test";

export interface E2EFault {
  /** Substring matched against the API path passed to apiFetchServer. */
  match: string;
  /** >= 400 forces the page's error path (error banner). */
  status?: number;
  /** Canned response body returned in place of the real fetch. */
  body?: unknown;
  message?: string;
}

export async function setE2EFault(
  page: Page,
  directives: E2EFault[],
): Promise<void> {
  await page.context().addCookies([
    {
      name: "e2e_fault",
      value: encodeURIComponent(JSON.stringify(directives)),
      domain: "localhost",
      path: "/",
    },
  ]);
}

export async function clearE2EFault(page: Page): Promise<void> {
  await page.context().clearCookies({ name: "e2e_fault" });
}
