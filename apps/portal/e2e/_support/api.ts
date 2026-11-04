/**
 * Shared API helpers for the request-context E2E specs.
 *
 * Owned by: api/ + journeys/ + cross-path/ agent.
 * DO NOT import from _support/fixtures.ts (circular dep risk) — import
 * directly from "@playwright/test" in each spec.
 *
 * Contract facts confirmed from source:
 *   - Meta webhook header  : "x-hub-signature-256" value "sha256=<hexdigest>"
 *     (meta_router.py line 83: _META_SIGNATURE_HEADER = "x-hub-signature-256")
 *   - Chatwoot header      : "x-chatwoot-signature" value "<hexdigest>"  (bare, no prefix)
 *     (_hmac.py verify_chatwoot_signature — no prefix strip)
 *   - EA webhook header    : "x-polaris-signature"  value "sha256=<hexdigest>"
 *     (ea_router.py _verify_signature: startsWith("sha256="))
 *   - Internal-e2e auth    : Authorization: Bearer <POLARIS_E2E_INTERNAL_SECRET>
 *     (internal_e2e.py _require_e2e_auth)
 *   - Binding token format : v1.<b64url(payload)>.<b64url(hmac-sha256(payload_b64))>
 *     (binding_token.py mint())
 *
 * Secret env vars used (loaded by playwright.config.ts from root .env):
 *   POLARIS_E2E_INTERNAL_SECRET   — internal-e2e Bearer gate
 *   META_APP_SECRET               — Meta HMAC key (platform-level)
 *   CHATWOOT_WEBHOOK_HMAC_SECRET  — Chatwoot HMAC key
 *   EA_WEBHOOK_HMAC_SECRET        — EA webhook HMAC key
 *   BOOKING_BINDING_SECRET        — binding-token signing key
 *   POLARIS_META_MOCK_ENABLED     — must be "true" for meta-mock to be active
 */

import * as crypto from "crypto";
import type { APIRequestContext } from "@playwright/test";

// ---------------------------------------------------------------------------
// Base URLs
// ---------------------------------------------------------------------------

export const API_BASE = "http://localhost:8000";
export const EA_BASE = "http://localhost:8090";

// ---------------------------------------------------------------------------
// Secrets guard
// ---------------------------------------------------------------------------

/**
 * Returns true only when ALL secrets required by the webhook/internal specs
 * are present in the environment. Use as the `test.skip` condition:
 *
 *   test.skip(() => !secretsPresent(), "webhook/internal secrets not set");
 */
export function secretsPresent(): boolean {
  return !!(
    process.env.POLARIS_E2E_INTERNAL_SECRET &&
    process.env.META_APP_SECRET &&
    process.env.CHATWOOT_WEBHOOK_HMAC_SECRET &&
    process.env.EA_WEBHOOK_HMAC_SECRET &&
    process.env.BOOKING_BINDING_SECRET
  );
}

// ---------------------------------------------------------------------------
// HMAC signers
// ---------------------------------------------------------------------------

/**
 * Sign a raw body for Meta webhooks.
 * Header value: "sha256=<hexdigest>"
 * Header name (lowercase): "x-hub-signature-256"
 */
export function signMeta(body: Buffer | string): string {
  const secret = process.env.META_APP_SECRET ?? "";
  const raw = typeof body === "string" ? Buffer.from(body, "utf8") : body;
  const hex = crypto.createHmac("sha256", secret).update(raw).digest("hex");
  return `sha256=${hex}`;
}

/**
 * Sign a raw body for Chatwoot webhooks.
 * Header value: "<hexdigest>"  (no "sha256=" prefix — bare hex)
 * Header name (lowercase): "x-chatwoot-signature"
 */
export function signChatwoot(body: Buffer | string): string {
  const secret = process.env.CHATWOOT_WEBHOOK_HMAC_SECRET ?? "";
  const raw = typeof body === "string" ? Buffer.from(body, "utf8") : body;
  return crypto.createHmac("sha256", secret).update(raw).digest("hex");
}

/**
 * Sign a raw body for EA webhooks.
 * Header value: "sha256=<hexdigest>"
 * Header name (lowercase): "x-polaris-signature"
 */
export function signEa(body: Buffer | string): string {
  const secret = process.env.EA_WEBHOOK_HMAC_SECRET ?? "";
  const raw = typeof body === "string" ? Buffer.from(body, "utf8") : body;
  const hex = crypto.createHmac("sha256", secret).update(raw).digest("hex");
  return `sha256=${hex}`;
}

// ---------------------------------------------------------------------------
// Internal-e2e auth headers
// ---------------------------------------------------------------------------

/**
 * Returns headers required for internal-e2e endpoints.
 * All internal-e2e routes require:  Authorization: Bearer <secret>
 * (internal_e2e.py _require_e2e_auth)
 */
export function internalHeaders(): Record<string, string> {
  return {
    Authorization: `Bearer ${process.env.POLARIS_E2E_INTERNAL_SECRET ?? ""}`,
    "Content-Type": "application/json",
  };
}

// ---------------------------------------------------------------------------
// Binding token (mirrors binding_token.py mint())
// ---------------------------------------------------------------------------

function b64urlEncode(buf: Buffer): string {
  return buf.toString("base64url");
}

/**
 * Mint an HMAC binding token that mirrors binding_token.py mint().
 *
 * Format: v1.<b64url(json-payload)>.<b64url(hmac-sha256(payload_b64))>
 * Payload keys (sorted, compact): bid, exp, jti, tid
 * (Python: json.dumps(sort_keys=True, separators=(',', ':')))
 */
export function mintBindingToken(opts: {
  jti: string;
  tenantId: string;
  bookingId: string;
  ttlSeconds?: number;
  nowSeconds?: number;
  secret?: string;
}): string {
  const {
    jti,
    tenantId,
    bookingId,
    ttlSeconds = 3600,
    nowSeconds = Math.floor(Date.now() / 1000),
    secret = process.env.BOOKING_BINDING_SECRET ?? "",
  } = opts;

  // Keys must be sorted to match Python's sort_keys=True.
  const payload = {
    bid: bookingId,
    exp: nowSeconds + ttlSeconds,
    jti,
    tid: tenantId,
  };
  // Compact separators match Python's separators=(',', ':').
  const payloadJson = JSON.stringify(payload);
  const payloadB64 = b64urlEncode(Buffer.from(payloadJson, "utf8"));

  const sig = crypto
    .createHmac("sha256", secret)
    .update(payloadB64, "ascii")
    .digest();
  const sigB64 = b64urlEncode(sig);

  return `v1.${payloadB64}.${sigB64}`;
}

// ---------------------------------------------------------------------------
// Meta-mock helpers (use Playwright APIRequestContext directly)
// ---------------------------------------------------------------------------

export interface MetaCapture {
  path: string;
  body: unknown;
}

export interface MetaOutboundResult {
  count: number;
  captures: MetaCapture[];
}

/**
 * Query all calls captured by the Meta Graph API mock.
 * Uses GET /internal/e2e/meta-outbound-query (requires internal auth).
 */
export async function metaMockQuery(
  request: APIRequestContext,
): Promise<MetaOutboundResult> {
  const res = await request.get(
    `${API_BASE}/internal/e2e/meta-outbound-query`,
    { headers: internalHeaders() },
  );
  if (!res.ok()) {
    throw new Error(
      `metaMockQuery failed: ${res.status()} ${await res.text()}`,
    );
  }
  return res.json() as Promise<MetaOutboundResult>;
}

/**
 * Clear all captured calls in the Meta mock store.
 * Uses DELETE /internal/e2e/meta-outbound-clear (requires internal auth).
 */
export async function metaMockClear(
  request: APIRequestContext,
): Promise<number> {
  const res = await request.delete(
    `${API_BASE}/internal/e2e/meta-outbound-clear`,
    { headers: internalHeaders() },
  );
  if (!res.ok()) {
    throw new Error(
      `metaMockClear failed: ${res.status()} ${await res.text()}`,
    );
  }
  const data = (await res.json()) as { cleared: number };
  return data.cleared;
}

// ---------------------------------------------------------------------------
// Polling helper (no waitForTimeout)
// ---------------------------------------------------------------------------

/**
 * Poll a predicate until it returns truthy or the retry budget is exhausted.
 * Retries up to `maxAttempts` times with `delayMs` between each attempt.
 */
export async function pollUntil<T>(
  fn: () => Promise<T | null | undefined | false>,
  {
    maxAttempts = 10,
    delayMs = 500,
  }: { maxAttempts?: number; delayMs?: number } = {},
): Promise<T> {
  for (let i = 0; i < maxAttempts; i++) {
    const result = await fn();
    if (result) return result as T;
    if (i < maxAttempts - 1) {
      await new Promise<void>((r) => setTimeout(r, delayMs));
    }
  }
  throw new Error(`pollUntil: condition not met after ${maxAttempts} attempts`);
}
