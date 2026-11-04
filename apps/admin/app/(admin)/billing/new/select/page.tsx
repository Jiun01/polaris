/**
 * Tenant selector for issuing an invoice (Phase 9 PR-7).
 */

import Link from "next/link";
import { Card } from "@/components/ui/card";
import { adminServerFetch } from "@/lib/api/server";
import { adminRoutes } from "@/lib/api/routes";
import { ApiError } from "@/lib/api/error";

interface TenantSummary {
  id: string;
  display_name: string;
  slug: string;
  status: "active" | "suspended" | "deleted";
}
interface TenantsResponse {
  items: TenantSummary[];
  total: number;
}

async function fetchTenants(): Promise<TenantSummary[]> {
  try {
    const res = await adminServerFetch<TenantsResponse>(
      adminRoutes.tenants.list(),
    );
    // Exclude hard-deleted tenants — you can't issue an invoice to one.
    // Suspended tenants stay (payment_overdue tenants are still billable).
    return (res?.items ?? []).filter((t) => t.status !== "deleted");
  } catch (err) {
    if (err instanceof ApiError && (err.status === 404 || err.status === 501))
      return [];
    throw err;
  }
}

export default async function SelectTenantPage() {
  const tenants = await fetchTenants();
  return (
    <div className="mx-auto max-w-2xl space-y-6">
      <Link
        href="/billing"
        className="text-xs text-muted-foreground hover:text-foreground hover:underline"
      >
        Back to Billing
      </Link>
      <div>
        <h1 className="text-xl font-semibold tracking-tight">
          Issue invoice — select tenant
        </h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Choose the tenant to invoice.
        </p>
      </div>
      {tenants.length === 0 ? (
        <Card className="border-dashed p-6 text-sm text-muted-foreground">
          No tenants found.
        </Card>
      ) : (
        <Card className="divide-y divide-border overflow-hidden">
          {tenants.map((t) => (
            <Link
              key={t.id}
              href={"/billing/new/" + t.id}
              className="flex items-center justify-between px-4 py-3 text-sm hover:bg-muted/40 transition"
            >
              <div>
                <span className="font-medium">{t.display_name}</span>
                <span className="ml-2 font-mono text-xs text-muted-foreground">
                  {t.slug}
                </span>
              </div>
              <span className="text-xs text-primary">Select →</span>
            </Link>
          ))}
        </Card>
      )}
    </div>
  );
}
