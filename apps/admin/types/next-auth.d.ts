/**
 * NextAuth module augmentation for the admin app (Phase 9 PR-2).
 *
 * Adds the single Polaris-admin-specific claim (`platform_admin_id`) to
 * the JWT and Session shapes. Mirrors apps/portal/types/next-auth.d.ts
 * but the admin claim set is much smaller — there's no tenant_id /
 * plan_tier / suspended_reason concept here; the platform_admin
 * operates above tenants.
 */

import "next-auth";
import "next-auth/jwt";

declare module "next-auth" {
  interface Session {
    user: {
      id: string;
      email?: string | null;
      name?: string | null;
      image?: string | null;
    };
    /** Resolved from platform_admins.id in the jwt callback. */
    platform_admin_id?: string;
  }
}

declare module "next-auth/jwt" {
  interface JWT {
    /** auth subject — platform_admins.id (migration 054). */
    platform_admin_id?: string;
  }
}
