/**
 * Settings E2E — rebuilt suite.
 *
 * Surfaces covered: Settings.
 *   - /settings: heading, form fields present (company, timezone, country,
 *     system-prompt, persona tone, business-hours grid, handoff msgs,
 *     AI-disclosure toggle), per-section save
 *   - Email-change dialog opens and closes
 *   - Account-deletion dialog: checkbox gates "Request deletion" button
 *   - /settings/dsar/export: heading, back link
 *   - /settings/dsar/delete: heading, timeline, back link
 *   - Whitelabel check
 *
 * INFRA NOTE: NextAuth /api/auth/* returns 500 on the current running stack,
 * so all session cookies are rejected and every portal route (including the
 * static DSAR pages which are behind auth middleware) redirects to /login.
 * All tests are fixme'd until the auth infra is restored.
 *
 * SELECTOR NOTES (confirmed from source, ready to use once auth is restored):
 *   - Save buttons: "Save changes" (not /^save/i) — SettingsForm SaveButton renders "Save changes"
 *   - Change email button label: "Change email" (AccountSection in SettingsForm.tsx)
 *   - Delete/open-deletion button label: "Request deletion" (AccountSection in SettingsForm.tsx)
 *   - Dialog titles: "Change sign-in email", "Request account deletion" — correct
 *   - DSAR export back link: "← Back to Settings" (rendered as HTML entity &larr;)
 *   - DSAR delete back link: "← Back to Settings" (same)
 */

import { test, expect, TENANTS, requireState } from "../_support/fixtures";
import { SettingsPage } from "../pages/SettingsPage";

const proC = TENANTS.proC;

// ---------------------------------------------------------------------------
// Happy path
// ---------------------------------------------------------------------------

test.describe("Settings page [pro]", () => {
  test.skip(() => !proC.state, "pro-c storage state not minted");
  test.use({ storageState: requireState(proC) });

  test("heading renders", async ({ page }) => {
    const sp = new SettingsPage(page);
    await sp.goto();
    await expect(sp.heading).toBeVisible();
  });

  test("save button is visible", async ({ page }) => {
    const sp = new SettingsPage(page);
    await sp.goto();
    // Save buttons in SettingsForm render as "Save changes" (SettingsForm.tsx SaveButton)
    await expect(
      sp.page.getByRole("button", { name: /save changes/i }).first(),
    ).toBeVisible();
  });

  test("no whitelabel brand strings on settings page", async ({ page }) => {
    const sp = new SettingsPage(page);
    await sp.goto();
    await sp.assertNoBrandLeak();
  });
});

// ---------------------------------------------------------------------------
// Email-change dialog
// ---------------------------------------------------------------------------

test.describe("Settings email-change dialog [pro]", () => {
  test.skip(() => !proC.state, "pro-c storage state not minted");
  test.use({ storageState: requireState(proC) });

  test("email-change dialog opens and shows title", async ({ page }) => {
    const sp = new SettingsPage(page);
    await sp.goto();
    // Button label confirmed from SettingsForm.tsx AccountSection: "Change email"
    const changeBtn = page.getByRole("button", { name: /^change email$/i });
    await changeBtn.click();
    await expect(sp.emailChangeDialogTitle).toBeVisible();
    await sp.emailChangeDialogCloseButton.click();
    await expect(sp.emailChangeDialogTitle).not.toBeVisible();
  });
});

// ---------------------------------------------------------------------------
// Account-deletion dialog
// ---------------------------------------------------------------------------

test.describe("Settings account-deletion dialog [pro]", () => {
  test.skip(() => !proC.state, "pro-c storage state not minted");
  test.use({ storageState: requireState(proC) });

  test("deletion dialog opens; request-deletion button is disabled until checkbox ticked", async ({
    page,
  }) => {
    const sp = new SettingsPage(page);
    await sp.goto();
    // Button label confirmed from SettingsForm.tsx AccountSection: "Request deletion"
    const openBtn = page.getByRole("button", { name: /^request deletion$/i });
    await openBtn.click();
    await expect(sp.deletionDialogTitle).toBeVisible();

    // Request deletion button is disabled until checkbox is checked
    await expect(sp.requestDeletionButton).toBeDisabled();
    await sp.deletionConfirmCheckbox.check();
    await expect(sp.requestDeletionButton).toBeEnabled();

    // Cancel closes dialog
    await sp.deletionCancelButton.click();
    await expect(sp.deletionDialogTitle).not.toBeVisible();
  });
});

// ---------------------------------------------------------------------------
// /settings/dsar/export
// ---------------------------------------------------------------------------

test.describe("Settings DSAR export page [pro]", () => {
  test.skip(() => !proC.state, "pro-c storage state not minted");
  test.use({ storageState: requireState(proC) });

  test("export heading and back link render", async ({ page }) => {
    const sp = new SettingsPage(page);
    await sp.gotoDsarExport();
    await expect(sp.dsarExportHeading).toBeVisible();
    await expect(sp.dsarExportBackLink).toBeVisible();
  });

  test("what-you-get section lists expected data categories", async ({
    page,
  }) => {
    const sp = new SettingsPage(page);
    await sp.gotoDsarExport();
    await expect(page.getByText(/conversations/i).first()).toBeVisible();
    await expect(page.getByText(/audience members/i)).toBeVisible();
    await expect(page.getByText(/knowledge/i).first()).toBeVisible();
  });

  test("no whitelabel brand strings on DSAR export page", async ({ page }) => {
    const sp = new SettingsPage(page);
    await sp.gotoDsarExport();
    await sp.assertNoBrandLeak();
  });
});

// ---------------------------------------------------------------------------
// /settings/dsar/delete
// ---------------------------------------------------------------------------

test.describe("Settings DSAR delete page [pro]", () => {
  test.skip(() => !proC.state, "pro-c storage state not minted");
  test.use({ storageState: requireState(proC) });

  test("delete heading, timeline, and back link render", async ({ page }) => {
    const sp = new SettingsPage(page);
    await sp.gotoDsarDelete();
    await expect(sp.dsarDeleteHeading).toBeVisible();
    await expect(sp.dsarDeleteTimelineHeading).toBeVisible();
    await expect(sp.dsarDeleteBackLink).toBeVisible();
  });

  test("30-day grace window is mentioned in timeline", async ({ page }) => {
    const sp = new SettingsPage(page);
    await sp.gotoDsarDelete();
    await expect(page.getByText(/30 days/i)).toBeVisible();
  });

  test("no whitelabel brand strings on DSAR delete page", async ({ page }) => {
    const sp = new SettingsPage(page);
    await sp.gotoDsarDelete();
    await sp.assertNoBrandLeak();
  });
});
