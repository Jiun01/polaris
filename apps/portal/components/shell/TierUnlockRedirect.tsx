"use client";

/**
 * Inverse of {@link TierLockRedirect}: bounces a tenant that DOES meet the
 * minimum tier away from a locked-variant page back to the real feature page.
 *
 * Pro+ tenants should never linger on /broadcasts/locked — landing there
 * (e.g. via a stale bookmark) redirects them to the live list. Mirrors the
 * client-side gate rationale: tier comes from the TenantProvider session.
 */

import * as React from "react";
import { useRouter } from "next/navigation";

import { useTenantContext } from "@/components/providers/tenant-provider";
import { meetsTier, type PlanTier } from "@/lib/tier-gate";

export function TierUnlockRedirect({
  minTier,
  unlockedPath,
  children,
}: {
  minTier: PlanTier;
  unlockedPath: string;
  children: React.ReactNode;
}) {
  const router = useRouter();
  const { plan_tier } = useTenantContext();
  const allowed = meetsTier(plan_tier, minTier);

  React.useEffect(() => {
    if (allowed) router.replace(unlockedPath);
  }, [allowed, unlockedPath, router]);

  if (allowed) {
    // Render nothing during the redirect tick — avoids flashing the locked
    // upgrade card before the router.replace completes.
    return null;
  }
  return <>{children}</>;
}
