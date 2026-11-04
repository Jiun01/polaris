/**
 * SettingsPage POM — /settings (rebuilt suite).
 *
 * Selectors grounded in:
 *   - apps/portal/app/(portal)/settings/page.tsx
 *   - apps/portal/components/settings/SettingsClient.tsx
 *   - apps/portal/components/settings/SettingsForm.tsx
 *   - apps/portal/app/(portal)/settings/dsar/export/page.tsx
 *   - apps/portal/app/(portal)/settings/dsar/delete/page.tsx
 */

import type { Page } from "@playwright/test";
import { BasePage } from "../_support/pom";

export class SettingsPage extends BasePage {
  constructor(page: Page) {
    super(page);
  }

  async goto() {
    await super.goto("/settings");
  }

  async gotoDsarExport() {
    await super.goto("/settings/dsar/export");
  }

  async gotoDsarDelete() {
    await super.goto("/settings/dsar/delete");
  }

  get heading() {
    return this.page.getByRole("heading", { name: /^settings$/i });
  }

  // ── SettingsForm sections ─────────────────────────────────────────────────────
  // SettingsForm uses RHF + shadcn Form; each save button is inside its section.
  // Selectors here use text-based role queries; confirm against SettingsForm.tsx.

  /** Any "Save changes" button inside the settings form sections.
   * Confirmed from SettingsForm.tsx SaveButton: renders as "Save changes". */
  saveSectionButton(_sectionLabel?: RegExp | string) {
    return this.page.getByRole("button", { name: /save changes/i }).first();
  }

  /** AI-disclosure toggle — shadcn Switch; confirm data-testid with SettingsForm.tsx */
  get aiDisclosureSwitch() {
    // TODO(selector): confirm aria-label for AI disclosure Switch against SettingsForm.tsx
    return this.page.getByRole("switch", { name: /ai disclosure/i });
  }

  // ── Email-change dialog ───────────────────────────────────────────────────────

  /** Button that opens the email-change dialog.
   * Confirmed from SettingsForm.tsx AccountSection: label is "Change email". */
  get changeEmailButton() {
    return this.page.getByRole("button", { name: /^change email$/i });
  }

  get emailChangeDialogTitle() {
    return this.page.getByRole("heading", { name: /change sign-in email/i });
  }

  /** Close button in the email-change dialog.
   * Confirmed from SettingsClient.tsx DialogFooter: label is "Close". */
  get emailChangeDialogCloseButton() {
    // The dialog has two "Close" elements: the footer button (text "Close") and
    // the shadcn X icon button (aria-label "Close"). Scope to the footer button
    // using exact text to avoid strict-mode violation.
    return this.page
      .getByRole("dialog")
      .getByRole("button", { name: "Close", exact: true })
      .filter({ hasText: "Close" });
  }

  // ── Account-deletion dialog ───────────────────────────────────────────────────

  /** Button that opens the account-deletion dialog.
   * Confirmed from SettingsForm.tsx AccountSection: label is "Request deletion". */
  get deleteAccountButton() {
    return this.page.getByRole("button", { name: /^request deletion$/i });
  }

  get deletionDialogTitle() {
    return this.page.getByRole("heading", {
      name: /request account deletion/i,
    });
  }

  get deletionConfirmCheckbox() {
    return this.page.locator("#acct-deletion-confirm");
  }

  /** "Request deletion" submit button inside the deletion dialog.
   * Confirmed from SettingsClient.tsx: label is "Request deletion" (or "Submitting…" while pending). */
  get requestDeletionButton() {
    return this.page
      .getByRole("dialog")
      .getByRole("button", { name: /^request deletion$/i });
  }

  /** "Cancel" button inside the deletion dialog.
   * Confirmed from SettingsClient.tsx DialogFooter. */
  get deletionCancelButton() {
    return this.page
      .getByRole("dialog")
      .getByRole("button", { name: /^cancel$/i });
  }

  // ── DSAR export page ──────────────────────────────────────────────────────────

  get dsarExportHeading() {
    return this.page.getByRole("heading", { name: /export your data/i });
  }

  get dsarExportBackLink() {
    return this.page.getByRole("link", { name: /back to settings/i });
  }

  // ── DSAR delete page ──────────────────────────────────────────────────────────

  get dsarDeleteHeading() {
    return this.page.getByRole("heading", { name: /delete your account/i });
  }

  get dsarDeleteBackLink() {
    // Two links on this page match /back to settings/i:
    //   1) "← Back to Settings" (header ghost-button link)
    //   2) "Back to Settings > Account" (bottom outline-button link)
    // Use exact text for the header link to avoid strict-mode.
    return this.page.getByRole("link", {
      name: "← Back to Settings",
      exact: true,
    });
  }

  get dsarDeleteTimelineHeading() {
    return this.page.getByRole("heading", { name: /timeline/i });
  }
}
