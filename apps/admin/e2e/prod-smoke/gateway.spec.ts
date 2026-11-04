/**
 * PROD SMOKE — WS1: gateway tenant protection.
 *
 * Verifies the delete-guards added around the fixed-UUID demo gateway
 * (services/demo_sessions.POLARIS_INTERNAL_TENANT_ID) actually hold in the
 * live deployment, and that the idempotent recovery endpoint is a safe no-op
 * when the gateway is already healthy.
 *
 * Read-only except for two requests that are EXPECTED to be rejected (403).
 * This spec must NEVER observe a 2xx from the delete/purge calls — a 2xx
 * there means the guard regressed and the gateway may have just been deleted.
 *
 * Auth: real NextAuth session via a pre-captured storageState
 * (see e2e/prod-smoke/README.md) — requests go through the admin app's own
 * `/api/proxy/*` route so the session cookie mints the Bearer server-side,
 * exactly like the real UI does (apps/admin/app/api/proxy/[...path]/route.ts).
 */

import { test, expect } from "@playwright/test";

const GATEWAY_ID = "00000000-0000-0000-0000-000000000001";
// The live gateway tenant's slug is an env override, not committed —
// see e2e/prod-smoke/README.md.
const GATEWAY_SLUG = process.env.SMOKE_TENANT_SLUG ?? "demo-tenant";

test.describe("WS1 — gateway protection", () => {
  test("gateway tenant is reachable by direct link and healthy", async ({
    page,
  }) => {
    // Excluded from the normal /tenants list view by design (is_demo=true) —
    // must deep-link straight to the detail page.
    const res = await page.goto(`/tenants/${GATEWAY_ID}`);
    expect(res?.ok()).toBeTruthy();

    // Confirms the slug set via SMOKE_TENANT_SLUG really is what's live
    // today, before any mutating spec below assumes it.
    await expect(page.getByText(GATEWAY_SLUG, { exact: false })).toBeVisible();
    await expect(page.getByText(/active/i).first()).toBeVisible();
  });

  test("soft-delete is blocked with gateway_protected", async ({ request }) => {
    const res = await request.delete(
      `/api/proxy/api/admin/tenants/${GATEWAY_ID}`,
    );
    expect(res.status()).toBe(403);
    const body = await res.json();
    expect(JSON.stringify(body)).toContain("gateway_protected");
  });

  test("purge is blocked with gateway_protected", async ({ request }) => {
    const res = await request.delete(
      `/api/proxy/api/admin/tenants/${GATEWAY_ID}/purge`,
    );
    expect(res.status()).toBe(403);
    const body = await res.json();
    expect(JSON.stringify(body)).toContain("gateway_protected");
  });

  test("gateway/ensure is a safe no-op when already healthy", async ({
    request,
  }) => {
    const res = await request.post("/api/proxy/api/admin/demos/gateway/ensure");
    expect(res.ok()).toBeTruthy();
    const body = await res.json();
    expect(body.ok).toBe(true);
    // The gateway is already healthy going into this suite (asserted above),
    // so recovery should find nothing to restore.
    expect(body.persona_restored).toBe(false);
  });
});
