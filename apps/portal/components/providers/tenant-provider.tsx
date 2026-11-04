"use client";

/**
 * Tenant context provider (Phase 8 PR-3).
 *
 * Receives the server-fetched tenant context from `(portal)/layout.tsx`
 * (a Server Component) as props, then exposes it through a React
 * Context so any client component in the portal subtree can read
 * `useTenantContext()`. Per the Next 14 docs guidance: the layout
 * fetches once per request and serialises down to props — navigations
 * inside the layout subtree DO NOT re-run the layout fetch.
 *
 * The context never holds a JWT or any secret — it's display-only
 * data + identity scalars the middleware already validated.
 */

import * as React from "react";

import type { PlanTier } from "@/lib/tier-gate";

export interface TenantContextValue {
  user_id: string;
  user_email: string;
  tenant_id: string;
  tenant_slug: string;
  tenant_name: string;
  role: string;
  plan_tier: PlanTier;
  suspended_reason: "quota_exhausted" | "payment_overdue" | null;
}

const TenantContext = React.createContext<TenantContextValue | null>(null);

export function TenantProvider({
  value,
  children,
}: {
  value: TenantContextValue;
  children: React.ReactNode;
}) {
  return (
    <TenantContext.Provider value={value}>{children}</TenantContext.Provider>
  );
}

/**
 * Read the tenant context. Throws when called outside the provider —
 * a missing provider is always a coding error (the (portal) layout
 * always wraps its tree), so failing loudly beats returning a stub
 * that masks the regression.
 */
export function useTenantContext(): TenantContextValue {
  const ctx = React.useContext(TenantContext);
  if (ctx === null) {
    throw new Error(
      "useTenantContext() called outside <TenantProvider>. " +
        "Make sure the consumer is rendered inside the (portal) layout.",
    );
  }
  return ctx;
}
