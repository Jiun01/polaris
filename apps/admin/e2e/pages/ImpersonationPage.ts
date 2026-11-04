/**
 * Page Object: /impersonation
 *
 * Source: apps/admin/app/(admin)/impersonation/page.tsx
 *         apps/admin/components/impersonation/PreImpersonationModal.tsx
 *         apps/admin/components/shell/ImpersonationBanner.tsx  (admin shell)
 */

import type { Locator, Page } from "@playwright/test";
import { BasePage } from "../_support/pom";

export class ImpersonationPage extends BasePage {
  /** h1 "Impersonate Tenant User" (when params present) */
  readonly heading: Locator;

  /** h1 "Impersonation" (usage-note state — no params) */
  readonly usageNoteHeading: Locator;

  /** "Navigate here from the Tenant detail page…" usage note text */
  readonly usageNoteText: Locator;

  /**
   * "Impersonate" trigger button (idle state of PreImpersonationModal).
   * Variant="outline", amber-coloured.
   */
  readonly impersonateBtn: Locator;

  /**
   * Confirmation dialog (role="dialog") — shown after clicking Impersonate.
   * aria-labelledby="impersonation-confirm-heading".
   */
  readonly confirmDialog: Locator;

  /** h2 "Start impersonation session?" inside the confirm dialog */
  readonly confirmHeading: Locator;

  /** "Start session" (destructive) button inside the confirm dialog */
  readonly startSessionBtn: Locator;

  /** "Cancel" (ghost) button inside the confirm dialog */
  readonly cancelBtn: Locator;

  /**
   * Post-start banner (data-testid="impersonation-banner").
   * Shows "Session started. Portal opened in new tab." + "Stop session" button.
   */
  readonly sessionBanner: Locator;

  /** "Stop session" button inside the post-start banner */
  readonly stopSessionBtn: Locator;

  /** Error alert (role="alert") when start fails */
  readonly errorAlert: Locator;

  /** Security notice box listing the four bullet points */
  readonly securityNotice: Locator;

  constructor(page: Page) {
    super(page);
    this.heading = page.getByRole("heading", {
      name: /impersonate tenant user/i,
    });
    this.usageNoteHeading = page.getByRole("heading", {
      name: /^impersonation$/i,
    });
    this.usageNoteText = page.getByText(
      /navigate here from the tenant detail page/i,
    );
    this.impersonateBtn = page.getByRole("button", { name: /^impersonate$/i });
    this.confirmDialog = page.getByRole("dialog");
    this.confirmHeading = page
      .getByRole("dialog")
      .getByRole("heading", { name: /start impersonation session/i });
    this.startSessionBtn = page.getByRole("button", {
      name: /start session/i,
    });
    this.cancelBtn = page.getByRole("button", { name: /^cancel$/i });
    this.sessionBanner = page.getByTestId("impersonation-banner");
    this.stopSessionBtn = page.getByRole("button", { name: /stop session/i });
    this.errorAlert = page.getByRole("alert").filter({
      hasText: /failed to start session/i,
    });
    this.securityNotice = page.getByText(/security notice/i).locator("..");
  }

  /**
   * Navigate to the impersonation page with all required params.
   * Missing tenantId or userId causes the usage-note state to render.
   */
  async gotoWithParams(params?: {
    tenantId?: string;
    userId?: string;
    slug?: string;
    email?: string;
  }): Promise<void> {
    if (!params || (!params.tenantId && !params.userId)) {
      await this.goto("/impersonation");
      return;
    }
    const qs = new URLSearchParams();
    if (params.tenantId) qs.set("tenantId", params.tenantId);
    if (params.userId) qs.set("userId", params.userId);
    if (params.slug) qs.set("slug", params.slug);
    if (params.email) qs.set("email", params.email);
    await this.goto(`/impersonation?${qs.toString()}`);
  }

  /** Open the confirm dialog and assert its heading is visible. */
  async openConfirmDialog(): Promise<void> {
    await this.impersonateBtn.click();
    await this.confirmHeading.waitFor({ timeout: 5_000 });
  }

  /** Confirm and start the session; waits for the success banner. */
  async startSession(): Promise<void> {
    await this.startSessionBtn.click();
    await this.sessionBanner.waitFor({ timeout: 10_000 });
  }

  /** Stop the session; waits for the banner to disappear. */
  async stopSession(): Promise<void> {
    await this.stopSessionBtn.click();
    await this.sessionBanner.waitFor({ state: "hidden", timeout: 5_000 });
  }
}
