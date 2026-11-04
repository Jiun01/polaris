/**
 * Shared portal E2E fixtures (rebuilt suite).
 *
 * Single source of truth for the seeded 5-tenant fixture identities + their
 * per-tier NextAuth storage states (minted by ./e2e/global-setup.ts via the
 * MailHog magic-link flow, written to ./e2e/.auth/*.json).
 *
 * Feature specs import from here and NEVER hard-code emails/slugs/UUIDs or
 * re-resolve storage-state paths. Usage:
 *
 *   import { test, expect, TENANTS, requireState } from "../_support/fixtures";
 *   const proC = TENANTS.proC;
 *   test.describe("Bookings [pro]", () => {
 *     test.skip(() => !proC.state, "pro-c storage state not minted");
 *     test.use({ storageState: requireState(proC) });
 *     test("...", async ({ page }) => { ... });
 *   });
 *
 * Storage-state resolution prefers the PLAYWRIGHT_STORAGE_STATE_* env var
 * (set by global-setup / CI) and falls back to the fixed ./e2e/.auth path,
 * so specs run identically locally and in CI. `state` is undefined when the
 * file is absent — guard every tier block with test.skip(() => !t.state).
 */

import * as fs from "fs";
import * as path from "path";

import { expect, test } from "@playwright/test";

export { expect, test };

const AUTH_DIR = path.resolve(__dirname, "../.auth");

function resolveState(envKey: string, file: string): string | undefined {
  const fromEnv = process.env[envKey];
  if (fromEnv && fs.existsSync(fromEnv)) return fromEnv;
  const fixed = path.join(AUTH_DIR, file);
  return fs.existsSync(fixed) ? fixed : undefined;
}

export type Tier = "ultra" | "starter" | "pro";

export interface TenantFixture {
  /** Stable seed UUID (apps/api/scripts/seed_e2e_multitenant.py). */
  id: string;
  slug: string;
  /** tenant_admin login email (magic-link). */
  email: string;
  tier: Tier;
  /** Resolved storage-state path, or undefined if not yet minted. */
  state: string | undefined;
  /** True for tenants seeded with >1 branch (multi_branch_enabled). */
  multiBranch: boolean;
}

/**
 * The five seeded tenants. Keys are stable; agents reference TENANTS.<key>.
 * - ultra      : Polaris owner, all channels, 1 branch (polaris-hq).
 * - starterA   : Tenant A, starter, WhatsApp only, no branch.
 * - starterB   : Tenant B, starter, Instagram only, no branch.
 * - proC       : Tenant C, pro, full-channel, 1 branch (tenant-c-hq).
 * - proD       : Tenant D, pro, multi-branch (tenant-d-hq + tenant-d-branch-2).
 */
export const TENANTS = {
  ultra: {
    id: "00000000-0000-0000-0000-000000000001",
    slug: "polaris-owner",
    email: "owner@getpolaris.my",
    tier: "ultra",
    state: resolveState("PLAYWRIGHT_STORAGE_STATE_ULTRA", "ultra.json"),
    multiBranch: false,
  },
  starterA: {
    id: "00000000-0000-0000-0000-00000000000a",
    slug: "tenant-a",
    email: "admin-a@e2e.polaris-local.dev",
    tier: "starter",
    state: resolveState("PLAYWRIGHT_STORAGE_STATE_STARTER", "starter-a.json"),
    multiBranch: false,
  },
  starterB: {
    id: "00000000-0000-0000-0000-00000000000b",
    slug: "tenant-b",
    email: "admin-b@e2e.polaris-local.dev",
    tier: "starter",
    state: resolveState("PLAYWRIGHT_STORAGE_STATE_STARTER_B", "starter-b.json"),
    multiBranch: false,
  },
  proC: {
    id: "00000000-0000-0000-0000-00000000000c",
    slug: "tenant-c",
    email: "admin-c@e2e.polaris-local.dev",
    tier: "pro",
    state: resolveState("PLAYWRIGHT_STORAGE_STATE_PRO", "pro-c.json"),
    multiBranch: false,
  },
  proD: {
    id: "00000000-0000-0000-0000-00000000000d",
    slug: "tenant-d",
    email: "admin-d@e2e.polaris-local.dev",
    tier: "pro",
    state: resolveState("PLAYWRIGHT_STORAGE_STATE_PRO_D", "pro-d.json"),
    multiBranch: true,
  },
  // payment_overdue tenant — middleware route-takeover redirects every route to
  // /pay; its session is minted by global-setup landing on /pay.
  overdue: {
    id: "00000000-0000-0000-0000-00000000000e",
    slug: "tenant-e-overdue",
    email: "admin-e@e2e.polaris-local.dev",
    tier: "starter",
    state: resolveState("PLAYWRIGHT_STORAGE_STATE_OVERDUE", "overdue.json"),
    multiBranch: false,
  },
} satisfies Record<string, TenantFixture>;

/** Narrow a possibly-undefined state to a string for test.use({ storageState }). */
export function requireState(t: TenantFixture): string {
  return t.state ?? "";
}

/** Whitelabel strings that must never appear on a customer-facing portal page. */
export const FORBIDDEN_BRAND_STRINGS = [
  "Chatwoot",
  "Easy!Appointments",
  "easyappointments",
] as const;

/** MailHog API base (magic-link interception), gated like global-setup. */
export const MAILHOG_API = process.env.MAILHOG_API_URL;
