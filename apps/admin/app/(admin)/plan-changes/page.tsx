/**
 * Plan changes queue page (Phase 9 PR-8).
 * Server component. Fetches pending plan-change requests.
 */

import { Card } from "@/components/ui/card";
import { adminServerFetch } from "@/lib/api/server";
import { adminRoutes } from "@/lib/api/routes";
import { ApiError } from "@/lib/api/error";
import { RequestQueue } from "@/components/plan-changes/RequestQueue";
import type { PlanChangeRequest } from "@/lib/types/billing";

interface PlanChangesResponse {
  items: PlanChangeRequest[];
  total: number;
}

async function fetchPending(): Promise<
  PlanChangesResponse | { error: string }
> {
  try {
    const res = await adminServerFetch<PlanChangesResponse>(
      adminRoutes.planChanges.pending(),
    );
    return res ?? { items: [], total: 0 };
  } catch (err) {
    if (err instanceof ApiError) {
      if (err.status === 404 || err.status === 501)
        return { items: [], total: 0 };
      return { error: err.message };
    }
    throw err;
  }
}

export default async function PlanChangesPage() {
  const data = await fetchPending();
  return (
    <div className="mx-auto max-w-7xl space-y-6">
      <div>
        <h1 className="text-xl font-semibold tracking-tight">Plan Changes</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Pending tier-change requests awaiting admin approval.
        </p>
      </div>
      {"error" in data ? (
        <Card className="border-destructive/30 bg-destructive/5 p-4 text-sm text-destructive">
          Failed to load plan changes: {data.error}
        </Card>
      ) : (
        <RequestQueue items={data.items} />
      )}
    </div>
  );
}
