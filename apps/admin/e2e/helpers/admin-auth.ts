/**
 * Synthetic platform_admin auth fixture for admin E2E (Phase 9 PR-12).
 *
 * Usage:
 *   import { ADMIN_STATE } from "./helpers/admin-auth";
 *   test.use({ storageState: ADMIN_STATE ?? "" });
 *
 * PLAYWRIGHT_ADMIN_STORAGE_STATE must point to a pre-generated Next.js
 * cookies JSON file (session cookie from a seeded platform_admin row).
 * In CI the setup step writes this file before running specs.
 */

export const ADMIN_STATE = process.env.PLAYWRIGHT_ADMIN_STORAGE_STATE;
