"use client";

import * as React from "react";

import { Badge } from "@/components/ui/badge";
import { useTenantContext } from "@/components/providers/tenant-provider";

/**
 * Plan-tier pill in the top bar (Phase 8 PR-3).
 *
 * Reads tier from the shared TenantContext so it stays in sync if a
 * plan change happens mid-session (currently rare at GA — plan
 * changes route through Phase 10 admin approval per ADR-029 — but
 * the live read costs nothing).
 */
export function TenantBadge() {
  const { tenant_name, plan_tier } = useTenantContext();
  const variant =
    plan_tier === "enterprise"
      ? "default"
      : plan_tier === "ultra"
        ? "secondary"
        : plan_tier === "pro"
          ? "secondary"
          : "outline";
  const label =
    plan_tier === "enterprise"
      ? "Enterprise"
      : plan_tier === "ultra"
        ? "Ultra"
        : plan_tier === "pro"
          ? "Pro"
          : "Starter";

  return (
    <div className="flex items-center gap-2">
      <span className="text-sm font-medium text-foreground">{tenant_name}</span>
      <Badge variant={variant}>{label}</Badge>
    </div>
  );
}
