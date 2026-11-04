/**
 * PROD SMOKE — WS2 (provisioning half): new-tenant default agent tools.
 *
 * Proves admin_tenants.py's new default (`DEFAULT_RETRIEVAL_TOOLS` =
 * lookup_products + lookup_promotions, on top of search_knowledge +
 * handoff_to_human) actually applies to a freshly-provisioned tenant in prod.
 * Existing tenants are unaffected by design — this spec is specifically
 * about the CREATE path, not a retrofit.
 *
 * Uses the real admin → portal impersonation flow (the same one
 * admin_demos.py's own docstring calls out as "the existing admin → portal
 * impersonation flow" for inspecting a tenant's settings) rather than
 * inventing a new inspection path. The impersonation cookie is domain-scoped
 * to `.getpolaris.my` (services/impersonation.py COOKIE_DOMAIN), so it's
 * automatically sent when this SAME browser context navigates to the portal
 * origin — no separate portal login needed for this one check.
 *
 * Teardown: stop impersonation, then soft-delete the throwaway tenant
 * (routine ADR-029 admin action — never touches the gateway).
 */

import { test, expect } from "@playwright/test";

const SLUG = `qa-smoke-${new Date().toISOString().slice(0, 10)}-${Date.now().toString().slice(-5)}`;

test.describe("WS2 — new-tenant default agent tools", () => {
  let tenantId: string | undefined;

  test.afterEach(async ({ request }) => {
    // Best-effort cleanup even if an assertion above failed mid-test.
    await request.delete("/api/proxy/api/admin/impersonate").catch(() => {});
    if (tenantId) {
      await request
        .delete(`/api/proxy/api/admin/tenants/${tenantId}`)
        .catch(() => {});
    }
  });

  test("new tenant ships with lookup_products + lookup_promotions enabled", async ({
    page,
    request,
  }) => {
    const createRes = await request.post("/api/proxy/api/admin/tenants", {
      data: {
        display_name: `QA Smoke ${new Date().toISOString()}`,
        slug: SLUG,
        plan_tier: "pro",
        billing_cycle: "monthly",
        admin_email: `qa-smoke+${Date.now()}@getpolaris.my`,
        admin_display_name: "QA Smoke Admin",
      },
    });
    expect(createRes.ok()).toBeTruthy();
    const created = await createRes.json();
    tenantId = created.id;
    expect(tenantId).toBeTruthy();

    const detailRes = await request.get(
      `/api/proxy/api/admin/tenants/${tenantId}`,
    );
    expect(detailRes.ok()).toBeTruthy();
    const detail = await detailRes.json();
    const primaryUserId = detail.primary_user_id;
    expect(primaryUserId).toBeTruthy();

    const impersonateRes = await request.post(
      `/api/proxy/api/admin/impersonate/${tenantId}/${primaryUserId}`,
    );
    expect(impersonateRes.ok()).toBeTruthy();

    // Domain-scoped cookie travels automatically to the portal origin.
    await page.goto("https://portal.getpolaris.my/agent");

    const productsToggle = page
      .locator("label", { hasText: "Look up products" })
      .locator('input[type="checkbox"]');
    const promotionsToggle = page
      .locator("label", { hasText: "Look up promotions" })
      .locator('input[type="checkbox"]');

    await expect(productsToggle).toBeChecked();
    await expect(promotionsToggle).toBeChecked();
  });
});
