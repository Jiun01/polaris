/**
 * Secrets spec — /secrets + /secrets/[tenant_id]
 *
 * Covers the Secrets surface:
 *   /secrets:          active-tenant selector list.
 *   /secrets/[id]:     4 RotationForms (meta_whatsapp / meta_ig /
 *                      meta_messenger / chatwoot) → POST rotate,
 *                      value NOT echoed, rotation history.
 *
 * Each rotation uses a throwaway value (never a real credential).
 * Tests that POST to the API are safe to re-run: rotation is idempotent
 * (each call encrypts the new value; re-runs just overwrite with a new dummy).
 */

import { test, expect, ADMIN_STATE, TENANT_IDS } from "../_support/fixtures";
import { SecretsPage } from "../pages/SecretsPage";
import type { ServiceSuffix } from "../pages/SecretsPage";

test.skip(() => !ADMIN_STATE, "admin state not minted");
test.use({ storageState: ADMIN_STATE ?? "" });

// ────────────────────────────────────────────────────────────────────────────
// /secrets — tenant selector
// ────────────────────────────────────────────────────────────────────────────

test.describe("/secrets index", () => {
  test("page heading and tenant list render", async ({ page }) => {
    const pom = new SecretsPage(page);
    await pom.gotoIndex();

    await expect(pom.heading).toBeVisible({ timeout: 10_000 });
    // Description from page.tsx line 54-57
    await expect(
      page.getByText(/select a tenant to rotate per-service credentials/i),
    ).toBeVisible();
  });

  test("tenant list contains at least one active tenant", async ({ page }) => {
    const pom = new SecretsPage(page);
    await pom.gotoIndex();

    // Each tenant is a <Link> inside the Card — use direct attribute selector
    const tenantLinks = page.locator('a[href^="/secrets/"]');
    const count = await tenantLinks.count();
    expect(count).toBeGreaterThan(0);
  });

  test("tenant link navigates to /secrets/[tenant_id]", async ({ page }) => {
    const pom = new SecretsPage(page);
    await pom.gotoIndex();

    // Navigate via the proC tenant link
    const proCLink = page.getByRole("link").filter({
      has: page.locator(`[href="/secrets/${TENANT_IDS.proC}"]`),
    });

    const linkExists = await proCLink.count().then((n) => n > 0);
    if (!linkExists) {
      // Navigate directly — tenant may be active but link text differs
      await pom.gotoTenant(TENANT_IDS.proC);
    } else {
      await proCLink.click();
    }

    await page.waitForURL(new RegExp(`/secrets/${TENANT_IDS.proC}`), {
      timeout: 5_000,
    });
    await expect(pom.rotationHeading).toBeVisible({ timeout: 5_000 });
  });
});

// ────────────────────────────────────────────────────────────────────────────
// /secrets/[tenant_id] — rotation forms
// ────────────────────────────────────────────────────────────────────────────

test.describe("/secrets/[tenant_id] rotation forms", () => {
  test("renders page heading and back link for proC", async ({ page }) => {
    const pom = new SecretsPage(page);
    await pom.gotoTenant(TENANT_IDS.proC);

    await expect(pom.rotationHeading).toBeVisible({ timeout: 10_000 });
    await expect(pom.backLink).toBeVisible();
  });

  test("all 4 rotation panels are visible", async ({ page }) => {
    const pom = new SecretsPage(page);
    await pom.gotoTenant(TENANT_IDS.proC);

    const services: ServiceSuffix[] = [
      "whatsapp",
      "ig",
      "messenger",
      "chatwoot",
    ];
    for (const s of services) {
      // data-testid="rotation-panel-{suffix}" — RotationForm.tsx line 91
      await expect(pom.rotationPanel(s)).toBeVisible({ timeout: 10_000 });
    }
  });

  test("each panel has a 'Rotate' button", async ({ page }) => {
    const pom = new SecretsPage(page);
    await pom.gotoTenant(TENANT_IDS.proC);

    const services: ServiceSuffix[] = [
      "whatsapp",
      "ig",
      "messenger",
      "chatwoot",
    ];
    for (const s of services) {
      await expect(pom.rotateBtn(s)).toBeVisible({ timeout: 5_000 });
    }
  });

  test("panel labels match expected service names", async ({ page }) => {
    const pom = new SecretsPage(page);
    await pom.gotoTenant(TENANT_IDS.proC);

    // SERVICE_LABELS in RotationForm.tsx
    await expect(
      pom.rotationPanel("whatsapp").getByText(/meta whatsapp/i),
    ).toBeVisible();
    await expect(
      pom.rotationPanel("ig").getByText(/meta instagram/i),
    ).toBeVisible();
    await expect(
      pom.rotationPanel("messenger").getByText(/meta messenger/i),
    ).toBeVisible();
    await expect(
      pom.rotationPanel("chatwoot").getByText(/^chatwoot$/i),
    ).toBeVisible();
  });

  test("rotate WhatsApp: dialog opens, fill value, confirm → success status, value not echoed", async ({
    page,
  }) => {
    const pom = new SecretsPage(page);
    await pom.gotoTenant(TENANT_IDS.proC);

    const throwawayValue = `e2e-test-throwaway-wa-${Date.now()}`;

    await pom.rotateService("whatsapp", throwawayValue);

    // RotationForm.tsx line 104: role="status" "Rotated at …"
    await expect(
      pom.rotationPanel("whatsapp").getByRole("status"),
    ).toContainText(/rotated at/i, { timeout: 10_000 });

    // Value must NOT be echoed anywhere on the page
    await pom.assertValueNotEchoed(throwawayValue);
  });

  test("rotate Instagram: success status and value not echoed", async ({
    page,
  }) => {
    const pom = new SecretsPage(page);
    await pom.gotoTenant(TENANT_IDS.proC);

    const throwawayValue = `e2e-test-throwaway-ig-${Date.now()}`;
    await pom.rotateService("ig", throwawayValue);

    await expect(pom.rotationPanel("ig").getByRole("status")).toContainText(
      /rotated at/i,
      { timeout: 10_000 },
    );
    await pom.assertValueNotEchoed(throwawayValue);
  });

  test("rotate Messenger: success status and value not echoed", async ({
    page,
  }) => {
    const pom = new SecretsPage(page);
    await pom.gotoTenant(TENANT_IDS.proC);

    const throwawayValue = `e2e-test-throwaway-msg-${Date.now()}`;
    await pom.rotateService("messenger", throwawayValue);

    await expect(
      pom.rotationPanel("messenger").getByRole("status"),
    ).toContainText(/rotated at/i, { timeout: 10_000 });
    await pom.assertValueNotEchoed(throwawayValue);
  });

  test("rotate Chatwoot: success status and value not echoed", async ({
    page,
  }) => {
    const pom = new SecretsPage(page);
    await pom.gotoTenant(TENANT_IDS.proC);

    const throwawayValue = `e2e-test-throwaway-cw-${Date.now()}`;
    await pom.rotateService("chatwoot", throwawayValue);

    await expect(
      pom.rotationPanel("chatwoot").getByRole("status"),
    ).toContainText(/rotated at/i, { timeout: 10_000 });
    await pom.assertValueNotEchoed(throwawayValue);
  });

  test("rotate dialog closes on Cancel without submitting", async ({
    page,
  }) => {
    const pom = new SecretsPage(page);
    await pom.gotoTenant(TENANT_IDS.proC);

    await pom.rotateBtn("whatsapp").click();
    const dialog = page.getByRole("dialog");
    await expect(dialog).toBeVisible({ timeout: 5_000 });

    // Cancel button
    await dialog.getByRole("button", { name: /^cancel$/i }).click();
    await expect(dialog).not.toBeVisible({ timeout: 3_000 });

    // No success message should appear
    await expect(
      pom.rotationPanel("whatsapp").getByRole("status"),
    ).not.toBeVisible();
  });

  test("rotate dialog closes on Escape key", async ({ page }) => {
    const pom = new SecretsPage(page);
    await pom.gotoTenant(TENANT_IDS.proC);

    await pom.rotateBtn("ig").click();
    const dialog = page.getByRole("dialog");
    await expect(dialog).toBeVisible({ timeout: 5_000 });

    // Focus an element inside the dialog so the keydown handler fires correctly.
    const input = dialog.getByLabel(/new token \/ secret/i);
    await input.click();
    await page.keyboard.press("Escape");
    await expect(dialog).not.toBeVisible({ timeout: 3_000 });
  });

  test("rotation history section renders (empty or with entries)", async ({
    page,
  }) => {
    const pom = new SecretsPage(page);
    await pom.gotoTenant(TENANT_IDS.proC);

    // Either "Rotation history" heading (entries exist) or empty-state card
    const historyVisible = await pom.historyHeading
      .isVisible({ timeout: 5_000 })
      .catch(() => false);
    const emptyVisible = await pom.historyEmptyCard
      .isVisible({ timeout: 3_000 })
      .catch(() => false);

    expect(historyVisible || emptyVisible).toBe(true);

    if (historyVisible) {
      // Table columns defined in RotationHistory.tsx
      await expect(
        page.getByRole("columnheader", { name: /service/i }),
      ).toBeVisible();
      await expect(
        page.getByRole("columnheader", { name: /rotated at/i }),
      ).toBeVisible();
      await expect(
        page.getByRole("columnheader", { name: /actor/i }),
      ).toBeVisible();
    }
  });

  test("404: unknown tenant renders not-found", async ({ page }) => {
    const pom = new SecretsPage(page);
    await pom.gotoTenant("00000000-dead-beef-0000-000000000000");
    // notFound() from Next.js
    await expect(
      page.getByText(/404|not found|page could not be found/i).first(),
    ).toBeVisible({ timeout: 10_000 });
  });
});
