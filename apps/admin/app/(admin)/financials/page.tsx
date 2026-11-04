/**
 * Financials page (Phase 9 PR-11).
 *
 * Server component. Fetches GET /api/admin/financials/mrr on load.
 * Renders:
 *   - MrrOverviewCard (big number + trend table + recompute button)
 */

import { Card } from "@/components/ui/card";
import { adminServerFetch } from "@/lib/api/server";
import { adminRoutes } from "@/lib/api/routes";
import { ApiError } from "@/lib/api/error";
import {
  MrrOverviewCard,
  type MrrRollupData,
} from "@/components/financials/MrrOverviewCard";

async function fetchMrr(): Promise<MrrRollupData | { error: string }> {
  try {
    const res = await adminServerFetch<MrrRollupData>(
      adminRoutes.financials.mrr(),
    );
    return (
      res ?? {
        mrr_myr: 0,
        alert_band: "green",
        computed_at: new Date().toISOString(),
        trend_30d: [],
      }
    );
  } catch (err) {
    if (err instanceof ApiError) {
      // 404 / 501 = endpoint not yet live in this env — return empty state
      if (err.status === 404 || err.status === 501) {
        return {
          mrr_myr: 0,
          alert_band: "green",
          computed_at: new Date().toISOString(),
          trend_30d: [],
        };
      }
      return { error: err.message };
    }
    throw err;
  }
}

export default async function FinancialsPage() {
  const data = await fetchMrr();

  return (
    <div className="mx-auto max-w-4xl space-y-6">
      <div>
        <h1 className="text-xl font-semibold tracking-tight">Financials</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          MRR monitoring and 30-day trend.
        </p>
      </div>

      {"error" in data ? (
        <Card className="border-destructive/30 bg-destructive/5 p-4 text-sm text-destructive">
          Failed to load MRR data: {data.error}
        </Card>
      ) : (
        <div className="grid gap-6 md:grid-cols-1">
          <MrrOverviewCard initial={data} />
        </div>
      )}
    </div>
  );
}
