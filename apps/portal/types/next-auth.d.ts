/**
 * NextAuth module augmentation (Phase 8 PR-2).
 *
 * Adds Polaris-specific claims to the JWT + session shapes so the
 * portal's server components and middleware can read `tenant_id`,
 * `tenant_slug`, `role`, `plan_tier`, and `suspended_reason` with
 * type safety.
 *
 * Module augmentation has to live in a `.d.ts` picked up by tsconfig's
 * `include`. The default Next.js tsconfig already includes
 * `**\/*.ts` (escape is just for this comment) so this file is picked
 * up automatically — no extra config wiring needed.
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
    tenant_id?: string;
    tenant_slug?: string;
    role?: string;
    plan_tier?: string;
    suspended_reason?: string | null;
  }
}

declare module "next-auth/jwt" {
  interface JWT {
    user_id?: string;
    tenant_id?: string;
    tenant_slug?: string;
    role?: string;
    plan_tier?: string;
    suspended_reason?: string | null;
  }
}
