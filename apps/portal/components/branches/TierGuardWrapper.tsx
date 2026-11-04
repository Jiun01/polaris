"use client";

/**
 * Branches tier guard (Phase 8 PR-9).
 *
 * Renders one of two children based on the tenant plan tier. Branches
 * is Pro+ per ADR-041 Amendment but we don't redirect — instead we
 * show a read-only HQ placeholder per portal_design_prompt.md "never
 * hide features". Pro+ tenants see the full CRUD client.
 */

import * as React from "react";

import { useTenantContext } from "@/components/providers/tenant-provider";
import { meetsTier } from "@/lib/tier-gate";

export function TierGuardWrapper({
  proContent,
  starterContent,
}: {
  proContent: React.ReactNode;
  starterContent: React.ReactNode;
}) {
  const { plan_tier } = useTenantContext();
  if (meetsTier(plan_tier, "pro")) {
    return <>{proContent}</>;
  }
  return <>{starterContent}</>;
}
