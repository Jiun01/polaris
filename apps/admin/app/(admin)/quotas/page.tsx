/**
 * Quotas usage page (Phase 9 PR-4).
 *
 * Server component. Calls GET /api/admin/quotas/usage via
 * adminServerFetch. Renders UsageTable with all active tenants.
 * Per-tenant override is at /quotas/[tenant_id].
 */

import { Card } from "@/components/ui/card";
import { adminServerFetch } from "@/lib/api/server";
import { adminRoutes } from "@/lib/api/routes";
import { ApiError } from "@/lib/api/error";
import { UsageTable, type QuotaUsageRow } from "@/components/quotas/UsageTable";

interface QuotaUsageResponse {
  items: QuotaUsageRow[];
  total: number;
}

async function fetchUsage(): Promise<QuotaUsageResponse | { error: string }> {
  try {
    const res = await adminServerFetch<QuotaUsageResponse>(
      adminRoutes.quotas.usage(),
    );
    return res ?? { items: [], total: 0 };
  } catch (err) {
    if (err instanceof ApiError) {
      if (err.status === 404 || err.status === 501) {
        return { items: [], total: 0 };
      }
      return { error: err.message };
    }
    throw err;
  }
}

export default async function QuotasPage() {
  const data = await fetchUsage();

  return (
    <div className="mx-auto max-w-7xl space-y-6">
      <div>
        <h1 className="text-xl font-semibold tracking-tight">Quotas</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Per-tenant quota caps and live bot-reply usage. Click Override to
          adjust a tenant's quota column.
        </p>
      </div>

      {"error" in data ? (
        <Card className="border-destructive/30 bg-destructive/5 p-4 text-sm text-destructive">
          Failed to load quota usage: {data.error}
        </Card>
      ) : (
        <UsageTable items={data.items} />
      )}
    </div>
  );
}
