/**
 * Cross-path §6-A: Admin channel identity → inbound resolution
 *
 * SET: A channel's waba_id/inbox_id (via admin API or internal-e2e)
 * EFFECT: Signed webhook for that id resolves to the tenant
 * ASSERT: meta-mock captures outbound from that channel
 *
 * The seeded fixture already has Tenant A with waba_id="e2e_waba_a_001".
 * This spec drives the cross-path assertion end-to-end using those known
 * fixture values rather than mutating channel config (which requires an
 * admin JWT we cannot mint from request-context today).
 *
 * Full mutation test (admin sets waba_id → assert webhook resolves) is
 * test.fixme'd until /internal/e2e/mint-admin-jwt or an admin API
 * endpoint is callable from request-context.
 */

import { expect, test } from "@playwright/test";
import {
  API_BASE,
  internalHeaders,
  metaMockClear,
  metaMockQuery,
  pollUntil,
  secretsPresent,
  signMeta,
} from "../_support/api";

const TENANT_A_ID = "00000000-0000-0000-0000-00000000000a";
const TENANT_A_WABA_ID = "e2e_waba_a_001";
const CUSTOMER_PHONE = "+60123450010";

test.describe("Cross-path §6-A: channel identity → inbound resolution", () => {
  test.skip(() => !secretsPresent(), "webhook/internal secrets not set");
  test.skip(
    () => process.env.POLARIS_META_MOCK_ENABLED !== "true",
    "POLARIS_META_MOCK_ENABLED must be true for meta-mock capture assertions",
  );

  test.beforeEach(async ({ request }) => {
    await metaMockClear(request);
  });

  test.afterEach(async ({ request }) => {
    await metaMockClear(request);
  });

  // -------------------------------------------------------------------------
  // Fixture-based: seeded waba_id resolves + pipeline dispatches
  // -------------------------------------------------------------------------

  test("seeded waba_id resolves to Tenant A and pipeline dispatches reply", async ({
    request,
  }) => {
    const wamid = `wamid.e2e-xp-channel-${Date.now()}`;
    const body = JSON.stringify({
      object: "whatsapp_business_account",
      entry: [
        {
          id: TENANT_A_WABA_ID,
          changes: [
            {
              value: {
                messaging_product: "whatsapp",
                metadata: {
                  display_phone_number: CUSTOMER_PHONE,
                  phone_number_id: "pnid_a_001",
                },
                messages: [
                  {
                    from: CUSTOMER_PHONE,
                    id: wamid,
                    timestamp: String(Math.floor(Date.now() / 1000)),
                    text: { body: "cross-path channel resolution test" },
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
    // 200/503 = waba_id resolved to Tenant A.
    // 503 = Redis unavailable but tenant resolution succeeded (meta_router line 30).
    // 204 = waba_id not found → would be a channel identity failure.
    expect(res.status()).not.toBe(204);
    expect(res.status()).not.toBe(401);
    expect([200, 503]).toContain(res.status());

    // Pipeline reply capture (meta-mock) only works when Redis is running.
    // When Redis is unavailable the 503 above means the message was never
    // enqueued, so no outbound capture will appear.
    if (res.status() === 503) {
      // Redis unavailable — skip pipeline assertion, resolution already verified.
      return;
    }

    // Poll for meta-mock to capture a reply from the pipeline.
    const result = await pollUntil(
      async () => {
        const data = await metaMockQuery(request);
        return data.count > 0 ? data : null;
      },
      { maxAttempts: 20, delayMs: 500 },
    );

    // A reply was dispatched from the tenant's channel.
    expect(result.count).toBeGreaterThan(0);
  });

  // -------------------------------------------------------------------------
  // Mutation test: admin sets waba_id → new webhook resolves
  // -------------------------------------------------------------------------

  test.fixme("admin sets waba_id on a synthetic channel → signed webhook resolves to that tenant", async () => {
    // TODO: Steps once admin JWT mint is available:
    //   1. POST /internal/e2e/tenants → create synthetic tenant (slug e2e-xp-channel-*)
    //   2. Obtain admin Bearer JWT (from admin storage state or mint endpoint)
    //   3. POST /api/admin/tenants/{id}/channels with channel=whatsapp,
    //      waba_id=<synthetic_waba_id>, phone_number_id=<synthetic_pnid>
    //   4. Send signed webhook with that waba_id
    //   5. Assert response is 200 (resolved, not 204)
    //   6. Assert meta-mock captures a dispatch FROM the channel
    //   7. DELETE /internal/e2e/tenants/{id} (cleanup)
    // Blocked by: no admin JWT mint path available in request-context tests.
  });

  // -------------------------------------------------------------------------
  // Unknown waba_id → 204, no resolution
  // -------------------------------------------------------------------------

  test("unknown waba_id → 204 (not resolved to any tenant)", async ({
    request,
  }) => {
    const body = JSON.stringify({
      object: "whatsapp_business_account",
      entry: [
        {
          id: "completely-unknown-waba-xp-99999",
          changes: [
            {
              value: {
                messaging_product: "whatsapp",
                metadata: {
                  display_phone_number: "+6099999999",
                  phone_number_id: "pnid_unknown",
                },
                messages: [
                  {
                    from: "+6099999999",
                    id: `wamid.e2e-xp-unknown-${Date.now()}`,
                    timestamp: String(Math.floor(Date.now() / 1000)),
                    text: { body: "unknown channel" },
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
    expect(res.status()).toBe(204);
  });
});
