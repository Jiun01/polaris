"use client";

/**
 * Knowledge Gaps tier guard (Knowledge Gap Detection M3).
 *
 * Ultra+ only. Per portal_design_prompt.md "never hide features": lower tiers
 * still see the screen in nav and land here, but get an upgrade card instead of
 * the real surface. Reads the plan tier from the tenant context the (portal)
 * layout already resolved.
 */

import * as React from "react";

import { useTenantContext } from "@/components/providers/tenant-provider";
import { UpgradeCard, meetsTier } from "@/lib/tier-gate";

export function KnowledgeGapsTierGuard({
  children,
}: {
  children: React.ReactNode;
}) {
  const { plan_tier } = useTenantContext();
  if (meetsTier(plan_tier, "ultra")) {
    return <>{children}</>;
  }
  return (
    <UpgradeCard
      feature="Knowledge Gaps"
      minTier="ultra"
      description="See the questions your bot can't answer, clustered and ranked, so you know exactly what to add to your knowledge base."
    />
  );
}
