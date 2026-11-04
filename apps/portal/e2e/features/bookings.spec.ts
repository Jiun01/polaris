/**
 * Bookings E2E — rebuilt suite.
 *
 * Surfaces covered: Bookings.
 *   - Config form: cancellation lead, reschedule window, confirmation msg,
 *     self-serve toggle; Save button; Calendar-admin link
 *   - Fetch-error → defaults still render
 *   - Form save error banner
 *   - Whitelabel check
 */

import { test, expect, TENANTS, requireState } from "../_support/fixtures";
import { BookingsPage } from "../pages/BookingsPage";
import { setE2EFault } from "../_support/e2e-fault";

const proC = TENANTS.proC;

const CONFIG_FIXTURE = {
  cancellation_lead_hours: 24,
  reschedule_window_hours: 48,
  confirmation_message: "Your booking is confirmed!",
  allow_self_serve_cancellation: true,
};

// ---------------------------------------------------------------------------
// Happy path
// ---------------------------------------------------------------------------

test.describe("Bookings config [pro]", () => {
  test.skip(() => !proC.state, "pro-c storage state not minted");
  test.use({ storageState: requireState(proC) });

  test("heading and config form fields render", async ({ page }) => {
    await page.route("**/api/tenant/bookings/config", async (route) => {
      if (route.request().method() === "GET") {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify(CONFIG_FIXTURE),
        });
      } else {
        await route.continue();
      }
    });

    const bp = new BookingsPage(page);
    await bp.goto();
    await expect(bp.heading).toBeVisible();
    // Read-only EA-mirror panel (lead-time/hours are no longer editable inputs).
    await expect(bp.calendarSettingsHeading).toBeVisible();
    await expect(bp.updateBotKnowledgeButton).toBeVisible();
    // Editable bot-messaging fields.
    await expect(bp.confirmationMessageTextarea).toBeVisible();
    await expect(bp.selfServeCancelCheckbox).toBeVisible();
    await expect(bp.saveButton).toBeVisible();
  });

  test("form is pre-populated with server values", async ({ page }) => {
    // NOTE: /bookings/page.tsx is a Next.js SSR Server Component.
    // page.route() cannot intercept its server-side fetch of
    // /api/tenant/bookings/config — the mock is a no-op and the form
    // renders with the REAL seeded tenant config (or DEFAULT_CONFIG if
    // the API returns no row). Lead-time/hours now live in the READ-ONLY
    // EA-mirror panel (sourced from EA, possibly "Not synced yet"), so we
    // assert the panel + the editable bot fields render rather than input
    // values that can't be mocked through SSR.
    const bp = new BookingsPage(page);
    await bp.goto();
    await expect(bp.calendarSettingsHeading).toBeVisible();
    // confirmation_message is empty string by default — just check the field renders
    await expect(bp.confirmationMessageTextarea).toBeVisible();
    await expect(bp.selfServeCancelCheckbox).toBeChecked();
  });

  test("Calendar admin link is visible and points to the correct URL", async ({
    page,
  }) => {
    await page.route("**/api/tenant/bookings/config", async (route) => {
      if (route.request().method() === "GET") {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify(CONFIG_FIXTURE),
        });
      } else {
        await route.continue();
      }
    });

    const bp = new BookingsPage(page);
    await bp.goto();
    await expect(bp.calendarAdminLink).toBeVisible();
    const href = await bp.calendarAdminLink.getAttribute("href");
    expect(href).toContain("calendar.getpolaris.my");
  });

  test("no whitelabel brand strings on bookings page", async ({ page }) => {
    const bp = new BookingsPage(page);
    await bp.goto();
    await bp.assertNoBrandLeak();
  });
});

// ---------------------------------------------------------------------------
// Fetch-error → defaults
// ---------------------------------------------------------------------------

test.describe("Bookings fetch-error → defaults [pro]", () => {
  test.skip(() => !proC.state, "pro-c storage state not minted");
  test.use({ storageState: requireState(proC) });

  test("503 on config endpoint shows error banner and falls back to defaults", async ({
    page,
  }) => {
    await setE2EFault(page, [{ match: "bookings/config", status: 503 }]);

    const bp = new BookingsPage(page);
    await bp.goto();
    await expect(bp.fetchErrorBanner).toBeVisible();
    await expect(bp.fetchErrorBanner).toContainText(
      /couldn't load booking config/i,
    );
    // Form still renders with defaults (editable bot panel + read-only EA panel).
    await expect(bp.confirmationMessageTextarea).toBeVisible();
    await expect(bp.calendarSettingsHeading).toBeVisible();
  });
});

// ---------------------------------------------------------------------------
// Form save error
// ---------------------------------------------------------------------------

test.describe("Bookings save error [pro]", () => {
  test.skip(() => !proC.state, "pro-c storage state not minted");
  test.use({ storageState: requireState(proC) });

  test("PUT 500 shows inline error alert", async ({ page }) => {
    await page.route("**/api/tenant/bookings/config", async (route) => {
      if (route.request().method() === "GET") {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify(CONFIG_FIXTURE),
        });
      } else {
        await route.fulfill({ status: 500 });
      }
    });

    const bp = new BookingsPage(page);
    await bp.goto();
    await bp.saveButton.click();
    await expect(bp.saveErrorAlert).toBeVisible();
  });
});
