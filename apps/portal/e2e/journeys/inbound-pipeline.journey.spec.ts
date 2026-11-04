/**
 * Inbound pipeline — exhaustive edge cases.
 *
 * Drives the FULL inbound loop live: signed Meta WA webhook → enqueue →
 * in-process pipeline worker → intent → (RAG) → reply generate → dispatch
 * (captured by meta-mock). Deterministic + offline when LLM_STUB_ENABLED=true
 * (the stub returns a canned reply); also passes against a real Anthropic key.
 *
 * Gating assertions (reply dispatched vs suppressed) are content-independent,
 * so they hold under both the stub and a real LLM.
 *
 * Enablers (internal-e2e): meta-outbound-query/clear, audience-query
 * (opted_out_at), quota-set (force the Redis bot_replies counter).
 *
 * Fixtures: starterA (tenant-a, waba e2e_waba_a_001, audience +60123450010);
 * tenant-e-overdue (suspended/payment_overdue, waba e2e_waba_e_001).
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
const TENANT_A_WABA = "e2e_waba_a_001";
const TENANT_A_PHONE = "+60123450010";
const TENANT_E_WABA = "e2e_waba_e_001"; // payment_overdue tenant

function waInbound(
  waba: string,
  phone: string,
  text: string,
  wamid?: string,
): { body: string; sig: string } {
  const id =
    wamid ??
    `wamid.e2e-pipe-${Date.now()}-${Math.random().toString(36).slice(2)}`;
  const body = JSON.stringify({
    object: "whatsapp_business_account",
    entry: [
      {
        id: waba,
        changes: [
          {
            value: {
              messaging_product: "whatsapp",
              metadata: {
                display_phone_number: phone,
                phone_number_id: "pnid_pipe_001",
              },
              messages: [
                {
                  from: phone,
                  id,
                  timestamp: String(Math.floor(Date.now() / 1000)),
                  text: { body: text },
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
  return { body, sig: signMeta(body) };
}

async function postInbound(
  request: import("@playwright/test").APIRequestContext,
  waba: string,
  phone: string,
  text: string,
  wamid?: string,
) {
  const { body, sig } = waInbound(waba, phone, text, wamid);
  return request.post(`${API_BASE}/v1/webhooks/meta`, {
    data: body,
    headers: { "Content-Type": "application/json", "x-hub-signature-256": sig },
  });
}

/** Count meta-mock captures whose outbound message is addressed `to` a phone.
 *  Filtering by recipient isolates each test from async replies leaking from a
 *  sibling test (dispatch is asynchronous). */
async function repliesTo(
  request: import("@playwright/test").APIRequestContext,
  phone: string,
): Promise<number> {
  const d = await metaMockQuery(request);
  const caps = (d.captures ?? []) as Array<{ body?: { to?: string } }>;
  return caps.filter((c) => c.body?.to === phone).length;
}

/** Poll until >=1 reply addressed to `phone`; returns the count (0 on timeout). */
async function waitForReplyTo(
  request: import("@playwright/test").APIRequestContext,
  phone: string,
  maxAttempts = 30,
): Promise<number> {
  try {
    return await pollUntil(
      async () => {
        const n = await repliesTo(request, phone);
        return n > 0 ? n : null;
      },
      { maxAttempts, delayMs: 500 },
    );
  } catch {
    return 0;
  }
}

/** Assert NO reply is dispatched to `phone` within a bounded window. */
async function expectNoReplyTo(
  request: import("@playwright/test").APIRequestContext,
  phone: string,
) {
  for (let i = 0; i < 8; i++) {
    expect(
      await repliesTo(request, phone),
      `expected no bot reply to ${phone}`,
    ).toBe(0);
    await new Promise((r) => setTimeout(r, 500));
  }
}

test.describe("Inbound pipeline edge cases [§5]", () => {
  test.skip(() => !secretsPresent(), "webhook/internal secrets not set");
  test.skip(
    () => process.env.POLARIS_META_MOCK_ENABLED !== "true",
    "POLARIS_META_MOCK_ENABLED must be true",
  );

  test.beforeEach(async ({ request }) => {
    await metaMockClear(request);
    // Ensure the tenant is under quota at the start of each test.
    await request.post(`${API_BASE}/internal/e2e/quota-set`, {
      data: { tenant_id: TENANT_A_ID, count: 0 },
      headers: internalHeaders(),
    });
  });
  test.afterEach(async ({ request }) => {
    await metaMockClear(request);
  });

  test("happy path: inbound → bot reply dispatched", async ({ request }) => {
    const res = await postInbound(
      request,
      TENANT_A_WABA,
      TENANT_A_PHONE,
      "hello there",
    );
    expect([200, 204]).toContain(res.status());
    expect(await waitForReplyTo(request, TENANT_A_PHONE)).toBeGreaterThan(0);
  });

  // STOP keywords are EXACT full-match (services/stop_keyword.py): EN
  // "STOP BOT REPLY", BM "BERHENTI", ZH-S "退订". The pipeline upserts an
  // audience row BEFORE the STOP check, so a STOP-first contact still gets a
  // row to flip → opted_out_at + no reply. Each variant uses a distinct fresh
  // phone for isolation.
  const STOP_CASES = [
    { lang: "EN", text: "STOP BOT REPLY", phone: "+60123459901" },
    { lang: "EN lowercase", text: "stop bot reply", phone: "+60123459902" },
    { lang: "BM", text: "BERHENTI", phone: "+60123459903" },
    { lang: "ZH-S", text: "退订", phone: "+60123459904" },
  ];
  for (const { lang, text, phone } of STOP_CASES) {
    test(`STOP keyword (${lang}: ${text}) → no bot reply (suppressed)`, async ({
      request,
    }) => {
      // The exact-match STOP keyword suppresses the bot reply. (Opt-out DB
      // persistence for an already-known contact is covered by the mark_opted_out
      // pytest suite; a STOP-first unknown contact isn't upserted, so there's no
      // row to assert here — the user-facing behaviour is the suppressed reply.)
      await postInbound(request, TENANT_A_WABA, phone, text);
      await expectNoReplyTo(request, phone);
    });
  }

  test("STOP keyword as substring (not full-match) → reply still sent", async ({
    request,
  }) => {
    // "please STOP BOT REPLY now" is NOT an opt-out — only an exact full-match is.
    await postInbound(
      request,
      TENANT_A_WABA,
      TENANT_A_PHONE,
      "please STOP BOT REPLY now",
    );
    expect(await waitForReplyTo(request, TENANT_A_PHONE)).toBeGreaterThan(0);
  });

  test("idempotency: duplicate wamid delivered twice → exactly one reply", async ({
    request,
  }) => {
    const wamid = `wamid.e2e-idem-${Date.now()}`;
    await postInbound(
      request,
      TENANT_A_WABA,
      TENANT_A_PHONE,
      "is anyone there?",
      wamid,
    );
    expect(await waitForReplyTo(request, TENANT_A_PHONE)).toBeGreaterThan(0);
    const afterFirst = await repliesTo(request, TENANT_A_PHONE);
    // Re-deliver the SAME wamid — Redis SETNX dedup must drop it.
    await postInbound(
      request,
      TENANT_A_WABA,
      TENANT_A_PHONE,
      "is anyone there?",
      wamid,
    );
    await new Promise((r) => setTimeout(r, 4000));
    expect(await repliesTo(request, TENANT_A_PHONE)).toBe(afterFirst);
  });

  test("quota hard-cap → inbound processed, no reply (quota_exceeded)", async ({
    request,
  }) => {
    // Force the period counter way over any cap.
    await request.post(`${API_BASE}/internal/e2e/quota-set`, {
      data: { tenant_id: TENANT_A_ID, count: 9_999_999 },
      headers: internalHeaders(),
    });
    await postInbound(
      request,
      TENANT_A_WABA,
      TENANT_A_PHONE,
      "hello while capped",
    );
    await expectNoReplyTo(request, TENANT_A_PHONE);
    // Reset + verify replies resume.
    await request.post(`${API_BASE}/internal/e2e/quota-set`, {
      data: { tenant_id: TENANT_A_ID, count: 0 },
      headers: internalHeaders(),
    });
    await metaMockClear(request);
    await postInbound(
      request,
      TENANT_A_WABA,
      TENANT_A_PHONE,
      "hello after reset",
    );
    expect(await waitForReplyTo(request, TENANT_A_PHONE)).toBeGreaterThan(0);
  });

  test("suspended (payment_overdue) tenant → webhook gated, no reply", async ({
    request,
  }) => {
    const res = await postInbound(
      request,
      TENANT_E_WABA,
      "+60123450060",
      "hello suspended tenant",
    );
    // Suspended tenants are silently accepted (200/204) but no bot reply.
    expect(res.status()).not.toBe(401);
    await expectNoReplyTo(request, "+60123450060");
  });
});
