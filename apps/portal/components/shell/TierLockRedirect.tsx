"use client";

/**
 * Client-side tier gate that redirects to a locked-variant page when
 * the tenant doesn't meet the minimum tier (Phase 8 PR-9).
 *
 * Used by /broadcasts and /branches — both have dedicated locked
 * pages so Starter tenants see a contextual upgrade card instead of
 * a generic empty state.
 *
 * Why not a Server-Component redirect? The tier comes from the
 * NextAuth session JWT (TenantProvider). Server Components would
 * need to thread the session into every page; the client-side gate
 * reuses the existing TenantProvider context.
 */

import * as React from "react";
import { useRouter } from "next/navigation";

import { useTenantContext } from "@/components/providers/tenant-provider";
import { meetsTier, type PlanTier } from "@/lib/tier-gate";

export function TierLockRedirect({
  minTier,
  lockedPath,
  children,
}: {
  minTier: PlanTier;
  lockedPath: string;
  children: React.ReactNode;
}) {
  const router = useRouter();
  const { plan_tier } = useTenantContext();
  const allowed = meetsTier(plan_tier, minTier);

  React.useEffect(() => {
    if (!allowed) router.replace(lockedPath);
  }, [allowed, lockedPath, router]);

  if (!allowed) {
    // Render nothing during the redirect tick — avoids flashing the
    // gated content before the router.replace completes.
    return null;
  }
  return <>{children}</>;
}
