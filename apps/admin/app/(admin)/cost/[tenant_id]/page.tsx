/**
 * Per-tenant cost detail page (Phase 9 PR-10).
 *
 * Server component. Fetches from GET /api/admin/cost/tenants/{tenant_id}.
 * Shows:
 *   - Summary card (total cost, MRR, alert band, period)
 *   - CostCuttingRevisitBanner when data age >= 4 weeks
 *   - IntentTierBreakdown table (simple / medium / hard × token columns)
 */

import Link from "next/link";
import { Suspense } from "react";
import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { adminServerFetch } from "@/lib/api/server";
import { adminRoutes } from "@/lib/api/routes";
import { ApiError } from "@/lib/api/error";
import { IntentTierBreakdown } from "@/components/cost/IntentTierBreakdown";
import { CostCuttingRevisitBanner } from "@/components/cost/CostCuttingRevisitBanner";
import { formatMyr } from "@/lib/format";
import type { TenantCostMonthly } from "@/lib/types/billing";

function currentYyyymm(): string {
  const now = new Date();
  const y = now.getUTCFullYear();
  const m = String(now.getUTCMonth() + 1).padStart(2, "0");
  return `${y}${m}`;
}

function yyyymmToIso(yyyymm: string): string {
  // Returns the first of the month in ISO format for CostCuttingRevisitBanner
  const year = yyyymm.slice(0, 4);
  const month = yyyymm.slice(4, 6);
  return `${year}-${month}-01`;
}

function alertBandBadge(band: string) {
  switch (band) {
    case "green":
      return (
        <Badge
          variant="secondary"
          className="text-xs text-success border-success/30 bg-success/10"
        >
          Green
        </Badge>
      );
    case "amber":
      return (
        <Badge
          variant="secondary"
          className="text-xs text-warning border-warning/30 bg-amber-50"
        >
          Amber
        </Badge>
      );
    case "red":
      return (
        <Badge variant="destructive" className="text-xs">
          Red
        </Badge>
      );
    default:
      return (
        <Badge variant="outline" className="text-xs">
          {band}
        </Badge>
      );
  }
}

async function fetchTenantCost(
  tenantId: string,
  month: string,
): Promise<TenantCostMonthly | { error: string } | null> {
  try {
    const path = adminRoutes.cost.perTenant(tenantId) + `?month=${month}`;
    const res = await adminServerFetch<TenantCostMonthly>(path);
    return res ?? null;
  } catch (err) {
    if (err instanceof ApiError) {
      if (err.status === 404) return null;
      return { error: err.message };
    }
    throw err;
  }
}

export default async function TenantCostDetailPage({
  params,
  searchParams,
}: {
  params: { tenant_id: string };
  searchParams?: { month?: string };
}) {
  const { tenant_id } = params;
  const month = searchParams?.month ?? currentYyyymm();
  const data = await fetchTenantCost(tenant_id, month);

  if (data === null) {
    return (
      <div className="mx-auto max-w-4xl space-y-4">
        <Link
          href="/cost"
          className="text-xs text-muted-foreground underline-offset-2 hover:underline"
        >
          ← Back to cost dashboard
        </Link>
        <Card className="p-6 text-sm text-muted-foreground">
          Tenant not found.
        </Card>
      </div>
    );
  }

  if ("error" in data) {
    return (
      <div className="mx-auto max-w-4xl space-y-4">
        <Link
          href="/cost"
          className="text-xs text-muted-foreground underline-offset-2 hover:underline"
        >
          ← Back to cost dashboard
        </Link>
        <Card className="border-destructive/30 bg-destructive/5 p-4 text-sm text-destructive">
          Failed to load tenant cost: {data.error}
        </Card>
      </div>
    );
  }

  const anthropicTotal =
    Number(data.anthropic_input_cost_myr) +
    Number(data.anthropic_output_cost_myr);

  return (
    <div className="mx-auto max-w-4xl space-y-6">
      <div>
        <Link
          href="/cost"
          className="text-xs text-muted-foreground underline-offset-2 hover:underline"
        >
          ← Back to cost dashboard
        </Link>
        <h1 className="mt-2 text-xl font-semibold tracking-tight">
          Tenant Cost Detail
        </h1>
        <p className="mt-1 text-xs font-mono text-muted-foreground">
          {tenant_id}
        </p>
      </div>

      {/* Cost-cutting revisit banner (shown after 4 weeks of data) */}
      <CostCuttingRevisitBanner
        dataWindowStart={yyyymmToIso(data.period_yyyymm)}
      />

      {/* Summary card */}
      <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
        <Card className="p-4 space-y-1">
          <p className="text-xs text-muted-foreground uppercase tracking-wider">
            Total Cost
          </p>
          <p className="text-lg font-semibold font-mono tabular-nums">
            {formatMyr(Number(data.total_cost_myr))}
          </p>
        </Card>
        <Card className="p-4 space-y-1">
          <p className="text-xs text-muted-foreground uppercase tracking-wider">
            MRR
          </p>
          <p className="text-lg font-semibold font-mono tabular-nums">
            {formatMyr(Number(data.tenant_mrr_myr))}
          </p>
        </Card>
        <Card className="p-4 space-y-1">
          <p className="text-xs text-muted-foreground uppercase tracking-wider">
            % of MRR
          </p>
          <p className="text-lg font-semibold font-mono tabular-nums">
            {(data.cost_pct_of_mrr * 100).toFixed(1)}%
          </p>
        </Card>
        <Card className="p-4 space-y-1">
          <p className="text-xs text-muted-foreground uppercase tracking-wider">
            Alert Band
          </p>
          <div className="pt-1">{alertBandBadge(data.alert_band)}</div>
        </Card>
      </div>

      {/* Cost breakdown cards */}
      <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
        <Card className="p-4 space-y-1">
          <p className="text-xs text-muted-foreground">Anthropic</p>
          <p className="font-mono tabular-nums text-sm">
            {formatMyr(anthropicTotal)}
          </p>
        </Card>
        <Card className="p-4 space-y-1">
          <p className="text-xs text-muted-foreground">Cohere Rerank</p>
          <p className="font-mono tabular-nums text-sm">
            {formatMyr(Number(data.cohere_rerank_cost_myr))}
          </p>
        </Card>
        <Card className="p-4 space-y-1">
          <p className="text-xs text-muted-foreground">Supabase Storage</p>
          <p className="font-mono tabular-nums text-sm">
            {formatMyr(Number(data.supabase_storage_cost_myr))}
          </p>
        </Card>
        <Card className="p-4 space-y-1">
          <p className="text-xs text-muted-foreground">OpenRouter</p>
          <p className="font-mono tabular-nums text-sm">
            {formatMyr(Number(data.openrouter_embedding_cost_myr))}
          </p>
        </Card>
      </div>

      {/* Per-intent-tier breakdown */}
      <div className="space-y-3">
        <h2 className="text-base font-medium">Per-Intent-Tier Breakdown</h2>
        <p className="text-xs text-muted-foreground">
          Token usage by routing tier for the selected period. Cache hit rate
          below 30% after 4 weeks indicates system prompt instability or a short
          cache TTL (ADR-031).
        </p>
        <Suspense fallback={null}>
          {/* per_intent_tier has a Pydantic default_factory=list, which
              json-schema-to-typescript (correctly, per JSON Schema's
              "required" semantics) marks optional in the generated type —
              but the field is a real list default, always present on the
              wire (never actually undefined). Nullish-coalesce to bridge
              the generator's conservative optionality vs the real payload. */}
          <IntentTierBreakdown rows={data.per_intent_tier ?? []} />
        </Suspense>
      </div>
    </div>
  );
}
