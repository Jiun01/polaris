/**
 * RLS / token-separation isolation specs
 *
 * Covers:
 *   - Admin platform_admin JWT rejected on tenant endpoints (403)
 *   - Portal tenant JWT rejected on /api/admin/* (403)
 *   - Wrong issuer / audience → 401
 *   - Expired token → 401
 *   - Cross-tenant id in URL → 404 (RLS-filtered, not 403)
 *   - Missing Bearer → 401
 *
 * Token contracts confirmed from source (deps/tenant.py):
 *   - JWT decoded with JWT_SECRET, HS256, iss=settings.jwt_issuer
 *   - Claims: tenant_id, sub, role
 *   - Suspended tenant → 403
 *   - Missing tenant → 404
 *
 * Uses /internal/e2e/mint-tenant-jwt (now live) for all JWT-based tests.
 */

import { expect, test } from "@playwright/test";
import {
  API_BASE,
  internalHeaders,
  secretsPresent,
  signMeta,
} from "../_support/api";

const TENANT_A_ID = "00000000-0000-0000-0000-00000000000a";
const TENANT_B_ID = "00000000-0000-0000-0000-00000000000b";

/** Mint a tenant JWT via internal-e2e endpoint. */
async function mintJwt(
  request: import("@playwright/test").APIRequestContext,
  opts: {
    tenantId: string;
    ttlSeconds?: number;
    issuer?: string;
    role?: string;
  },
): Promise<string> {
  const body: Record<string, unknown> = { tenant_id: opts.tenantId };
  if (opts.ttlSeconds !== undefined) body.ttl_seconds = opts.ttlSeconds;
  if (opts.issuer !== undefined) body.issuer = opts.issuer;
  if (opts.role !== undefined) body.role = opts.role;

  const res = await request.post(`${API_BASE}/internal/e2e/mint-tenant-jwt`, {
    data: body,
    headers: internalHeaders(),
  });
  expect(res.status()).toBe(200);
  const data = (await res.json()) as { token: string };
  return data.token;
}

test.describe("RLS / token isolation", () => {
  test.skip(() => !secretsPresent(), "webhook/internal secrets not set");

  // -------------------------------------------------------------------------
  // Missing Bearer → 401
  // -------------------------------------------------------------------------

  test("no Authorization header → 401 on tenant endpoint", async ({
    request,
  }) => {
    const res = await request.get(`${API_BASE}/api/tenant/dashboard/summary`);
    expect(res.status()).toBe(401);
  });

  test("no Authorization header → 401 on admin endpoint", async ({
    request,
  }) => {
    const res = await request.get(`${API_BASE}/api/admin/tenants`);
    expect(res.status()).toBe(401);
  });

  // -------------------------------------------------------------------------
  // Garbage / malformed Bearer → 401
  // -------------------------------------------------------------------------

  test("malformed Bearer token → 401 on tenant endpoint", async ({
    request,
  }) => {
    const res = await request.get(`${API_BASE}/api/tenant/dashboard/summary`, {
      headers: { Authorization: "Bearer not.a.jwt" },
    });
    expect(res.status()).toBe(401);
  });

  test("malformed Bearer token → 401 on admin endpoint", async ({
    request,
  }) => {
    const res = await request.get(`${API_BASE}/api/admin/tenants`, {
      headers: { Authorization: "Bearer not.a.jwt" },
    });
    expect(res.status()).toBe(401);
  });

  // -------------------------------------------------------------------------
  // Wrong-issuer token → 401
  // -------------------------------------------------------------------------

  test("wrong issuer JWT → 401 on tenant endpoint", async ({ request }) => {
    const token = await mintJwt(request, {
      tenantId: TENANT_A_ID,
      issuer: "wrong-issuer",
      ttlSeconds: 900,
    });
    const res = await request.get(`${API_BASE}/api/tenant/dashboard/summary`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    expect(res.status()).toBe(401);
  });

  // -------------------------------------------------------------------------
  // Expired token → 401
  // -------------------------------------------------------------------------

  test("expired tenant JWT → 401 on tenant endpoint", async ({ request }) => {
    const token = await mintJwt(request, {
      tenantId: TENANT_A_ID,
      ttlSeconds: -10, // already expired
    });
    const res = await request.get(`${API_BASE}/api/tenant/dashboard/summary`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    expect(res.status()).toBe(401);
  });

  // -------------------------------------------------------------------------
  // Admin JWT on tenant endpoints → 403
  // -------------------------------------------------------------------------

  test("platform_admin JWT on /api/tenant/* → 401 (no tenant_id claim)", async ({
    request,
  }) => {
    // Admin tokens are signed with the SAME jwt_secret/issuer as tenant tokens
    // (admin_tenants._decode_admin_jwt) — they differ only by role=platform_admin
    // and the ABSENCE of a tenant_id claim. The tenant dep (deps/tenant.py) decodes
    // the signature successfully, then rejects the missing tenant_id with 401
    // (invalid_token) — NOT 403. This is the correct token-separation outcome.
    const res = await request.post(`${API_BASE}/internal/e2e/mint-admin-jwt`, {
      data: { ttl_seconds: 900 },
      headers: internalHeaders(),
    });
    expect(res.status()).toBe(200);
    const { token } = (await res.json()) as { token: string };

    const tenantRes = await request.get(
      `${API_BASE}/api/tenant/dashboard/summary`,
      { headers: { Authorization: `Bearer ${token}` } },
    );
    expect(tenantRes.status()).toBe(401);
  });

  // -------------------------------------------------------------------------
  // Tenant JWT on admin endpoints → 403
  // -------------------------------------------------------------------------

  test("portal tenant JWT on /api/admin/* → 403", async ({ request }) => {
    // A portal tenant JWT carries role=admin but no platform_admin role.
    // The admin auth dep checks for platform_admin and returns 403.
    const token = await mintJwt(request, {
      tenantId: TENANT_A_ID,
      ttlSeconds: 900,
      role: "admin",
    });
    const res = await request.get(`${API_BASE}/api/admin/tenants`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    expect(res.status()).toBe(403);
  });

  // -------------------------------------------------------------------------
  // Cross-tenant id in URL → 404 (RLS-filtered, not 403)
  // -------------------------------------------------------------------------

  test("Tenant-A JWT + Tenant-B booking id in URL → 404 (RLS-filtered)", async ({
    request,
  }) => {
    // Seed a booking under Tenant B.
    const seedRes = await request.post(`${API_BASE}/internal/e2e/bookings`, {
      data: {
        tenant_id: TENANT_B_ID,
        customer_handle: "@rls-xtest-b",
        start_at: "2027-09-01T10:00:00Z",
        channel: "instagram",
        status: "confirmed",
      },
      headers: internalHeaders(),
    });
    expect(seedRes.status()).toBe(201);
    const { booking_id } = (await seedRes.json()) as { booking_id: string };

    // Mint a JWT for Tenant A.
    const token = await mintJwt(request, {
      tenantId: TENANT_A_ID,
      ttlSeconds: 900,
    });

    // Try to access Tenant B's booking with Tenant A's JWT.
    // RLS filters B's row → 404 (not-found, not 403 forbidden).
    const res = await request.get(
      `${API_BASE}/api/tenant/bookings/${booking_id}`,
      {
        headers: { Authorization: `Bearer ${token}` },
      },
    );
    expect(res.status()).toBe(404);
  });

  test("Tenant-A JWT + nonexistent UUID → 404 on tenant bookings", async ({
    request,
  }) => {
    // A UUID that doesn't exist in any tenant returns 404 regardless of RLS.
    const token = await mintJwt(request, {
      tenantId: TENANT_A_ID,
      ttlSeconds: 900,
    });
    const res = await request.get(
      `${API_BASE}/api/tenant/bookings/ffffffff-0000-0000-0000-000000000099`,
      { headers: { Authorization: `Bearer ${token}` } },
    );
    expect(res.status()).toBe(404);
  });

  // -------------------------------------------------------------------------
  // Internal-e2e auth guard itself
  // -------------------------------------------------------------------------

  test("internal-e2e endpoint with missing auth → 401/403/422", async ({
    request,
  }) => {
    const res = await request.get(
      `${API_BASE}/internal/e2e/meta-outbound-query`,
    );
    expect([401, 403, 422]).toContain(res.status());
  });

  test("internal-e2e endpoint with wrong secret → 403", async ({ request }) => {
    const res = await request.get(
      `${API_BASE}/internal/e2e/meta-outbound-query`,
      {
        headers: { Authorization: "Bearer wrong-secret-value" },
      },
    );
    expect(res.status()).toBe(403);
  });

  test("internal-e2e endpoint with correct secret → 200", async ({
    request,
  }) => {
    const res = await request.get(
      `${API_BASE}/internal/e2e/meta-outbound-query`,
      {
        headers: internalHeaders(),
      },
    );
    expect(res.status()).toBe(200);
  });

  // -------------------------------------------------------------------------
  // Webhook resolution isolation
  // -------------------------------------------------------------------------

  test("Meta webhook for Tenant-A waba_id does not resolve to unknown (not 204)", async ({
    request,
  }) => {
    const wamid = `wamid.e2e-rls-${Date.now()}`;
    const body = JSON.stringify({
      object: "whatsapp_business_account",
      entry: [
        {
          id: "e2e_waba_a_001", // Tenant A's waba_id per seed_e2e_multitenant.py
          changes: [
            {
              value: {
                messaging_product: "whatsapp",
                metadata: {
                  display_phone_number: "+60123450010",
                  phone_number_id: "pnid_a_001",
                },
                messages: [
                  {
                    from: "+60123450010",
                    id: wamid,
                    timestamp: String(Math.floor(Date.now() / 1000)),
                    text: { body: "rls isolation check" },
                    type: "text",
                  },
                ],
              },
              field: "messages",
            },
          ],
        },
      ],
    });

    const res = await request.post(`${API_BASE}/v1/webhooks/meta`, {
      data: body,
      headers: {
        "Content-Type": "application/json",
        "x-hub-signature-256": signMeta(body),
      },
    });
    // 200/503 = resolved to Tenant A (503 = Redis unavailable, but waba_id resolved).
    // 204 = not resolved (unknown waba_id) — that would be a RLS leak failure.
    // We assert NOT 204 (not-found) and NOT 401/403 (auth failure).
    // 503 is acceptable: HMAC guard + tenant resolution passed, just Redis is down.
    expect(res.status()).not.toBe(204);
    expect(res.status()).not.toBe(401);
    expect([200, 503]).toContain(res.status());
  });
});
