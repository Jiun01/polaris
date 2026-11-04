/**
 * Platform P&L card (cost-model rework 2026-06-01).
 *
 * Fixed and variable costs kept SEPARATE (no amortization):
 *   gross margin = Σ MRR − (Σ per-tenant variable COGS + Σ fixed pool).
 */

import { Card } from "@/components/ui/card";
import { formatMyr } from "@/lib/format";
import type { PlatformCostSummary } from "@/lib/types/billing";

function Stat({
  label,
  value,
  sub,
  tone,
}: {
  label: string;
  value: string;
  sub?: string;
  tone?: "good" | "bad";
}) {
  const toneClass =
    tone === "good"
      ? "text-green-600 dark:text-green-400"
      : tone === "bad"
        ? "text-destructive"
        : "";
  return (
    <div className="flex flex-col gap-0.5">
      <span className="text-xs uppercase tracking-wide text-muted-foreground">
        {label}
      </span>
      <span className={`text-2xl font-bold tabular-nums ${toneClass}`}>
        {value}
      </span>
      {sub ? (
        <span className="text-xs text-muted-foreground">{sub}</span>
      ) : null}
    </div>
  );
}

export function PlatformPnLCard({
  summary,
}: {
  summary: PlatformCostSummary | null;
}) {
  if (!summary) {
    return (
      <Card className="p-4 text-sm text-muted-foreground">
        Platform P&amp;L unavailable.
      </Card>
    );
  }
  const margin = Number(summary.gross_margin_myr);
  const marginPct = Math.round(summary.gross_margin_pct * 100);
  const positive = margin >= 0;

  return (
    <Card className="p-5">
      <div className="grid grid-cols-2 gap-6 sm:grid-cols-4">
        <Stat
          label="MRR"
          value={formatMyr(summary.total_mrr_myr)}
          sub={`${summary.active_tenant_count} active tenants`}
        />
        <Stat
          label="Variable (COGS)"
          value={formatMyr(summary.total_variable_cost_myr)}
          sub="Anthropic + Cohere + OpenRouter"
        />
        <Stat
          label="Fixed pool"
          value={formatMyr(summary.total_fixed_cost_myr)}
          sub="Supabase, infra, demo WA…"
        />
        <Stat
          label="Gross margin"
          value={formatMyr(summary.gross_margin_myr)}
          sub={`${marginPct}% of MRR`}
          tone={positive ? "good" : "bad"}
        />
      </div>
    </Card>
  );
}
