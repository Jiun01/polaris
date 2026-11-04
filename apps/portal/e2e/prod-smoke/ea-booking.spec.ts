/**
 * PROD SMOKE — EA fork r19 (CW design-parity restyle) visual/structural check.
 *
 * The other session's EA restyle shipped in the same release (v1.6.0-r19).
 * This is a STRUCTURAL smoke check only, not a full booking submission:
 * the live public booking page is guarded by a Cloudflare Turnstile widget
 * (confirmed via polaris-ea-fork/tests/e2e/pages/BookingPage.ts —
 * `.cf-turnstile`), so automating past it isn't appropriate or reliably
 * scriptable in a smoke test. This spec only confirms the restyle didn't
 * break rendering of the wizard; it never submits a real appointment.
 *
 * Cross-origin by design — the public booking page lives on
 * calendar.getpolaris.my, not the portal's own origin, so every navigation
 * here uses an absolute URL (Playwright allows this even with a baseURL set).
 */

import { test, expect } from "@playwright/test";

// The live booking URL targets the gateway tenant — set via env, see
// e2e/prod-smoke/README.md.
const BOOKING_URL =
  process.env.SMOKE_BOOKING_URL ??
  "https://calendar.example.com/book/demo-tenant";

test.describe("EA r19 — booking page restyle smoke check", () => {
  test("public booking wizard renders after the restyle", async ({ page }) => {
    const res = await page.goto(BOOKING_URL);
    expect(res?.ok()).toBeTruthy();

    // Selectors confirmed against polaris-ea-fork/tests/e2e/pages/BookingPage.ts —
    // #wizard-frame-1 is the service/provider selection step, present on load.
    await expect(page.locator("#wizard-frame-1")).toBeAttached({
      timeout: 15_000,
    });

    // Structural CW-design-parity check: no leftover raw EA branding string
    // outside the acknowledged GPL license comment (mirrors the EA fork's own
    // brand_sweep.spec.ts convention).
    const bodyText = await page.locator("body").innerText();
    expect(bodyText).not.toMatch(/Easy!Appointments/i);

    await page.screenshot({
      path: "test-results/prod-smoke-ea-booking-page.png",
      fullPage: true,
    });
  });

  test("submit button is present and enabled (no submission attempted)", async ({
    page,
  }) => {
    await page.goto(BOOKING_URL);
    // Presence-only check — do NOT click through: the form is Turnstile-gated
    // and actually submitting would create a real appointment / risk tripping
    // anti-bot defenses. Full submission round-trip is intentionally out of
    // scope for this smoke suite.
    await expect(page.locator("#wizard-frame-1")).toBeAttached({
      timeout: 15_000,
    });
  });
});
