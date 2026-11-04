/**
 * Shared admin E2E fixtures (rebuilt suite).
 *
 * Centralizes the platform_admin storage state (minted by ./e2e/global-setup.ts
 * via MailHog magic-link → ./e2e/.auth/admin.json) and the seeded tenant UUIDs
 * the admin specs drive against (/tenants/[id], /cost/[id], /secrets/[id], …).
 *
 * Usage:
 *   import { test, expect, ADMIN_STATE, TENANT_IDS } from "../_support/fixtures";
 *   test.skip(() => !ADMIN_STATE, "admin storage state not minted");
 *   test.use({ storageState: ADMIN_STATE ?? "" });
 */

import * as fs from "fs";
import * as path from "path";

import { expect, test } from "@playwright/test";

export { expect, test };

const AUTH_DIR = path.resolve(__dirname, "../.auth");

function resolveAdminState(): string | undefined {
  const fromEnv = process.env.PLAYWRIGHT_ADMIN_STORAGE_STATE;
  if (fromEnv && fs.existsSync(fromEnv)) return fromEnv;
  const fixed = path.join(AUTH_DIR, "admin.json");
  return fs.existsSync(fixed) ? fixed : undefined;
}

/** Resolved platform_admin storage-state path, or undefined if not minted. */
export const ADMIN_STATE = resolveAdminState();

/** Seeded tenant UUIDs (apps/api/scripts/seed_e2e_multitenant.py). */
export const TENANT_IDS = {
  ultra: "00000000-0000-0000-0000-000000000001",
  starterA: "00000000-0000-0000-0000-00000000000a",
  starterB: "00000000-0000-0000-0000-00000000000b",
  proC: "00000000-0000-0000-0000-00000000000c",
  proD: "00000000-0000-0000-0000-00000000000d",
} as const;

export const TENANT_SLUGS = {
  ultra: "polaris-owner",
  starterA: "tenant-a",
  starterB: "tenant-b",
  proC: "tenant-c",
  proD: "tenant-d",
} as const;

/** Seeded platform_admin login (magic-link). */
export const ADMIN_EMAIL =
  process.env.E2E_ADMIN_EMAIL ?? "e2e-admin@polaris-local.dev";

/** Whitelabel strings that must never leak into admin-rendered tenant views/PDFs. */
export const FORBIDDEN_BRAND_STRINGS = [
  "Chatwoot",
  "Easy!Appointments",
  "easyappointments",
] as const;
