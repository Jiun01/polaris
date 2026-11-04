/**
 * BookingsPage POM — /bookings (rebuilt suite).
 *
 * Selectors grounded in:
 *   - apps/portal/app/(portal)/bookings/page.tsx
 *   - apps/portal/components/bookings/BookingConfigForm.tsx
 */

import type { Page } from "@playwright/test";
import { BasePage } from "../_support/pom";

export class BookingsPage extends BasePage {
  constructor(page: Page) {
    super(page);
  }

  async goto() {
    await super.goto("/bookings");
  }

  get heading() {
    return this.page.getByRole("heading", { name: /^bookings$/i });
  }

  /** Fetch-error banner — role="status" */
  get fetchErrorBanner() {
    return this.page.getByRole("status");
  }

  // ── Read-only "Calendar settings" EA-mirror panel ─────────────────────────────

  /** Panel heading — the read-only EA mirror. */
  get calendarSettingsHeading() {
    return this.page.getByRole("heading", { name: /^calendar settings$/i });
  }

  /** "Update bot knowledge" — POST /config/sync. */
  get updateBotKnowledgeButton() {
    return this.page.getByRole("button", { name: /update bot knowledge/i });
  }

  // ── Editable "Bot messaging" fields (id-anchored) ─────────────────────────────

  get confirmationMessageTextarea() {
    return this.page.locator("#confirm-msg");
  }

  get selfServeCancelCheckbox() {
    return this.page.locator("#self-serve");
  }

  get saveButton() {
    return this.page.getByRole("button", { name: /^save$/i });
  }

  /** Inline save-error alert — role="alert" */
  get saveErrorAlert() {
    return this.page.getByRole("alert");
  }

  // ── Calendar admin link ───────────────────────────────────────────────────────

  get calendarAdminLink() {
    return this.page.getByRole("link", { name: /open calendar admin/i });
  }
}
