/**
 * All-tenants monthly cost page (Phase 9 PR-10).
 *
 * Server component. Fetches from GET /api/admin/cost/tenants.
 * Renders TenantCostTable with amber/red row banding.
 * Month-picker defaults to current month (YYYYMM format).
 * If Mimir is unavailable → shows empty state with explanation.
 */

import { Suspense } from "react";
import { Card } from "@/components/ui/card";
import { adminServerFetch } from "@/lib/api/server";
import { adminRoutes } from "@/lib/api/routes";
import { ApiError } from "@/lib/api/error";
import { TenantCostTable } from "@/components/cost/TenantCostTable";
import { CohereOverrideSubpanel } from "@/components/cost/CohereOverrideSubpanel";
import { PlatformPnLCard } from "@/components/cost/PlatformPnLCard";
import { FixedCostsEditor } from "@/components/cost/FixedCostsEditor";
import type {
  AllTenantsCostResponse,
  CohereOverridesResponse,
  PlatformCostSummary,
  PlatformFixedCost,
} from "@/lib/types/billing";

function currentYyyymm(): string {
  const now = new Date();
  const y = now.getUTCFullYear();
  const m = String(now.getUTCMonth() + 1).padStart(2, "0");
  return `${y}${m}`;
}

function formatMonthDisplay(yyyymm: string): string {
  if (yyyymm.length !== 6) return yyyymm;
  const year = yyyymm.slice(0, 4);
  const month = yyyymm.slice(4, 6);
  const d = new Date(Number(year), Number(month) - 1, 1);
  return d.toLocaleDateString("en-MY", {
    year: "numeric",
    month: "long",
    timeZone: "Asia/Kuala_Lumpur",
  });
}

async function fetchAllTenantsCost(
  month: string,
): Promise<AllTenantsCostResponse | { error: string }> {
  try {
    const path = adminRoutes.cost.allTenants() + `?month=${month}`;
    const res = await adminServerFetch<AllTenantsCostResponse>(path);
    return (
      res ?? { items: [], period_yyyymm: month, data_source: "unavailable" }
    );
  } catch (err) {
    if (err instanceof ApiError) {
      if (err.status === 404 || err.status === 501) {
        return { items: [], period_yyyymm: month, data_source: "unavailable" };
      }
      return { error: err.message };
    }
    // Network / timeout failures (e.g. undici's HeadersTimeoutError surfacing
    // as `TypeError: fetch failed` when the Mimir-backed cost query is slow or
    // the API is unreachable) are NOT ApiErrors. Re-throwing here used to 500
    // the entire dashboard; instead degrade to the same empty state the page
    // already shows when the cost data source is unavailable.
    return { items: [], period_yyyymm: month, data_source: "unavailable" };
  }
}

async function fetchCohereOverrides(
  month: string,
): Promise<CohereOverridesResponse | null> {
  try {
    const path = adminRoutes.cost.cohereOverrides() + `?month=${month}`;
    const res = await adminServerFetch<CohereOverridesResponse>(path);
    return res ?? null;
  } catch {
    return null;
  }
}

async function fetchSummary(
  month: string,
): Promise<PlatformCostSummary | null> {
  try {
    const path = adminRoutes.cost.summary() + `?month=${month}`;
    return (await adminServerFetch<PlatformCostSummary>(path)) ?? null;
  } catch {
    return null;
  }
}

async function fetchFixedCosts(): Promise<PlatformFixedCost[]> {
  try {
    const res = await adminServerFetch<{ items: PlatformFixedCost[] }>(
      adminRoutes.cost.fixed(),
    );
    return res?.items ?? [];
  } catch {
    return [];
  }
}

export default async function CostPage({
  searchParams,
}: {
  searchParams?: { month?: string };
}) {
  const month = searchParams?.month ?? currentYyyymm();
  const [costData, cohereData, summary, fixedCosts] = await Promise.all([
    fetchAllTenantsCost(month),
    fetchCohereOverrides(month),
    fetchSummary(month),
    fetchFixedCosts(),
  ]);

  return (
    <div className="mx-auto max-w-7xl space-y-8">
      <div className="flex items-end justify-between">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">
            Cost Dashboard
          </h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Per-tenant LLM and infrastructure costs —{" "}
            {formatMonthDisplay(month)}
          </p>
        </div>
      </div>

      {/* Platform P&L — fixed vs variable kept separate */}
      <PlatformPnLCard summary={summary} />

      <div className="space-y-3">
        <h2 className="text-base font-medium">
          Per-tenant variable cost (COGS)
        </h2>
        <p className="text-xs text-muted-foreground">
          Marginal cost of serving each tenant — Anthropic + Cohere +
          OpenRouter. Supabase &amp; other infra are fixed platform costs
          (below), not attributed per tenant. WhatsApp fees are borne by
          tenants&rsquo; own WABAs. Banding is variable-cost vs MRR
          (contribution margin).
        </p>
        {"error" in costData ? (
          <Card className="border-destructive/30 bg-destructive/5 p-4 text-sm text-destructive">
            Failed to load cost data: {costData.error}
          </Card>
        ) : (
          <Suspense fallback={null}>
            <TenantCostTable
              items={costData.items}
              dataSource={costData.data_source}
            />
          </Suspense>
        )}
      </div>

      <div className="space-y-3">
        <h2 className="text-base font-medium">Platform fixed costs</h2>
        <p className="text-xs text-muted-foreground">
          Operator-entered global overhead — not amortized into per-tenant cost.
          Edit amounts inline; totals feed the Platform P&amp;L above.
        </p>
        <FixedCostsEditor initial={fixedCosts} />
      </div>

      {cohereData && cohereData.items.length > 0 && (
        <div className="space-y-3">
          <h2 className="text-base font-medium">Cohere Rerank Overrides</h2>
          <p className="text-xs text-muted-foreground">
            Tenants with a per-tenant Cohere rerank override (ADR-026 Am.5).
            Alert fires when Cohere cost exceeds 5% of MRR.
          </p>
          <Suspense fallback={null}>
            <CohereOverrideSubpanel items={cohereData.items} />
          </Suspense>
        </div>
      )}
    </div>
  );
}
