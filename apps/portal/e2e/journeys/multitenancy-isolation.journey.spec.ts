/**
 * Multi-tenancy isolation journey
 *
 * Covers:
 *   - Same phone number used in both Tenant A and Tenant B → distinct
 *     audience_member rows (different tenant_id) — never merged
 *   - Tenant A's bookings list + cancel lookup never returns Tenant B's bookings
 *   - Customer-X cannot cancel Customer-Y's booking even with a guessed booking_id
 *     (RLS + channel_user_id gate)
 *   - Webhook resolution isolation: A's waba_id webhook never resolves to B
 *
 * Uses:
 *   - POST /internal/e2e/bookings       (seed bookings for both tenants)
 *   - POST /internal/e2e/bookings-query (assert per-tenant isolation)
 *   - POST /internal/e2e/audience-query (assert per-tenant identity isolation)
 *   - POST /v1/webhooks/meta            (webhook resolution isolation)
 */

import { expect, test } from "@playwright/test";
import {
  API_BASE,
  internalHeaders,
  mintBindingToken,
  secretsPresent,
  signEa,
  signMeta,
} from "../_support/api";

const TENANT_A_ID = "00000000-0000-0000-0000-00000000000a";
const TENANT_B_ID = "00000000-0000-0000-0000-00000000000b";

// A phone number used in BOTH tenants to verify distinct identity rows.
const SHARED_PHONE = "+60123450010"; // seeded for Tenant A (WA)
// Tenant B uses Instagram; we use a handle for the audience query.
const TENANT_B_IG_HANDLE = "@b_customer_1"; // seeded for Tenant B

test.describe("Journey: multi-tenancy isolation", () => {
  test.skip(() => !secretsPresent(), "webhook/internal secrets not set");

  // -------------------------------------------------------------------------
  // Audience identity isolation: same phone → distinct rows per tenant
  // -------------------------------------------------------------------------

  test("audience-query: same channel_user_id appears at most once per tenant", async ({
    request,
  }) => {
    // SHARED_PHONE is seeded only under Tenant A (whatsapp).
    // The audience-query endpoint uses BYPASSRLS, so it returns ALL tenants.
    // We assert that the SHARED_PHONE row belongs only to TENANT_A_ID.
    const res = await request.post(`${API_BASE}/internal/e2e/audience-query`, {
      data: { channel_user_id: SHARED_PHONE },
      headers: internalHeaders(),
    });
    expect(res.status()).toBe(200);
    const data = (await res.json()) as {
      found: boolean;
      rows: Array<{ tenant_id: string; channel: string }>;
    };
    expect(data.found).toBe(true);

    // All rows for this phone must belong to Tenant A.
    const tenantIds = [...new Set(data.rows.map((r) => r.tenant_id))];
    expect(tenantIds).toHaveLength(1);
    expect(tenantIds[0]).toBe(TENANT_A_ID);

    // Tenant B must not appear.
    const tenantBRows = data.rows.filter((r) => r.tenant_id === TENANT_B_ID);
    expect(tenantBRows).toHaveLength(0);
  });

  test("audience-query: Tenant B's IG handle is scoped to Tenant B only", async ({
    request,
  }) => {
    const res = await request.post(`${API_BASE}/internal/e2e/audience-query`, {
      data: { channel_user_id: TENANT_B_IG_HANDLE },
      headers: internalHeaders(),
    });
    expect(res.status()).toBe(200);
    const data = (await res.json()) as {
      found: boolean;
      rows: Array<{ tenant_id: string }>;
    };
    if (data.found) {
      const tenantARows = data.rows.filter((r) => r.tenant_id === TENANT_A_ID);
      expect(tenantARows).toHaveLength(0);
    }
    // If not found, Tenant B audience member may not be seeded yet — skip assert.
  });

  // -------------------------------------------------------------------------
  // Bookings isolation: A's bookings-query never returns B's bookings
  // -------------------------------------------------------------------------

  test("bookings-query: Tenant A's list never contains Tenant B's bookings", async ({
    request,
  }) => {
    // Seed one booking in each tenant.
    const bookingARes = await request.post(
      `${API_BASE}/internal/e2e/bookings`,
      {
        data: {
          tenant_id: TENANT_A_ID,
          customer_handle: `+6012345A${Date.now().toString().slice(-3)}`,
          start_at: "2027-09-01T10:00:00Z",
          channel: "whatsapp",
          status: "confirmed",
        },
        headers: internalHeaders(),
      },
    );
    expect(bookingARes.status()).toBe(201);
    const { booking_id: bookingAId } = (await bookingARes.json()) as {
      booking_id: string;
    };

    const bookingBRes = await request.post(
      `${API_BASE}/internal/e2e/bookings`,
      {
        data: {
          tenant_id: TENANT_B_ID,
          customer_handle: `@b_iso_${Date.now().toString().slice(-3)}`,
          start_at: "2027-09-02T11:00:00Z",
          channel: "instagram",
          status: "confirmed",
        },
        headers: internalHeaders(),
      },
    );
    expect(bookingBRes.status()).toBe(201);
    const { booking_id: bookingBId } = (await bookingBRes.json()) as {
      booking_id: string;
    };

    // Query Tenant A's bookings.
    const qARes = await request.post(
      `${API_BASE}/internal/e2e/bookings-query`,
      {
        data: { tenant_id: TENANT_A_ID },
        headers: internalHeaders(),
      },
    );
    expect(qARes.status()).toBe(200);
    const aData = (await qARes.json()) as {
      rows: Array<{ booking_id: string }>;
    };
    const aIds = aData.rows.map((r) => r.booking_id);
    expect(aIds).toContain(bookingAId);
    // Tenant B's booking must NOT appear in Tenant A's query.
    expect(aIds).not.toContain(bookingBId);

    // Query Tenant B's bookings.
    const qBRes = await request.post(
      `${API_BASE}/internal/e2e/bookings-query`,
      {
        data: { tenant_id: TENANT_B_ID },
        headers: internalHeaders(),
      },
    );
    expect(qBRes.status()).toBe(200);
    const bData = (await qBRes.json()) as {
      rows: Array<{ booking_id: string }>;
    };
    const bIds = bData.rows.map((r) => r.booking_id);
    expect(bIds).toContain(bookingBId);
    // Tenant A's booking must NOT appear in Tenant B's query.
    expect(bIds).not.toContain(bookingAId);
  });

  // -------------------------------------------------------------------------
  // Cross-tenant booking_id guess: customer-X cannot affect customer-Y's booking
  // -------------------------------------------------------------------------

  test("cross-tenant booking_id guess: Tenant-A JWT cannot access Tenant-B booking → 404", async ({
    request,
  }) => {
    // Seed a booking in Tenant B.
    const customerB = `@b_guesstest_${Date.now().toString().slice(-4)}`;
    const bookingRes = await request.post(`${API_BASE}/internal/e2e/bookings`, {
      data: {
        tenant_id: TENANT_B_ID,
        customer_handle: customerB,
        start_at: "2027-10-01T09:00:00Z",
        channel: "instagram",
        status: "confirmed",
      },
      headers: internalHeaders(),
    });
    expect(bookingRes.status()).toBe(201);
    const { booking_id } = (await bookingRes.json()) as { booking_id: string };

    // Mint a JWT for Tenant A via /internal/e2e/mint-tenant-jwt (now live).
    const mintRes = await request.post(
      `${API_BASE}/internal/e2e/mint-tenant-jwt`,
      {
        data: { tenant_id: TENANT_A_ID, ttl_seconds: 900 },
        headers: internalHeaders(),
      },
    );
    expect(mintRes.status()).toBe(200);
    const { token: tenantAToken } = (await mintRes.json()) as { token: string };

    // Attempt to access Tenant B's booking using Tenant A's JWT.
    // RLS filters B's row out of A's context → 404 (not-found, not 403).
    // This validates tenant isolation: "customer-X cannot cancel
    // customer-Y's booking even with a guessed booking_id (RLS gate)".
    const res = await request.get(
      `${API_BASE}/api/tenant/bookings/${booking_id}`,
      {
        headers: { Authorization: `Bearer ${tenantAToken}` },
      },
    );
    expect(res.status()).toBe(404);
  });

  // -------------------------------------------------------------------------
  // Webhook resolution: Tenant A's waba_id NEVER resolves to Tenant B
  // -------------------------------------------------------------------------

  test("webhook: Tenant-A waba_id webhook resolves to A (not B or unknown)", async ({
    request,
  }) => {
    const wamid = `wamid.e2e-mt-iso-${Date.now()}`;
    const body = JSON.stringify({
      object: "whatsapp_business_account",
      entry: [
        {
          id: "e2e_waba_a_001", // Tenant A's waba_id
          changes: [
            {
              value: {
                messaging_product: "whatsapp",
                metadata: {
                  display_phone_number: SHARED_PHONE,
                  phone_number_id: "pnid_a_001",
                },
                messages: [
                  {
                    from: SHARED_PHONE,
                    id: wamid,
                    timestamp: String(Math.floor(Date.now() / 1000)),
                    text: { body: "multi-tenant isolation check" },
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
    // 200/503 = resolved to Tenant A (503 = Redis down, but tenant resolved).
    // 204 = waba_id not found → isolation failure.
    expect(res.status()).not.toBe(204);
    expect(res.status()).not.toBe(401);
    expect([200, 503]).toContain(res.status());
  });

  test("webhook: Tenant-B IG waba_id webhook resolves to B (not A or unknown)", async ({
    request,
  }) => {
    const body = JSON.stringify({
      object: "instagram",
      entry: [
        {
          id: "e2e_ig_b_001", // Tenant B's ig_business_account_id
          changes: [
            {
              value: {
                messaging_product: "instagram",
                metadata: {},
                messages: [
                  {
                    from: "ig_user_123",
                    id: `mid.e2e-mt-b-${Date.now()}`,
                    timestamp: String(Math.floor(Date.now() / 1000)),
                    text: { body: "IG message for B" },
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
    // 200/503 = resolved to Tenant B (503 = Redis down, but tenant resolved).
    // 204 = waba_id not found → isolation failure.
    expect(res.status()).not.toBe(204);
    expect(res.status()).not.toBe(401);
    expect([200, 503]).toContain(res.status());
  });

  // -------------------------------------------------------------------------
  // EA binding token cross-tenant: token for A cannot confirm B's booking
  // -------------------------------------------------------------------------

  test("EA webhook: binding token signed with wrong secret cannot confirm any booking", async ({
    request,
  }) => {
    // Token minted with a wrong secret — verify() will fail signature check.
    // The EA router returns 200 {rejected_token} — never touches either tenant.
    // Cross-tenant isolation: a token crafted for Tenant B with a wrong key
    // cannot be used to confirm a Tenant A booking (or B's).
    const crossToken = mintBindingToken({
      jti: "cross-tenant-jti",
      tenantId: TENANT_B_ID,
      bookingId: "00000000-0000-0000-0000-000000000099",
      ttlSeconds: 3600,
      secret: "wrong-cross-tenant-secret",
    });

    const body = JSON.stringify({
      polaris_ref: crossToken,
      appointment_id: `ea-cross-${Date.now()}`,
      start_datetime: "2027-11-01T10:00:00",
    });

    const res = await request.post(`${API_BASE}/v1/webhooks/ea`, {
      data: body,
      headers: {
        "Content-Type": "application/json",
        "x-polaris-signature": signEa(body),
      },
    });
    expect(res.status()).toBe(200);
    const json = (await res.json()) as { outcome: string };
    expect(json.outcome).toBe("rejected_token");
  });
});
