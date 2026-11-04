/**
 * Impersonation spec — /impersonation
 *
 * Covers the Impersonation surface:
 *   - /impersonation (no params) → usage note
 *   - PreImpersonationModal: confirm warnings → start (60-min cookie, opens portal)
 *   - ImpersonationBanner (post-start) + stop
 *   - Security notice bullets visible before confirmation
 *
 * Uses TENANT_IDS.proC + a seeded user id.
 *
 * NOTE: The seeded portal-admin user id for proC is not directly available from
 * the fixtures file. Tests that require a real userId are marked test.fixme
 * until the seed script (seed_e2e_multitenant.py) exports the admin user id.
 * The modal open/close flow is tested with a placeholder id (which will fail
 * the startImpersonation POST but still exercises the UI state machine).
 */

import {
  test,
  expect,
  ADMIN_STATE,
  TENANT_IDS,
  TENANT_SLUGS,
} from "../_support/fixtures";
import { ImpersonationPage } from "../pages/ImpersonationPage";

test.skip(() => !ADMIN_STATE, "admin state not minted");
test.use({ storageState: ADMIN_STATE ?? "" });

// Internal-e2e: create a disposable tenant (with a real, returned admin user_id)
// so the start-session flow can run against a real user and be torn down after.
const API_BASE = "http://localhost:8000";
const E2E_SECRET =
  process.env.POLARIS_E2E_INTERNAL_SECRET ?? "polaris-e2e-local-dev";

async function seedDisposableTenant(): Promise<{
  tenantId: string;
  tenantSlug: string;
  adminUserId: string;
}> {
  const res = await fetch(`${API_BASE}/internal/e2e/tenants`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${E2E_SECRET}`,
    },
    body: JSON.stringify({ tier: "pro" }),
  });
  if (!res.ok) {
    throw new Error(`seed_tenant failed ${res.status}: ${await res.text()}`);
  }
  const d = (await res.json()) as {
    tenant_id: string;
    tenant_slug: string;
    tenant_admin_user_id: string;
  };
  return {
    tenantId: d.tenant_id,
    tenantSlug: d.tenant_slug,
    adminUserId: d.tenant_admin_user_id,
  };
}

async function deleteDisposableTenant(tenantId: string): Promise<void> {
  await fetch(`${API_BASE}/internal/e2e/tenants/${tenantId}`, {
    method: "DELETE",
    headers: { Authorization: `Bearer ${E2E_SECRET}` },
  }).catch(() => undefined);
}

// ────────────────────────────────────────────────────────────────────────────
// Usage note (no params)
// ────────────────────────────────────────────────────────────────────────────

test.describe("/impersonation — no params", () => {
  test("shows usage note when tenantId and userId are missing", async ({
    page,
  }) => {
    const pom = new ImpersonationPage(page);
    await pom.gotoWithParams();

    await expect(pom.usageNoteHeading).toBeVisible({ timeout: 10_000 });
    await expect(pom.usageNoteText).toBeVisible();
    // "Go to Tenants" link should be present
    await expect(
      page.getByRole("link", { name: /go to tenants/i }),
    ).toBeVisible();
    await pom.assertNoBrandLeak();
  });

  test("shows usage note when only tenantId is provided (userId missing)", async ({
    page,
  }) => {
    const pom = new ImpersonationPage(page);
    await pom.gotoWithParams({ tenantId: TENANT_IDS.proC });
    await expect(pom.usageNoteHeading).toBeVisible({ timeout: 10_000 });
  });
});

// ────────────────────────────────────────────────────────────────────────────
// Impersonation page with valid params
// ────────────────────────────────────────────────────────────────────────────

test.describe("/impersonation — with params (proC tenant)", () => {
  // Seeded admin user id for proC. The seed creates a user whose id is
  // stable across seed resets. Placeholder used until exported from seed.
  // TODO: replace PLACEHOLDER_USER_ID with the real seeded user id from
  //       seed_e2e_multitenant.py once it's exported in fixtures.ts.
  const PLACEHOLDER_USER_ID = "00000000-0000-0000-0000-0000000000c1";

  test("renders page heading and security notice with valid params", async ({
    page,
  }) => {
    const pom = new ImpersonationPage(page);
    await pom.gotoWithParams({
      tenantId: TENANT_IDS.proC,
      userId: PLACEHOLDER_USER_ID,
      slug: TENANT_SLUGS.proC,
      email: "e2e-admin-c@polaris-local.dev",
    });

    await expect(pom.heading).toBeVisible({ timeout: 10_000 });

    // Security notice box (impersonation/page.tsx lines 71-88)
    await expect(page.getByText(/security notice/i)).toBeVisible();
    await expect(
      page.getByText(/does not elevate portal privileges/i),
    ).toBeVisible();
    await expect(
      page.getByText(/every portal action.*is audited/i),
    ).toBeVisible();
    await expect(
      page.getByText(/httponly.*cannot be read by portal javascript/i),
    ).toBeVisible();
    await expect(page.getByText(/expires in 60 minutes/i)).toBeVisible();

    await pom.assertNoBrandLeak();
  });

  test("'Impersonate' button is visible in idle state", async ({ page }) => {
    const pom = new ImpersonationPage(page);
    await pom.gotoWithParams({
      tenantId: TENANT_IDS.proC,
      userId: PLACEHOLDER_USER_ID,
      slug: TENANT_SLUGS.proC,
    });

    await expect(pom.impersonateBtn).toBeVisible({ timeout: 10_000 });
  });

  test("clicking 'Impersonate' opens confirm dialog", async ({ page }) => {
    const pom = new ImpersonationPage(page);
    await pom.gotoWithParams({
      tenantId: TENANT_IDS.proC,
      userId: PLACEHOLDER_USER_ID,
      slug: TENANT_SLUGS.proC,
    });

    await pom.openConfirmDialog();

    // Confirm dialog structure (PreImpersonationModal.tsx lines 88-129)
    await expect(pom.confirmDialog).toBeVisible({ timeout: 5_000 });
    await expect(pom.confirmHeading).toBeVisible();
    await expect(pom.startSessionBtn).toBeVisible();
    await expect(pom.cancelBtn).toBeVisible();
  });

  test("'Cancel' in confirm dialog returns to idle state", async ({ page }) => {
    const pom = new ImpersonationPage(page);
    await pom.gotoWithParams({
      tenantId: TENANT_IDS.proC,
      userId: PLACEHOLDER_USER_ID,
      slug: TENANT_SLUGS.proC,
    });

    await pom.openConfirmDialog();
    await pom.cancelBtn.click();

    // Back to idle: dialog gone, Impersonate button visible again
    await expect(pom.confirmDialog).not.toBeVisible({ timeout: 3_000 });
    await expect(pom.impersonateBtn).toBeVisible({ timeout: 3_000 });
  });

  test("Escape key closes confirm dialog", async ({ page }) => {
    const pom = new ImpersonationPage(page);
    await pom.gotoWithParams({
      tenantId: TENANT_IDS.proC,
      userId: PLACEHOLDER_USER_ID,
      slug: TENANT_SLUGS.proC,
    });

    await pom.openConfirmDialog();
    await page.keyboard.press("Escape");

    await expect(pom.confirmDialog).not.toBeVisible({ timeout: 3_000 });
    await expect(pom.impersonateBtn).toBeVisible({ timeout: 3_000 });
  });

  test("start session: banner appears and stop session clears it", async ({
    page,
  }) => {
    // Use a disposable tenant whose admin user_id is returned by the seeder
    // (the shared-fixture admin id is DB-generated, hence not usable here).
    // Starting a session mutates impersonation+audit state; deleting the
    // tenant in the finally block tears that down.
    const { tenantId, tenantSlug, adminUserId } = await seedDisposableTenant();
    try {
      const pom = new ImpersonationPage(page);
      await pom.gotoWithParams({
        tenantId,
        userId: adminUserId,
        slug: tenantSlug,
      });

      await pom.openConfirmDialog();
      await pom.startSession(); // waits for data-testid="impersonation-banner"

      // Banner: "Session started. Portal opened in new tab."
      await expect(
        page.getByRole("status").filter({ hasText: /session started/i }),
      ).toBeVisible({ timeout: 10_000 });
      await expect(pom.stopSessionBtn).toBeVisible();

      // Stop the session
      await pom.stopSession(); // waits for banner to disappear

      await expect(pom.impersonateBtn).toBeVisible({ timeout: 5_000 });
    } finally {
      await deleteDisposableTenant(tenantId);
    }
  });

  test("error state renders 'Retry' link when start fails", async ({
    page,
  }) => {
    // With the placeholder user id, the POST /api/admin/impersonation/start
    // will return an error (user not found or similar).
    const pom = new ImpersonationPage(page);
    await pom.gotoWithParams({
      tenantId: TENANT_IDS.proC,
      userId: PLACEHOLDER_USER_ID,
      slug: TENANT_SLUGS.proC,
    });

    await pom.openConfirmDialog();
    await pom.startSessionBtn.click();

    // May enter "starting" state then error state
    await expect(pom.errorAlert.or(pom.sessionBanner)).toBeVisible({
      timeout: 10_000,
    });

    if (await pom.errorAlert.isVisible()) {
      await expect(
        page
          .getByRole("button", { name: /retry/i })
          .or(page.getByText(/retry/i)),
      ).toBeVisible();
    }
  });
});
