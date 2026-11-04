/**
 * Booking-link journey
 *
 * Covers:
 *   - Link-mode booking: seed a pending booking + mint binding token
 *   - EA webhook confirm → booking flips to confirmed
 *   - Idempotent replay of the EA webhook → already_confirmed
 *   - Cancel/reschedule identity isolation keyed on verified channel_user_id:
 *     customer-X cannot cancel customer-Y's booking (cross-channel_user_id guard)
 *
 * Uses:
 *   - POST /internal/e2e/bookings          (seed pending booking)
 *   - POST /internal/e2e/bookings-query    (assert booking status)
 *   - POST /v1/webhooks/ea                 (EA webhook confirm)
 *   - mintBindingToken()                   (mirrors binding_token.py)
 *
 * Contract confirmed from ea_router.py:
 *   - valid HMAC + valid unexpired token + valid start_datetime → confirm
 *   - already confirmed on re-delivery → {"outcome":"already_confirmed"}
 *   - expired/bad token → {"outcome":"rejected_token"}
 *   - missing start_datetime → 422
 */

import { expect, test } from "@playwright/test";
import crypto from "crypto";
import {
  API_BASE,
  internalHeaders,
  mintBindingToken,
  pollUntil,
  secretsPresent,
  signEa,
} from "../_support/api";

const TENANT_A_ID = "00000000-0000-0000-0000-00000000000a";
const TENANT_B_ID = "00000000-0000-0000-0000-00000000000b";

test.describe("Journey: booking-link + EA webhook confirm", () => {
  test.skip(() => !secretsPresent(), "webhook/internal secrets not set");

  // -------------------------------------------------------------------------
  // Happy path: seed pending → EA confirm → confirmed
  // -------------------------------------------------------------------------

  test("EA webhook confirm: pending booking → confirmed", async ({
    request,
  }) => {
    // 1. Seed a pending booking via internal-e2e with binding_token=jti.
    //    The /internal/e2e/bookings endpoint now accepts binding_token (the jti
    //    stored in bookings.binding_token — the EA webhook matches on this).
    const customerHandle = `+6012345${Date.now().toString().slice(-4)}`;
    const jti = crypto.randomUUID();

    const bookingRes = await request.post(`${API_BASE}/internal/e2e/bookings`, {
      data: {
        tenant_id: TENANT_A_ID,
        customer_handle: customerHandle,
        start_at: "2027-06-15T10:00:00Z",
        channel: "whatsapp",
        service_name: "E2E Test Service",
        status: "pending",
        binding_token: jti, // stored in bookings.binding_token
        binding_ttl_seconds: 3600,
      },
      headers: internalHeaders(),
    });
    expect(bookingRes.status()).toBe(201);
    const { booking_id } = (await bookingRes.json()) as {
      booking_id: string;
      reminder_id: string | null;
    };

    // 2. Mint a polaris_ref binding token carrying the same jti.
    //    The EA webhook verify() extracts jti and calls confirm_booking(jti).
    const polarisRef = mintBindingToken({
      jti,
      tenantId: TENANT_A_ID,
      bookingId: booking_id,
      ttlSeconds: 3600,
    });

    // 3. Send the EA webhook — valid HMAC + valid unexpired token + start_datetime.
    const eaPayload = JSON.stringify({
      polaris_ref: polarisRef,
      appointment_id: `ea-appt-${Date.now()}`,
      start_datetime: "2027-06-15T10:00:00",
    });
    const eaRes = await request.post(`${API_BASE}/v1/webhooks/ea`, {
      data: eaPayload,
      headers: {
        "Content-Type": "application/json",
        "x-polaris-signature": signEa(eaPayload),
      },
    });
    expect(eaRes.status()).toBe(200);
    const outcome = (await eaRes.json()) as { outcome: string };
    expect(outcome.outcome).toBe("confirmed");

    // 4. Verify booking status flipped to confirmed via bookings-query.
    const bookingsQueryRes = await request.post(
      `${API_BASE}/internal/e2e/bookings-query`,
      {
        data: { tenant_id: TENANT_A_ID },
        headers: internalHeaders(),
      },
    );
    expect(bookingsQueryRes.status()).toBe(200);
    const bqData = (await bookingsQueryRes.json()) as {
      rows: Array<{ booking_id: string; status: string }>;
    };
    const row = bqData.rows.find((r) => r.booking_id === booking_id);
    expect(row).toBeDefined();
    expect(row!.status).toBe("confirmed");
  });

  // -------------------------------------------------------------------------
  // Idempotent replay → already_confirmed
  // -------------------------------------------------------------------------

  test("EA webhook replay on already-confirmed booking → already_confirmed", async ({
    request,
  }) => {
    // Seed a PENDING booking with binding_token=jti so confirm_booking can find it.
    const customerHandle = `+6012999${Date.now().toString().slice(-4)}`;
    const jti = crypto.randomUUID();

    const bookingRes = await request.post(`${API_BASE}/internal/e2e/bookings`, {
      data: {
        tenant_id: TENANT_A_ID,
        customer_handle: customerHandle,
        start_at: "2027-07-10T14:00:00Z",
        channel: "whatsapp",
        status: "pending",
        binding_token: jti,
        binding_ttl_seconds: 3600,
      },
      headers: internalHeaders(),
    });
    expect(bookingRes.status()).toBe(201);
    const { booking_id } = (await bookingRes.json()) as { booking_id: string };

    const polarisRef = mintBindingToken({
      jti,
      tenantId: TENANT_A_ID,
      bookingId: booking_id,
      ttlSeconds: 3600,
    });

    const eaPayload = JSON.stringify({
      polaris_ref: polarisRef,
      appointment_id: `ea-replay-${Date.now()}`,
      start_datetime: "2027-07-10T14:00:00",
    });
    const headers = {
      "Content-Type": "application/json",
      "x-polaris-signature": signEa(eaPayload),
    };

    // First delivery → confirmed.
    const first = await request.post(`${API_BASE}/v1/webhooks/ea`, {
      data: eaPayload,
      headers,
    });
    expect(first.status()).toBe(200);
    expect(((await first.json()) as { outcome: string }).outcome).toBe(
      "confirmed",
    );

    // Replay (same polaris_ref + appointment_id) → already_confirmed (idempotent).
    const second = await request.post(`${API_BASE}/v1/webhooks/ea`, {
      data: eaPayload,
      headers,
    });
    expect(second.status()).toBe(200);
    expect(((await second.json()) as { outcome: string }).outcome).toBe(
      "already_confirmed",
    );
  });

  // -------------------------------------------------------------------------
  // Cancel identity isolation: customer-X cannot cancel customer-Y's booking
  // -------------------------------------------------------------------------

  test("cancel: cross-channel_user_id guard — customer-X cannot cancel customer-Y booking", async ({
    request,
  }) => {
    // Seed a confirmed booking for customer-Y in Tenant A.
    const customerY = `+6011111${Date.now().toString().slice(-4)}`;
    const bookingRes = await request.post(`${API_BASE}/internal/e2e/bookings`, {
      data: {
        tenant_id: TENANT_A_ID,
        customer_handle: customerY,
        start_at: "2027-08-01T09:00:00Z",
        channel: "whatsapp",
        status: "confirmed",
      },
      headers: internalHeaders(),
    });
    expect(bookingRes.status()).toBe(201);
    const { booking_id } = (await bookingRes.json()) as { booking_id: string };

    // customer-X is a DIFFERENT phone number (distinct channel_user_id).
    // The cancel/reschedule endpoint should gate on channel_user_id ownership.
    // TODO: once a public cancel endpoint exists at /api/tenant/bookings/cancel
    // or similar, call it with a JWT for customer-X and assert 403/404.
    // The ownership check:
    // "cancel/reschedule identity isolation (keyed on verified channel_user_id)".
    test.fixme(
      true,
      `TODO: No public customer-facing cancel endpoint has been confirmed from source. ` +
        `Inspect tenant_bookings.py for a cancel/reschedule route, confirm it gates on ` +
        `channel_user_id, then: ` +
        `1. Obtain a JWT for customer-X (phone different from ${customerY}) ` +
        `2. Attempt to cancel booking_id=${booking_id} (owned by ${customerY}) ` +
        `3. Assert 403 or 404 (the booking does not appear in X's identity scope). ` +
        `booking_id seeded: ${booking_id}`,
    );
  });
});
