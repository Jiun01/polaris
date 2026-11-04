/**
 * DB seed helpers for admin E2E (Phase 9 PR-12).
 *
 * Thin wrappers that call the Polaris API's internal seed/teardown
 * endpoints (gated behind POLARIS_E2E_INTERNAL_SECRET). Keeping DB
 * operations in the API layer avoids a direct Postgres dependency from
 * the Playwright runner and re-uses the API's asyncpg pool.
 *
 * POLARIS_API_INTERNAL_URL: base URL for internal seed calls.
 * POLARIS_E2E_INTERNAL_SECRET: shared bearer token accepted only by
 *   the /internal/e2e/* router (mounted only when ENVIRONMENT=test).
 */

import type { APIRequestContext } from "@playwright/test";

const API_BASE =
  process.env.POLARIS_E2E_API_BASE_URL ??
  process.env.POLARIS_API_BASE_URL ??
  "http://localhost:8000";

const SECRET = process.env.POLARIS_E2E_INTERNAL_SECRET ?? "";

async function seedPost(
  request: APIRequestContext,
  path: string,
  body: unknown,
): Promise<unknown> {
  const res = await request.post(`${API_BASE}${path}`, {
    data: body,
    headers: { Authorization: `Bearer ${SECRET}` },
  });
  if (!res.ok()) {
    throw new Error(
      `E2E seed POST ${path} failed: ${res.status()} ${await res.text()}`,
    );
  }
  return res.json();
}

async function seedDelete(
  request: APIRequestContext,
  path: string,
): Promise<void> {
  const res = await request.delete(`${API_BASE}${path}`, {
    headers: { Authorization: `Bearer ${SECRET}` },
  });
  // 404 is acceptable in finally-block teardown — the row may never have been
  // created if the test failed before the seed completed.
  if (!res.ok() && res.status() !== 404) {
    throw new Error(
      `E2E seed DELETE ${path} failed: ${res.status()} ${await res.text()}`,
    );
  }
}

export interface SyntheticTenant {
  tenant_id: string;
  tenant_slug: string;
  tenant_admin_email: string;
  tenant_admin_user_id: string;
}

/** Seed a synthetic active tenant and return its IDs. */
export async function seedTenant(
  request: APIRequestContext,
  overrides: { slug?: string; tier?: string } = {},
): Promise<SyntheticTenant> {
  return (await seedPost(
    request,
    "/internal/e2e/tenants",
    overrides,
  )) as SyntheticTenant;
}

/** Hard-delete a synthetic tenant by ID (cleanup). */
export async function teardownTenant(
  request: APIRequestContext,
  tenantId: string,
): Promise<void> {
  await seedDelete(request, `/internal/e2e/tenants/${tenantId}`);
}

export interface MrrSeed {
  mrr_myr: number;
  alert_band: "green" | "amber" | "red";
}

/** Seed an MRR rollup row directly (bypasses the scheduler). */
export async function seedMrr(
  request: APIRequestContext,
  mrrMyr: number,
): Promise<MrrSeed> {
  return (await seedPost(request, "/internal/e2e/mrr-rollup", {
    mrr_myr: mrrMyr,
  })) as MrrSeed;
}

export interface PlanChangeRequestSeed {
  request_id: string;
}

/** Seed a pending plan-change request for a tenant. */
export async function seedPlanChangeRequest(
  request: APIRequestContext,
  tenantId: string,
  targetTier: "starter" | "pro" | "ultra_plus",
): Promise<PlanChangeRequestSeed> {
  return (await seedPost(request, "/internal/e2e/plan-change-requests", {
    tenant_id: tenantId,
    target_tier: targetTier,
  })) as PlanChangeRequestSeed;
}

/** Corrupt an audit_log row_hash to simulate chain break. */
export async function corruptAuditHash(
  request: APIRequestContext,
  tenantId: string,
): Promise<void> {
  // The endpoint returns 204 No Content — use a raw request instead of
  // seedPost which calls res.json() and would throw on an empty body.
  const res = await request.post(
    `${API_BASE}/internal/e2e/audit-chain-corrupt`,
    {
      data: { tenant_id: tenantId },
      headers: { Authorization: `Bearer ${SECRET}` },
    },
  );
  if (!res.ok()) {
    throw new Error(
      `E2E audit-chain-corrupt failed: ${res.status()} ${await res.text()}`,
    );
  }
}
