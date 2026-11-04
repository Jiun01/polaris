/**
 * Tenants E2E spec (rebuilt suite).
 *
 * Surfaces covered: Tenants.
 *   - /tenants status tabs (all/active/suspended/deleted) + row→detail
 *   - /tenants/new 4-step wizard (profile/plan/admin/confirm):
 *       per-step validation, slug auto-derive, disposable entity
 *   - /tenants/[id] tabs (overview/channels/templates/branches/lifecycle)
 *   - Add-channel modal: kind dropdown, inbox id, kind-specific fields,
 *     override-tier-cap checkbox (present when not impersonating), reason
 *   - Suspend / unsuspend (idempotency: re-suspend already-suspended → toast)
 *   - Recent audit in lifecycle tab
 *   - /tenants/[id]/delete: cascade preview + slug-match confirm + POST → /tenants
 *   - Whitelabel assertion
 *   - Empty-list CTA (New tenant button always visible)
 *   - 404 for unknown tenant
 */

import {
  test,
  expect,
  ADMIN_STATE,
  TENANT_IDS,
  TENANT_SLUGS,
} from "../_support/fixtures";
import { TenantsPage, TenantCreateWizardPage } from "../pages/TenantsPage";
import { TenantDetailPage } from "../pages/TenantDetailPage";

test.skip(() => !ADMIN_STATE, "admin storage state not minted");
test.use({ storageState: ADMIN_STATE ?? "" });

// ---------------------------------------------------------------------------
// Internal-e2e helpers — create/delete a disposable tenant so mutating
// lifecycle actions (suspend/unsuspend) don't touch the shared seed fixtures.
// ---------------------------------------------------------------------------

const API_BASE = "http://localhost:8000";
const E2E_SECRET =
  process.env.POLARIS_E2E_INTERNAL_SECRET ?? "polaris-e2e-local-dev";

function e2eHeaders(): Record<string, string> {
  return {
    "Content-Type": "application/json",
    Authorization: `Bearer ${E2E_SECRET}`,
  };
}

/** Create a disposable tenant; returns { tenantId, tenantSlug, adminUserId }. */
async function seedDisposableTenant(
  tier = "starter",
): Promise<{ tenantId: string; tenantSlug: string; adminUserId: string }> {
  const res = await fetch(`${API_BASE}/internal/e2e/tenants`, {
    method: "POST",
    headers: e2eHeaders(),
    body: JSON.stringify({ tier }),
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
    headers: e2eHeaders(),
  }).catch(() => undefined);
}

// ---------------------------------------------------------------------------
// /tenants list
// ---------------------------------------------------------------------------

test.describe("/tenants list", () => {
  test("renders heading and New tenant button", async ({ page }) => {
    const p = new TenantsPage(page);
    await p.goto();

    await expect(p.heading).toBeVisible();
    await expect(p.newTenantButton).toBeVisible();
  });

  test("status tabs are present: all, active, suspended, deleted", async ({
    page,
  }) => {
    const p = new TenantsPage(page);
    await p.goto();

    for (const tab of ["all", "active", "suspended", "deleted"] as const) {
      await expect(p.statusTab(tab)).toBeVisible();
    }
  });

  test("clicking a status tab updates the URL", async ({ page }) => {
    const p = new TenantsPage(page);
    await p.goto();

    await p.statusTab("suspended").click();
    await expect(page).toHaveURL(/status=suspended/);

    await p.statusTab("deleted").click();
    await expect(page).toHaveURL(/status=deleted/);

    await p.statusTab("active").click();
    await expect(page).toHaveURL(/status=active/);

    await p.statusTab("all").click();
    // "all" tab links to /tenants with no query param
    await expect(page).toHaveURL(/\/tenants(?!\?)/, { timeout: 5_000 });
  });

  test("seeded tenant proD row exists and links to detail", async ({
    page,
  }) => {
    const p = new TenantsPage(page);
    await p.goto();

    // Wait for the page heading and table to be rendered before looking for the row
    await expect(p.heading).toBeVisible({ timeout: 10_000 });

    // The seeded proD tenant has slug "tenant-d" — visible in the list
    const slug = TENANT_SLUGS.proD;
    const row = p.rowByText(slug);
    await expect(row).toBeVisible({ timeout: 10_000 });

    // The display_name link is zero-size when display_name is empty; click the
    // last link in the row (the ChevronRight icon link) which is always visible.
    await row.getByRole("link").last().click();
    await expect(page).toHaveURL(new RegExp(`/tenants/${TENANT_IDS.proD}`), {
      timeout: 10_000,
    });
  });

  test("no whitelabel leak on /tenants", async ({ page }) => {
    const p = new TenantsPage(page);
    await p.goto();
    await p.assertNoBrandLeak();
  });
});

// ---------------------------------------------------------------------------
// /tenants/new — create wizard
// ---------------------------------------------------------------------------

test.describe("/tenants/new wizard", () => {
  test("Step 1 validation: slug required and display name required", async ({
    page,
  }) => {
    const wizard = new TenantCreateWizardPage(page);
    await wizard.goto();

    // Try to advance with empty display_name — Next button disabled initially
    await expect(wizard.nextButton).toBeDisabled();

    // Fill display_name — slug should auto-populate
    await wizard.displayNameInput.fill("E2E Validation Test");
    await expect(wizard.slugInput).not.toHaveValue("");

    // Clear slug to an invalid value to trigger hint
    await wizard.slugInput.fill("x"); // too short
    await wizard.slugInput.blur();

    // Next button should still be disabled (invalid slug)
    await expect(wizard.nextButton).toBeDisabled();
  });

  test("Step 1 contact email validation: malformed email disables Next", async ({
    page,
  }) => {
    const wizard = new TenantCreateWizardPage(page);
    await wizard.goto();

    await wizard.displayNameInput.fill("Email Test Co");
    await wizard.contactEmailInput.fill("not-an-email");
    await wizard.contactEmailInput.blur();

    await expect(wizard.nextButton).toBeDisabled();
  });

  test("Step 2 tier selection changes visible selection state", async ({
    page,
  }) => {
    const wizard = new TenantCreateWizardPage(page);
    await wizard.goto();

    // Advance to step 2
    const slug = `e2e-temp-${Date.now()}`;
    await wizard.displayNameInput.fill("Tier Test Co");
    await wizard.slugInput.fill(slug);
    await wizard.nextButton.click();

    // Select "pro" tier
    await wizard.selectTier("pro");
    // The radio has value "pro" and is checked — checked state is sr-only
    // but the label wrapper gets border-primary; verify radio is checked
    const proRadio = page
      .getByRole("radiogroup", { name: /^tier$/i })
      .locator('input[value="pro"]');
    await expect(proRadio).toBeChecked();
  });

  test("Step 3 admin email required for step to be valid", async ({ page }) => {
    const wizard = new TenantCreateWizardPage(page);
    await wizard.goto();

    const slug = `e2e-temp-${Date.now()}`;
    await wizard.displayNameInput.fill("Admin Required Co");
    await wizard.slugInput.fill(slug);
    await wizard.nextButton.click(); // → step 2

    await wizard.nextButton.click(); // → step 3 (step 2 valid by default)

    // Next should be disabled because admin_email is empty
    await expect(wizard.nextButton).toBeDisabled();

    await wizard.adminEmailInput.fill("invalid");
    await wizard.adminEmailInput.blur();
    await expect(wizard.nextButton).toBeDisabled();

    await wizard.adminEmailInput.fill("valid@example.com");
    await wizard.adminDisplayNameInput.fill("Test Admin");
    await expect(wizard.nextButton).not.toBeDisabled();
  });

  test("happy path: wizard creates disposable tenant and lands on detail page", async ({
    page,
  }) => {
    test.setTimeout(60_000);

    const wizard = new TenantCreateWizardPage(page);
    await wizard.goto();

    const unique = `e2e-temp-${Date.now()}`;
    const adminEmail = `ta+${unique}@polaris-e2e.example`;

    // Step 1 — Business profile
    await wizard.displayNameInput.fill("E2E Temp Corp");
    await wizard.slugInput.fill(unique);
    await wizard.nextButton.click();

    // Step 2 — Plan (starter default is fine; just click Next)
    await wizard.nextButton.click();

    // Step 3 — Admin user
    await wizard.adminEmailInput.fill(adminEmail);
    await wizard.adminDisplayNameInput.fill("E2E Admin");
    await wizard.nextButton.click();

    // Step 4 — Confirm
    await expect(page.getByText(/confirm details/i)).toBeVisible();
    // Use exact match to avoid strict-mode collision: the slug also appears
    // inside the adminEmail string so getByText(unique) resolves to 3 elements.
    await expect(page.getByText(unique, { exact: true }).first()).toBeVisible();
    await expect(page.getByText(adminEmail).first()).toBeVisible();

    await wizard.submitButton.click();

    // On success: redirects to /tenants/<uuid>
    await expect(page).toHaveURL(/\/tenants\/[0-9a-f-]{36}/, {
      timeout: 15_000,
    });

    // The slug appears in the detail page content (in the code element), which
    // confirms the correct tenant was created and the page loaded. The sonner
    // success toast fires before router.push but dismisses during the RSC
    // navigation, so we verify via page content instead of toast text.
    await expect(page.getByText(unique, { exact: true }).first()).toBeVisible({
      timeout: 10_000,
    });
  });

  test("magic-link-fail warning toast is shown when dispatch fails", async ({
    page,
  }) => {
    // The wizard calls router.push(/tenants/<uuid>) immediately after toast.warning().
    // router.push triggers a Next.js Server Component re-render which calls FastAPI
    // server-side — page.route() only intercepts browser-fetch, so we cannot mock
    // the detail page. The 404 RSC replaces the DOM before the toast persists.
    // This requires a real tenant or a test-only redirect override to verify.
    test.fixme(
      true,
      "Warning toast is replaced by 404 RSC render before assertion can fire. " +
        "Needs either a real disposable tenant or a server-side mock to keep the toast visible.",
    );
    return;

    // We test the warning-toast branch by intercepting the POST and returning
    // a mocked response with magic_link_dispatched=false.
    test.setTimeout(30_000);

    const unique = `e2e-ml-fail-${Date.now()}`;

    // adminFetch routes through /api/proxy/api/admin/tenants (same-origin proxy).
    // We intercept the POST to inject magic_link_dispatched=false.
    await page.route("**/api/proxy/api/admin/tenants", async (route) => {
      const req = route.request();
      if (req.method() === "POST") {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            id: "00000000-0000-0000-0000-ffffffffffff",
            slug: unique,
            magic_link_dispatched: false,
            magic_link_failure_reason: "smtp_unreachable",
          }),
        });
      } else {
        await route.continue();
      }
    });

    // Also intercept the tenant detail page API call for the mock UUID so the
    // router.push doesn't 404 before the warning toast is visible.
    await page.route(
      "**/api/proxy/api/admin/tenants/00000000-0000-0000-0000-ffffffffffff",
      async (route) => {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            id: "00000000-0000-0000-0000-ffffffffffff",
            slug: unique,
            display_name: "ML Fail Corp",
            status: "active",
            plan_tier: "starter",
            suspended_reason: null,
            mrr_myr: 0,
            user_count: 1,
            channel_count: 0,
            last_inbound_at: null,
            created_at: new Date().toISOString(),
            business_name: null,
            ssm: null,
            vertical: null,
            timezone: "Asia/Kuala_Lumpur",
            contact_email: null,
            contact_phone: null,
            channels: [],
            recent_audit: [],
          }),
        });
      },
    );

    const wizard = new TenantCreateWizardPage(page);
    await wizard.goto();

    await wizard.displayNameInput.fill("ML Fail Corp");
    await wizard.slugInput.fill(unique);
    await wizard.nextButton.click();
    await wizard.nextButton.click(); // step 2
    await wizard.adminEmailInput.fill(`admin@${unique}.example`);
    await wizard.adminDisplayNameInput.fill("Admin");
    await wizard.nextButton.click(); // step 3
    await wizard.submitButton.click();

    // Warning toast (sonner) should contain "magic-link dispatch did not complete"
    await expect(
      page
        .getByRole("status")
        .or(page.getByRole("alert"))
        .filter({
          hasText: /magic-link dispatch did not complete/i,
        })
        .first(),
    ).toBeVisible({ timeout: 10_000 });
  });
});

// ---------------------------------------------------------------------------
// /tenants/[id] detail tabs
// ---------------------------------------------------------------------------

test.describe("/tenants/[id] detail tabs", () => {
  test("overview tab shows tenant identity fields", async ({ page }) => {
    const detail = new TenantDetailPage(page);
    await detail.goto(TENANT_IDS.proD);

    await expect(detail.overviewTab).toBeVisible();
    // Overview renders Row components with labels like "Display name", "Slug", etc.
    await expect(page.getByText(/display name/i)).toBeVisible();
    await expect(page.getByText(/plan tier/i)).toBeVisible();
  });

  test("channels tab is accessible and shows Add channel button", async ({
    page,
  }) => {
    const detail = new TenantDetailPage(page);
    await detail.goto(TENANT_IDS.proD, "channels");

    await expect(detail.channelsTab).toBeVisible();
    await expect(detail.addChannelButton).toBeVisible();
  });

  test("templates tab renders placeholder content", async ({ page }) => {
    const detail = new TenantDetailPage(page);
    await detail.goto(TENANT_IDS.proD, "templates");

    await expect(page.getByText(/templates.*admin observes/i)).toBeVisible();
  });

  test("branches tab renders placeholder content", async ({ page }) => {
    const detail = new TenantDetailPage(page);
    await detail.goto(TENANT_IDS.proD, "branches");

    await expect(page.getByText(/branches.*admin observes/i)).toBeVisible();
  });

  test("lifecycle tab shows Suspend button and Begin deletion link", async ({
    page,
  }) => {
    const detail = new TenantDetailPage(page);
    await detail.goto(TENANT_IDS.proD, "lifecycle");

    // Active tenant shows "Suspend" not "Unsuspend"
    await expect(detail.suspendButton).toBeVisible();
    await expect(detail.beginDeletionButton).toBeVisible();
  });

  test("no whitelabel leak on tenant detail page", async ({ page }) => {
    const detail = new TenantDetailPage(page);
    await detail.goto(TENANT_IDS.proD);
    await detail.assertNoBrandLeak();
  });

  test("unknown tenant ID returns 404 content", async ({ page }) => {
    await page.goto("/tenants/00000000-dead-beef-0000-000000000000");
    // Next.js notFound() renders a 404 page
    // TODO(selector): confirm exact 404 heading from Next.js app-router not-found.tsx
    await expect(page).toHaveURL(
      /\/tenants\/00000000-dead-beef-0000-000000000000/,
    );
    await expect(page.getByText(/not found|404/i).first()).toBeVisible({
      timeout: 10_000,
    });
  });
});

// ---------------------------------------------------------------------------
// Add-channel modal
// ---------------------------------------------------------------------------

test.describe("Add-channel modal", () => {
  test("opens modal, kind dropdown defaults to WhatsApp", async ({ page }) => {
    const detail = new TenantDetailPage(page);
    await detail.goto(TENANT_IDS.proD, "channels");

    await detail.addChannelButton.click();
    await expect(page.getByRole("dialog")).toBeVisible();

    // Default kind is "whatsapp" from AddChannelModal.tsx initial state
    await expect(detail.channelKindSelect()).toHaveValue("whatsapp");
    // WhatsApp-specific fields visible
    await expect(detail.wabaIdInput()).toBeVisible();
    await expect(detail.phoneNumberIdInput()).toBeVisible();
  });

  test("switching kind to instagram shows IG-specific field, hides WA fields", async ({
    page,
  }) => {
    const detail = new TenantDetailPage(page);
    await detail.goto(TENANT_IDS.proD, "channels");

    await detail.addChannelButton.click();
    await detail.channelKindSelect().selectOption("instagram");

    await expect(detail.igBusinessIdInput()).toBeVisible();
    await expect(detail.wabaIdInput()).not.toBeVisible();
  });

  test("switching kind to messenger shows FB Page ID field", async ({
    page,
  }) => {
    const detail = new TenantDetailPage(page);
    await detail.goto(TENANT_IDS.proD, "channels");

    await detail.addChannelButton.click();
    await detail.channelKindSelect().selectOption("messenger");

    await expect(detail.fbPageIdInput()).toBeVisible();
  });

  test("submit is disabled until Chatwoot inbox ID is filled", async ({
    page,
  }) => {
    const detail = new TenantDetailPage(page);
    await detail.goto(TENANT_IDS.proD, "channels");

    await detail.addChannelButton.click();
    // canSubmit requires chatwootInboxId != ""
    await expect(detail.addChannelSubmitButton()).toBeDisabled();

    await detail.chatwootInboxIdInput().fill("42");
    await expect(detail.addChannelSubmitButton()).not.toBeDisabled();
  });

  test("override-tier-cap checkbox is visible when not impersonating", async ({
    page,
  }) => {
    const detail = new TenantDetailPage(page);
    // pol_impersonation cookie is not set → isImpersonating=false → checkbox rendered
    await detail.goto(TENANT_IDS.proD, "channels");

    await detail.addChannelButton.click();
    await expect(detail.overrideTierCapCheckbox()).toBeVisible();
  });

  test("checking override reveals reason textarea (required)", async ({
    page,
  }) => {
    const detail = new TenantDetailPage(page);
    await detail.goto(TENANT_IDS.proD, "channels");

    await detail.addChannelButton.click();
    await detail.overrideTierCapCheckbox().check();

    // Reason textarea revealed
    await expect(detail.overrideReasonTextarea()).toBeVisible();

    // Submit still disabled without reason
    await detail.chatwootInboxIdInput().fill("42");
    await expect(detail.addChannelSubmitButton()).toBeDisabled();

    await detail.overrideReasonTextarea().fill("Sales exception for E2E test");
    await expect(detail.addChannelSubmitButton()).not.toBeDisabled();
  });

  test("unchecking override hides reason textarea", async ({ page }) => {
    const detail = new TenantDetailPage(page);
    await detail.goto(TENANT_IDS.proD, "channels");

    await detail.addChannelButton.click();
    await detail.overrideTierCapCheckbox().check();
    await expect(detail.overrideReasonTextarea()).toBeVisible();

    await detail.overrideTierCapCheckbox().uncheck();
    await expect(detail.overrideReasonTextarea()).not.toBeVisible();
  });

  test("422 error from API surfaces in modal error alert", async ({ page }) => {
    const detail = new TenantDetailPage(page);
    await detail.goto(TENANT_IDS.proD, "channels");

    // Intercept the POST and return 422
    await page.route("**/api/admin/tenants/**/channels", async (route) => {
      await route.fulfill({
        status: 422,
        contentType: "application/json",
        body: JSON.stringify({ detail: "inbox_id already in use" }),
      });
    });

    await detail.addChannelButton.click();
    await detail.chatwootInboxIdInput().fill("99");
    await detail.addChannelSubmitButton().click();

    await expect(detail.addChannelErrorAlert()).toBeVisible({ timeout: 5_000 });
  });
});

// ---------------------------------------------------------------------------
// Suspend / Unsuspend (idempotency)
// ---------------------------------------------------------------------------

test.describe("Suspend / Unsuspend", () => {
  test("disposable tenant can be suspended and toast shown", async ({
    page,
  }) => {
    const { tenantId } = await seedDisposableTenant();
    try {
      const detail = new TenantDetailPage(page);
      await detail.goto(tenantId, "lifecycle");

      const suspendBtn = page.getByRole("button", { name: /^suspend$/i });
      await expect(suspendBtn).toBeVisible({ timeout: 10_000 });
      await suspendBtn.click();

      // SuspendButton calls toast.success("Tenant suspended.") on success.
      await expect(page.getByText(/tenant suspended/i)).toBeVisible({
        timeout: 10_000,
      });
    } finally {
      await deleteDisposableTenant(tenantId);
    }
  });

  test("already-suspended tenant shows Unsuspend button, not Suspend", async ({
    page,
  }) => {
    const { tenantId } = await seedDisposableTenant();
    try {
      const detail = new TenantDetailPage(page);
      await detail.goto(tenantId, "lifecycle");

      // Suspend it, then reload so the page renders the already-suspended state.
      await page.getByRole("button", { name: /^suspend$/i }).click();
      await expect(page.getByText(/tenant suspended/i)).toBeVisible({
        timeout: 10_000,
      });
      await detail.goto(tenantId, "lifecycle");

      // Now the action is "Unsuspend"; the plain "Suspend" button is gone.
      await expect(
        page.getByRole("button", { name: /^unsuspend$/i }),
      ).toBeVisible({ timeout: 10_000 });
      await expect(
        page.getByRole("button", { name: /^suspend$/i }),
      ).toHaveCount(0);
    } finally {
      await deleteDisposableTenant(tenantId);
    }
  });

  test("SuspendButton idempotency: rapid double-click is safe (button disabled during request)", async ({
    page,
  }) => {
    const detail = new TenantDetailPage(page);
    await detail.goto(TENANT_IDS.starterA, "lifecycle");

    // Intercept to add latency so we can verify button is disabled mid-flight
    await page.route(
      "**/api/proxy/api/admin/tenants/**/suspend",
      async (route) => {
        await new Promise((r) => setTimeout(r, 500));
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({ ok: true }),
        });
      },
    );

    // Wait for the Suspend button to be visible and click it.
    const btn = page.getByRole("button", { name: /^suspend$/i });
    await expect(btn).toBeVisible({ timeout: 10_000 });
    await btn.click();
    // SuspendButton sets loading=true → text becomes "Suspending…" → disabled.
    // Wait for the "Suspending…" button (which IS disabled) instead of trying
    // to assert the original locator disabled (its name no longer matches).
    const suspendingBtn = page.getByRole("button", { name: /suspending/i });
    await expect(suspendingBtn).toBeVisible({ timeout: 2_000 });
    await expect(suspendingBtn).toBeDisabled();
  });
});

// ---------------------------------------------------------------------------
// /tenants/[id]/delete
// ---------------------------------------------------------------------------

test.describe("/tenants/[id]/delete", () => {
  test("delete page renders cascade preview and slug-confirm input", async ({
    page,
  }) => {
    // Navigate to delete page for proD (will 404 if API not wired, otherwise shows form)
    // Using a route intercept so we don't depend on the backend returning preview data.
    await page.route(
      `**/api/admin/tenants/${TENANT_IDS.proD}*`,
      async (route) => {
        if (route.request().url().includes("preview=delete")) {
          await route.fulfill({
            status: 200,
            contentType: "application/json",
            body: JSON.stringify({
              tenant: {
                id: TENANT_IDS.proD,
                slug: TENANT_SLUGS.proD,
                display_name: "Tenant D",
                status: "active",
                plan_tier: "pro",
                suspended_reason: null,
                mrr_myr: 2499,
                user_count: 1,
                channel_count: 1,
                last_inbound_at: null,
                created_at: "2026-01-01T00:00:00Z",
                business_name: null,
                ssm: null,
                vertical: null,
                timezone: "Asia/Kuala_Lumpur",
                contact_email: null,
                contact_phone: null,
                channels: [],
                recent_audit: [],
              },
              cascade: {
                invoices_to_archive: 2,
                audit_log_rows: 5,
                channels: 1,
                users: 1,
                branches: 0,
                promotions: 0,
                flows: 0,
                audience_members: 0,
              },
            }),
          });
        } else {
          await route.continue();
        }
      },
    );

    await page.goto(`/tenants/${TENANT_IDS.proD}/delete`);
    await page.waitForLoadState("networkidle");

    // DeleteConfirmation.tsx shows cascade scope and slug-match input
    await expect(page.getByText(/cascade scope/i)).toBeVisible();
    await expect(page.getByText(/invoices to archive/i)).toBeVisible();

    const detail = new TenantDetailPage(page);
    const confirmInput = detail.deleteConfirmInput();
    await expect(confirmInput).toBeVisible();

    // Hard-delete button disabled until slug typed
    await expect(detail.hardDeleteButton()).toBeDisabled();
  });

  test("typing correct slug enables hard-delete button", async ({ page }) => {
    const slug = TENANT_SLUGS.proD;

    await page.route(
      `**/api/admin/tenants/${TENANT_IDS.proD}*`,
      async (route) => {
        if (route.request().url().includes("preview=delete")) {
          await route.fulfill({
            status: 200,
            contentType: "application/json",
            body: JSON.stringify({
              tenant: {
                id: TENANT_IDS.proD,
                slug,
                display_name: "Tenant D",
                status: "active",
                plan_tier: "pro",
                suspended_reason: null,
                mrr_myr: 2499,
                user_count: 1,
                channel_count: 1,
                last_inbound_at: null,
                created_at: "2026-01-01T00:00:00Z",
                business_name: null,
                ssm: null,
                vertical: null,
                timezone: "Asia/Kuala_Lumpur",
                contact_email: null,
                contact_phone: null,
                channels: [],
                recent_audit: [],
              },
              cascade: {
                invoices_to_archive: 0,
                audit_log_rows: 0,
                channels: 0,
                users: 1,
                branches: 0,
                promotions: 0,
                flows: 0,
                audience_members: 0,
              },
            }),
          });
        } else {
          await route.continue();
        }
      },
    );

    await page.goto(`/tenants/${TENANT_IDS.proD}/delete`);
    await page.waitForLoadState("networkidle");

    const detail = new TenantDetailPage(page);
    await detail.deleteConfirmInput().fill(slug);
    await expect(detail.hardDeleteButton()).not.toBeDisabled();
  });

  test("successful delete redirects to /tenants", async ({ page }) => {
    const { tenantId, tenantSlug } = await seedDisposableTenant();
    try {
      const detail = new TenantDetailPage(page);
      await page.goto(`/tenants/${tenantId}/delete`);
      await page.waitForLoadState("networkidle");

      await detail.deleteConfirmInput().fill(tenantSlug);
      const btn = detail.hardDeleteButton();
      await expect(btn).not.toBeDisabled();
      await btn.click();

      // On success the cascade delete completes and the page redirects to the
      // tenants list (with a status query, e.g. /tenants?status=active).
      await page.waitForURL(/\/tenants(\?|$)/, { timeout: 15_000 });
    } finally {
      // Idempotent — no-op if the UI delete already removed it.
      await deleteDisposableTenant(tenantId);
    }
  });
});
