/**
 * (portal) route-group layout (Phase 8 PR-3).
 *
 * Server Component. Resolves the tenant context once per request from
 * the NextAuth session + Polaris DB, then hands it off to the client
 * tree via <TenantProvider>. Navigations inside this subtree do NOT
 * re-run this fetch (Next 14 partial-rendering behaviour).
 *
 * If the session is missing OR the user no longer resolves to a
 * tenant_users row, we surface a 500-ish error via redirect to
 * /login. Middleware should normally catch that case first (the
 * matcher hits this route), but the fall-back here is defensive in
 * case middleware order changes.
 */

import { redirect } from "next/navigation";
import { getServerSession } from "next-auth";
import { Toaster } from "sonner";

import { Sidebar } from "@/components/shell/Sidebar";
import { TopBar } from "@/components/shell/TopBar";
import {
  TenantProvider,
  type TenantContextValue,
} from "@/components/providers/tenant-provider";
import { authOptions } from "@/lib/auth/options";
import { portalQuery, DbUnavailableError } from "@/lib/auth/db";
import { ServiceUnavailable } from "@/components/shell/ServiceUnavailable";
import type { PlanTier } from "@/lib/tier-gate";

// Allow-lists for narrowing DB-returned strings into our literal unions.
// External data (DB) must be validated at the boundary; if a future
// migration adds a new suspension reason or tier value, we want a typed
// failure path (default fallback + console warning), not a silent cast
// that produces an out-of-contract value downstream.
const VALID_TIERS: ReadonlySet<PlanTier> = new Set([
  "starter",
  "pro",
  "ultra",
  "enterprise",
]);

const VALID_SUSPENSION_REASONS = new Set([
  "quota_exhausted",
  "payment_overdue",
] as const);

function narrowTier(raw: string | null): PlanTier {
  if (raw && VALID_TIERS.has(raw as PlanTier)) {
    return raw as PlanTier;
  }
  if (raw !== null && raw !== undefined) {
    // eslint-disable-next-line no-console
    console.warn(
      `[portal layout] unknown plan_tier=${JSON.stringify(raw)} from DB; treating as 'starter'.`,
    );
  }
  return "starter";
}

function narrowSuspension(
  raw: string | null,
): "quota_exhausted" | "payment_overdue" | null {
  if (raw === null || raw === undefined) return null;
  if (
    VALID_SUSPENSION_REASONS.has(raw as "quota_exhausted" | "payment_overdue")
  ) {
    return raw as "quota_exhausted" | "payment_overdue";
  }
  // eslint-disable-next-line no-console
  console.warn(
    `[portal layout] unknown suspended_reason=${JSON.stringify(raw)} from DB; treating as not suspended.`,
  );
  return null;
}

async function fetchTenantContext(
  email: string,
): Promise<TenantContextValue | null> {
  const normalized = email.trim().toLowerCase();
  // Use SECURITY DEFINER helper (migration 066) — polaris_service cannot
  // query auth.users directly (Supabase restricts USAGE on auth schema).
  const { rows } = await portalQuery<{
    user_id: string;
    user_email: string;
    tenant_id: string;
    tenant_slug: string;
    tenant_name: string;
    role: string;
    plan_tier: string | null;
    suspended_reason: string | null;
  }>(
    `
    SELECT pu.id                AS user_id,
           pu.email              AS user_email,
           t.id                  AS tenant_id,
           t.slug                AS tenant_slug,
           t.slug                AS tenant_name,
           tu.role               AS role,
           tq.plan_tier          AS plan_tier,
           t.suspended_reason    AS suspended_reason
      FROM public.get_portal_user_by_email($1) pu
      JOIN tenant_users  tu ON tu.user_id = pu.id
      JOIN tenants       t  ON t.id = tu.tenant_id
 LEFT JOIN tenant_quotas tq ON tq.tenant_id = t.id
     LIMIT 1
    `,
    [normalized],
  );
  if (rows.length === 0) return null;
  const r = rows[0];
  return {
    user_id: r.user_id,
    user_email: r.user_email,
    tenant_id: r.tenant_id,
    tenant_slug: r.tenant_slug,
    tenant_name: r.tenant_name,
    role: r.role,
    plan_tier: narrowTier(r.plan_tier),
    suspended_reason: narrowSuspension(r.suspended_reason),
  };
}

export default async function PortalLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  const session = await getServerSession(authOptions);
  if (!session?.user?.email) {
    redirect("/login");
  }

  let ctx: TenantContextValue | null;
  try {
    ctx = await fetchTenantContext(session.user.email);
  } catch (err) {
    // Distinguish "DB unreachable" (transient — e.g. Supabase network ban) from
    // a genuine failure. On unreachable we show a calm "try again" page rather
    // than redirect("/login"): /login hits the same DB, so redirecting would
    // loop the user and surface a raw 500. Genuine failures stay fail-closed.
    if (err instanceof DbUnavailableError) {
      // eslint-disable-next-line no-console
      console.error(
        "[portal layout] DB unreachable; showing transient unavailable page (NOT redirecting to /login to avoid a loop).",
        err,
      );
      return <ServiceUnavailable />;
    }
    // eslint-disable-next-line no-console
    console.error(
      "[portal layout] fetchTenantContext failed; redirecting to /login (fail-closed).",
      err,
    );
    redirect("/login");
  }
  if (ctx === null) {
    redirect("/login");
  }

  return (
    <TenantProvider value={ctx}>
      <div className="flex min-h-screen bg-background">
        <Sidebar />
        <div className="flex min-w-0 flex-1 flex-col">
          <TopBar />
          <main className="flex-1 overflow-y-auto">
            <div className="mx-auto max-w-7xl px-4 py-6 md:px-6 md:py-8">
              {children}
            </div>
          </main>
        </div>
      </div>
      {/* Sonner toast container — mounted once at the portal layout level
          so any client component inside can call toast() without its own
          <Toaster>. richColors keeps the palette consistent with our
          destructive/success design tokens. */}
      <Toaster richColors position="bottom-right" />
    </TenantProvider>
  );
}
