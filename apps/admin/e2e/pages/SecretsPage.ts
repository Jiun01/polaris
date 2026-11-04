/**
 * Page Object: /secrets and /secrets/[tenant_id]
 *
 * Source: apps/admin/app/(admin)/secrets/page.tsx
 *         apps/admin/app/(admin)/secrets/[tenant_id]/page.tsx
 *         apps/admin/components/secrets/RotationForm.tsx
 *         apps/admin/components/secrets/RotationHistory.tsx
 */

import type { Locator, Page } from "@playwright/test";
import { BasePage } from "../_support/pom";

/** Service suffixes used in data-testid="rotation-panel-{suffix}". */
export type ServiceSuffix = "whatsapp" | "ig" | "messenger" | "chatwoot";

export class SecretsPage extends BasePage {
  // ── /secrets (index — tenant selector) ──────────────────────────────────

  /** h1 "Secrets rotation" */
  readonly heading: Locator;

  /** Card containing the tenant link list */
  readonly tenantListCard: Locator;

  /** "No active tenants found" empty-state card */
  readonly emptyCard: Locator;

  // ── /secrets/[tenant_id] (rotation forms + history) ─────────────────────

  /** h1 "Secrets: {tenant_name}" */
  readonly rotationHeading: Locator;

  /** "Back to tenant detail" link */
  readonly backLink: Locator;

  /** "Rotation history" section heading */
  readonly historyHeading: Locator;

  /** "No rotation history yet" empty-state card */
  readonly historyEmptyCard: Locator;

  /** All rotation history table rows */
  readonly historyRows: Locator;

  constructor(page: Page) {
    super(page);

    // index
    this.heading = page.getByRole("heading", { name: /secrets rotation/i });
    this.tenantListCard = page.locator(".divide-y.divide-border").first();
    this.emptyCard = page.getByText(/no active tenants found/i);

    // per-tenant
    this.rotationHeading = page.getByRole("heading", { name: /^secrets:/i });
    this.backLink = page.getByRole("link", { name: /back to tenant detail/i });
    this.historyHeading = page.getByText(/rotation history/i).first();
    this.historyEmptyCard = page.getByText(
      /no rotation history yet for this tenant/i,
    );
    this.historyRows = page
      .getByRole("table")
      .getByRole("row")
      .filter({
        hasNot: page.getByRole("columnheader").locator(".."),
      });
  }

  async gotoIndex(): Promise<void> {
    await super.goto("/secrets");
  }

  async gotoTenant(tenantId: string): Promise<void> {
    await super.goto(`/secrets/${tenantId}`);
  }

  /** Navigate to a tenant's rotation page via the tenant list. */
  async clickTenantByName(name: string): Promise<void> {
    await this.page.getByRole("link", { name }).click();
  }

  // ── Per-service panel helpers ────────────────────────────────────────────

  /** Returns the rotation panel Card for a given service. */
  rotationPanel(service: ServiceSuffix): Locator {
    return this.page.getByTestId(`rotation-panel-${service}`);
  }

  /** Returns the "Rotate" button inside a service's panel. */
  rotateBtn(service: ServiceSuffix): Locator {
    return this.rotationPanel(service).getByRole("button", {
      name: /^rotate$/i,
    });
  }

  /**
   * Opens the rotate dialog for a service, fills the new value, and confirms.
   * Waits for the role="status" success message inside the panel.
   *
   * NOTE: The submitted value is NOT echoed back — the test only asserts the
   * "Rotated at …" status message appears (value not echoed requirement).
   */
  async rotateService(service: ServiceSuffix, newValue: string): Promise<void> {
    await this.rotateBtn(service).click();

    // Dialog: role="dialog" with aria-labelledby="rotate-heading-{fullServiceId}"
    const dialog = this.page.getByRole("dialog");
    await dialog.waitFor({ timeout: 5_000 });

    // Label htmlFor="secret-{service_kind}" — use getByLabel on the input
    // TODO(selector): label text is "New token / secret"; confirm against RotationForm.tsx line 130
    await dialog.getByLabel(/new token \/ secret/i).fill(newValue);
    await dialog.getByRole("button", { name: /^confirm$/i }).click();

    // Success: role="status" with "Rotated at …" inside the panel
    await this.rotationPanel(service)
      .getByRole("status")
      .filter({ hasText: /rotated at/i })
      .waitFor({ timeout: 10_000 });
  }

  /** Asserts the rotated value is not visible anywhere on the page. */
  async assertValueNotEchoed(value: string): Promise<void> {
    const body = await this.page.locator("body").textContent();
    if (body?.includes(value)) {
      throw new Error(
        `Secret value "${value.slice(0, 8)}…" was echoed back on the page`,
      );
    }
  }
}
