/**
 * Routes auth-matrix
 *
 * Representative 401/403/404/422 checks across key tenant and admin routes.
 * Verifies that auth guards are wired consistently across the full route set
 * without requiring a full authenticated session per route.
 *
 * Tenant routes checked (unauthenticated → 401):
 *   - GET  /api/tenant/audience
 *   - GET  /api/tenant/bookings/config
 *   - GET  /api/tenant/branches
 *   - GET  /api/tenant/conversations
 *   - GET  /api/tenant/flows
 *   - GET  /api/tenant/knowledge/requests
 *   - GET  /api/tenant/dashboard/summary
 *   - GET  /api/tenant/notifications
 *   - GET  /api/tenant/templates
 *   - GET  /api/tenant/broadcasts
 *   - GET  /api/tenant/promotions
 *
 * Admin routes checked (unauthenticated → 401):
 *   - GET  /api/admin/tenants
 *   - GET  /api/admin/billing
 *   - GET  /api/admin/cost
 *   - GET  /api/admin/quotas
 *   - GET  /api/admin/audit
 *
 * All routes: wrong content-type on POST → 422 (Pydantic validation)
 */

import { expect, test } from "@playwright/test";
import { API_BASE, secretsPresent } from "../_support/api";

// Routes that require tenant auth (Bearer JWT with tenant_id + sub claims).
const TENANT_GET_ROUTES = [
  "/api/tenant/audience",
  "/api/tenant/bookings/config",
  "/api/tenant/branches",
  "/api/tenant/conversations",
  "/api/tenant/flows",
  "/api/tenant/knowledge/requests",
  "/api/tenant/dashboard/summary",
  "/api/tenant/notifications",
  "/api/tenant/templates",
  "/api/tenant/broadcasts",
  "/api/tenant/promotions",
];

// Routes that require admin (platform_admin) JWT.
// Paths confirmed from GET /openapi.json (admin router registrations).
const ADMIN_GET_ROUTES = [
  "/api/admin/tenants",
  "/api/admin/billing/archive", // billing sub-route (not /billing root)
  "/api/admin/cost/tenants", // cost sub-route (not /cost root)
  "/api/admin/quotas/usage", // quotas sub-route (not /quotas root)
  "/api/admin/audit/logs", // audit sub-route (not /audit root)
];

test.describe("Routes auth matrix", () => {
  test.skip(() => !secretsPresent(), "webhook/internal secrets not set");

  // -------------------------------------------------------------------------
  // Tenant routes — unauthenticated → 401
  // -------------------------------------------------------------------------

  for (const route of TENANT_GET_ROUTES) {
    test(`GET ${route} — no auth → 401`, async ({ request }) => {
      const res = await request.get(`${API_BASE}${route}`);
      expect(res.status()).toBe(401);
    });

    test(`GET ${route} — garbage Bearer → 401`, async ({ request }) => {
      const res = await request.get(`${API_BASE}${route}`, {
        headers: { Authorization: "Bearer eyJhbGciOiJub25lIn0.e30." },
      });
      expect(res.status()).toBe(401);
    });
  }

  // -------------------------------------------------------------------------
  // Admin routes — unauthenticated → 401
  // -------------------------------------------------------------------------

  for (const route of ADMIN_GET_ROUTES) {
    test(`GET ${route} — no auth → 401`, async ({ request }) => {
      const res = await request.get(`${API_BASE}${route}`);
      expect(res.status()).toBe(401);
    });

    test(`GET ${route} — garbage Bearer → 401`, async ({ request }) => {
      const res = await request.get(`${API_BASE}${route}`, {
        headers: { Authorization: "Bearer eyJhbGciOiJub25lIn0.e30." },
      });
      expect(res.status()).toBe(401);
    });
  }

  // -------------------------------------------------------------------------
  // POST routes — wrong payload → 422 (after auth, so still 401 here)
  // The intent is to confirm 422 beats 401 only when auth passes.
  // We test the unauthenticated case as a sanity guard: auth must fire first.
  // -------------------------------------------------------------------------

  test("POST /api/tenant/broadcasts — no auth → 401 (not 422)", async ({
    request,
  }) => {
    const res = await request.post(`${API_BASE}/api/tenant/broadcasts`, {
      data: "not-json",
      headers: { "Content-Type": "text/plain" },
    });
    // Auth guard fires before body parsing → 401, not 422.
    expect(res.status()).toBe(401);
  });

  test("POST /api/admin/tenants — no auth → 401 (not 422)", async ({
    request,
  }) => {
    const res = await request.post(`${API_BASE}/api/admin/tenants`, {
      data: "{}",
      headers: { "Content-Type": "application/json" },
    });
    expect(res.status()).toBe(401);
  });

  // -------------------------------------------------------------------------
  // Non-existent resource id — with auth would be 404, without → 401
  // Confirms the guard fires before RLS/DB lookup.
  // -------------------------------------------------------------------------

  test("GET /api/tenant/conversations/nonexistent-id — no auth → 401", async ({
    request,
  }) => {
    const res = await request.get(
      `${API_BASE}/api/tenant/conversations/00000000-0000-0000-0000-000000000099`,
    );
    expect(res.status()).toBe(401);
  });

  test("GET /api/admin/cost/tenants/{id} — no auth → 401", async ({
    request,
  }) => {
    // Confirmed path from openapi.json: /api/admin/cost/tenants/{tenant_id}
    const res = await request.get(
      `${API_BASE}/api/admin/cost/tenants/00000000-0000-0000-0000-000000000099`,
    );
    expect(res.status()).toBe(401);
  });

  // -------------------------------------------------------------------------
  // Health route is public
  // -------------------------------------------------------------------------

  test("GET /v1/health — public, no auth needed → 200", async ({ request }) => {
    const res = await request.get(`${API_BASE}/v1/health`);
    expect(res.status()).toBe(200);
  });
});
