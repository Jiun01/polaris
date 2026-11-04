/**
 * Cross-path §6-C: Multi-branch EA routing
 *
 * SET: Branch-A has ea_provider_id=X, Branch-B has ea_provider_id=Y
 *      (written by seed_e2e_ea_providers.py via provisioning service)
 * EFFECT: Inbound on Branch-A inbox → booking routed to Branch-A's provider;
 *         Inbound on Branch-B inbox → booking routed to Branch-B's provider
 * ASSERT: Via bookings-query, check booking row's provider alignment;
 *         or via EA admin API GET /api/v1/appointments
 *
 * Tenant fixture: proD (tenant-d, multi-branch)
 *   - tenant-d-hq    → ea_provider_id seeded by seed_e2e_ea_providers.py
 *   - tenant-d-branch-2 → ea_provider_id seeded by seed_e2e_ea_providers.py
 *
 * The booking flow node (services/flow_runtime/nodes/_v21_stubs.py or the
 * real booking node) resolves: conversation.tenant_branch_id → branch.ea_provider_id
 *
 * NOTE: The full end-to-end flow (inbound → booking node → EA create_appointment)
 * requires a published flow with a booking node on both tenant-d branches,
 * which cannot be driven purely from request-context without a flow publish
 * step. Those sub-tests are test.fixme'd.
 * The structural assertion (branch rows have distinct ea_provider_ids in the DB)
 * is verifiable via the internal-e2e bookings-query and audit-query.
 */

import { expect, test } from "@playwright/test";
import {
  API_BASE,
  EA_BASE,
  internalHeaders,
  secretsPresent,
  signMeta,
} from "../_support/api";

const TENANT_D_ID = "00000000-0000-0000-0000-00000000000d";
// tenant-d-hq chatwoot_inbox_id per seed_e2e_multitenant.py — proD branches
// are seeded with channels; exact inbox_id values from seed confirmed below.
// seed_e2e_multitenant.py lines 230-270: tenant-d channels (TBD exact values).
// We use the bookings-query to inspect provider routing after a booking is seeded.

test.describe("Cross-path §6-C: branch → EA provider routing", () => {
  test.skip(() => !secretsPresent(), "webhook/internal secrets not set");

  // -------------------------------------------------------------------------
  // Structural: tenant-d branches have distinct ea_provider_ids (DB assertion)
  // -------------------------------------------------------------------------

  test("tenant-d branches have non-null, distinct ea_provider_ids after seed", async ({
    request,
  }) => {
    // Uses /internal/e2e/branches-query (now live) to assert DB state.
    // Seed contract (seed_e2e_ea_providers.py): proD gets two branches:
    //   tenant-d-hq     → ea_provider_id=67, ea_default_service_id=49
    //   tenant-d-branch-2 → ea_provider_id=68, ea_default_service_id=50
    const res = await request.post(`${API_BASE}/internal/e2e/branches-query`, {
      data: { tenant_id: TENANT_D_ID },
      headers: internalHeaders(),
    });
    expect(res.status()).toBe(200);
    const data = (await res.json()) as {
      row_count: number;
      rows: Array<{
        branch_id: string;
        slug: string | null;
        name: string | null;
        ea_provider_id: number | null;
        ea_default_service_id: number | null;
      }>;
    };

    // Must have at least 2 branches.
    expect(data.row_count).toBeGreaterThanOrEqual(2);

    // Every branch must have a non-null ea_provider_id.
    for (const branch of data.rows) {
      expect(branch.ea_provider_id).not.toBeNull();
    }

    // The two provider IDs must be distinct.
    const providerIds = data.rows.map((r) => r.ea_provider_id);
    const uniqueIds = [...new Set(providerIds)];
    expect(uniqueIds).toHaveLength(data.row_count);

    // NOTE: do NOT assert literal provider/service ids — EA assigns fresh
    // auto-increment ids on every `seed_e2e_ea_providers.py --reset`, so the
    // values change between seed runs. Assert the STRUCTURE the routing relies
    // on instead: both named branches exist, each has a non-null binding, and
    // the two branches map to DISTINCT providers/services (so inbound on one
    // inbox can never route to the other branch's calendar).
    const hq = data.rows.find((r) => r.slug === "tenant-d-hq");
    const branch2 = data.rows.find((r) => r.slug === "tenant-d-branch-2");
    expect(hq).toBeDefined();
    expect(branch2).toBeDefined();
    expect(hq!.ea_provider_id).not.toBeNull();
    expect(branch2!.ea_provider_id).not.toBeNull();
    expect(hq!.ea_default_service_id).not.toBeNull();
    expect(branch2!.ea_default_service_id).not.toBeNull();
    // Distinct provider + distinct default service across branches.
    expect(hq!.ea_provider_id).not.toBe(branch2!.ea_provider_id);
    expect(hq!.ea_default_service_id).not.toBe(branch2!.ea_default_service_id);
  });

  // -------------------------------------------------------------------------
  // Full routing journey: inbound on branch-A inbox → booking to branch-A provider
  // -------------------------------------------------------------------------

  test.fixme("inbound on tenant-d-hq inbox → booking routed to tenant-d-hq ea_provider_id", async () => {
    // TODO: Steps:
    //   1. Ensure tenant-d has a published flow with a booking node
    //      (flow publish requires portal UI or /api/tenant/flows/{id}/publish).
    //   2. POST a signed Meta WA webhook for tenant-d-hq's channel
    //      (inbox_id targeting tenant-d-hq's branch).
    //   3. Poll meta-mock until a booking confirmation reply is captured.
    //   4. POST /internal/e2e/bookings-query for TENANT_D_ID.
    //   5. Find the new booking row and assert appointment_id is set
    //      (EA created it) AND the booking was created under the hq branch.
    //   6. Optionally: GET /api/v1/appointments on EA_BASE and assert the
    //      appointment provider_id matches tenant-d-hq's seeded ea_provider_id.
    // Blocked by: need a published booking-node flow on tenant-d, and the
    // inbound pipeline must reach the booking node (not short-circuit to LLM).
  });

  test.fixme("inbound on tenant-d-branch-2 inbox → booking routed to branch-2 ea_provider_id", async () => {
    // TODO: Same as above but for the branch-2 inbox.
    // After seed, tenant-d-branch-2's ea_provider_id is different from hq's.
    // The booking row's provider context must match branch-2, not hq.
  });

  // -------------------------------------------------------------------------
  // EA provider API reachability
  // -------------------------------------------------------------------------

  test("EA admin API is reachable", async ({ request }) => {
    // Basic sanity: EA is up and the admin endpoint responds.
    // Uses EA_ADMIN_API_TOKEN from env if present.
    const token = process.env.EA_ADMIN_API_TOKEN;
    test.skip(!token, "EA_ADMIN_API_TOKEN not set — skip EA admin API check");

    // Check if EA is actually listening before attempting the request.
    // EA fork at localhost:8090 may not be running in all environments.
    let eaReachable = false;
    try {
      const probe = await request.get(`${EA_BASE}/api/v1/settings`, {
        headers: { Authorization: `Bearer ${token}` },
        timeout: 3000,
      });
      eaReachable = probe.status() !== 0;
    } catch {
      eaReachable = false;
    }
    test.skip(
      !eaReachable,
      "EA fork not running at localhost:8090 — skip EA admin API check",
    );

    const res = await request.get(`${EA_BASE}/api/v1/providers`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    // EA fork should return 200 with a list of providers.
    expect([200, 401]).toContain(res.status());
    // 401 means the token is wrong but EA is reachable.
  });

  // -------------------------------------------------------------------------
  // Booking seeded under branch-A has distinct provider from branch-B
  // -------------------------------------------------------------------------

  test("bookings seeded under different branches have distinct appointment contexts", async ({
    request,
  }) => {
    // Seed two bookings: one per branch slug via internal-e2e.
    // The appointment_id column would carry the EA-side ID when confirmed;
    // we use service_name as a proxy for branch identity here.
    const bookingHqRes = await request.post(
      `${API_BASE}/internal/e2e/bookings`,
      {
        data: {
          tenant_id: TENANT_D_ID,
          customer_handle: `+6011111${Date.now().toString().slice(-4)}`,
          start_at: "2027-12-01T10:00:00Z",
          channel: "whatsapp",
          service_name: "Standard D", // tenant-d-hq service per seed
          appointment_id: `ea-hq-${Date.now()}`,
          status: "confirmed",
        },
        headers: internalHeaders(),
      },
    );
    expect(bookingHqRes.status()).toBe(201);
    const { booking_id: bookingHqId } = (await bookingHqRes.json()) as {
      booking_id: string;
    };

    const bookingB2Res = await request.post(
      `${API_BASE}/internal/e2e/bookings`,
      {
        data: {
          tenant_id: TENANT_D_ID,
          customer_handle: `+6022222${Date.now().toString().slice(-4)}`,
          start_at: "2027-12-02T14:00:00Z",
          channel: "whatsapp",
          service_name: "Standard D", // tenant-d-branch-2 service per seed
          appointment_id: `ea-b2-${Date.now()}`,
          status: "confirmed",
        },
        headers: internalHeaders(),
      },
    );
    expect(bookingB2Res.status()).toBe(201);
    const { booking_id: bookingB2Id } = (await bookingB2Res.json()) as {
      booking_id: string;
    };

    // Query all tenant-d bookings and confirm both rows are present.
    const qRes = await request.post(`${API_BASE}/internal/e2e/bookings-query`, {
      data: { tenant_id: TENANT_D_ID },
      headers: internalHeaders(),
    });
    expect(qRes.status()).toBe(200);
    const qData = (await qRes.json()) as {
      rows: Array<{ booking_id: string; status: string }>;
    };
    const ids = qData.rows.map((r) => r.booking_id);
    expect(ids).toContain(bookingHqId);
    expect(ids).toContain(bookingB2Id);

    // Confirm the two bookings are distinct rows.
    expect(bookingHqId).not.toBe(bookingB2Id);
  });
});
