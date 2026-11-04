"use client";

/**
 * All-tenants cost table (Phase 9 PR-10).
 *
 * Columns: tenant, MRR, anthropic_cost, cohere_cost, supabase_cost,
 * total_cost, cost_pct_of_mrr, alert_band.
 * Rows coloured amber (>50% MRR) or red (>100% MRR) via Tailwind.
 * Sortable by cost_pct_of_mrr (default desc).
 */

import { useState } from "react";
import Link from "next/link";
import { ArrowUpDown } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Card } from "@/components/ui/card";
import { cn } from "@/lib/utils";
import { formatMyr } from "@/lib/format";
import type { AlertBand, TenantCostMonthly } from "@/lib/types/billing";

type SortKey = "cost_pct_of_mrr" | "total_cost_myr" | "tenant_mrr_myr";
type SortDir = "asc" | "desc";

function alertBadge(band: AlertBand) {
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
  }
}

function rowBg(band: AlertBand): string {
  switch (band) {
    case "amber":
      return "bg-amber-50";
    case "red":
      return "bg-red-50";
    default:
      return "";
  }
}

interface Props {
  items: TenantCostMonthly[];
  dataSource: string;
}

export function TenantCostTable({ items, dataSource }: Props) {
  const [sortKey, setSortKey] = useState<SortKey>("cost_pct_of_mrr");
  const [sortDir, setSortDir] = useState<SortDir>("desc");

  function toggleSort(key: SortKey) {
    if (sortKey === key) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortKey(key);
      setSortDir("desc");
    }
  }

  const sorted = [...items].sort((a, b) => {
    const av = Number(a[sortKey]);
    const bv = Number(b[sortKey]);
    const cmp = av - bv;
    return sortDir === "asc" ? cmp : -cmp;
  });

  if (dataSource === "unavailable") {
    return (
      <Card className="border-dashed p-8 text-center text-sm text-muted-foreground">
        <p className="font-medium">No data available</p>
        <p className="mt-1">
          Phase 4 LLM call-sites must emit OTel labels (
          <code className="font-mono text-xs">tenant_id</code>,{" "}
          <code className="font-mono text-xs">intent_tier</code>,{" "}
          <code className="font-mono text-xs">model_used</code>) before cost
          data appears here.
        </p>
      </Card>
    );
  }

  if (sorted.length === 0) {
    return (
      <Card className="border-dashed p-6 text-sm text-muted-foreground">
        No tenants found.
      </Card>
    );
  }

  function SortBtn({
    col,
    children,
  }: {
    col: SortKey;
    children: React.ReactNode;
  }) {
    return (
      <button
        onClick={() => toggleSort(col)}
        className={cn(
          "inline-flex items-center gap-1 hover:text-foreground transition-colors",
          sortKey === col ? "text-foreground" : "",
        )}
      >
        {children}
        <ArrowUpDown className="h-3 w-3 opacity-60" aria-hidden />
      </button>
    );
  }

  return (
    <Card className="overflow-hidden">
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="bg-muted/50 text-left text-xs uppercase tracking-wider text-muted-foreground">
            <tr>
              <th scope="col" className="px-4 py-2.5 font-medium">
                Tenant
              </th>
              <th scope="col" className="px-4 py-2.5 text-right font-medium">
                <SortBtn col="tenant_mrr_myr">MRR</SortBtn>
              </th>
              <th scope="col" className="px-4 py-2.5 text-right font-medium">
                Anthropic Cost
              </th>
              <th scope="col" className="px-4 py-2.5 text-right font-medium">
                Cohere Cost
              </th>
              <th
                scope="col"
                className="px-4 py-2.5 text-right font-medium text-muted-foreground/60"
                title="Informational — Supabase is a fixed platform cost, not per-tenant"
              >
                Supabase (fixed)
              </th>
              <th scope="col" className="px-4 py-2.5 text-right font-medium">
                <SortBtn col="total_cost_myr">Variable Cost</SortBtn>
              </th>
              <th scope="col" className="px-4 py-2.5 text-right font-medium">
                <SortBtn col="cost_pct_of_mrr">Cost % MRR</SortBtn>
              </th>
              <th scope="col" className="px-4 py-2.5 text-right font-medium">
                Margin
              </th>
              <th scope="col" className="px-4 py-2.5 font-medium">
                Band
              </th>
              <th scope="col" className="px-4 py-2.5 font-medium">
                Detail
              </th>
            </tr>
          </thead>
          <tbody>
            {sorted.map((row) => {
              const anthropicCost =
                Number(row.anthropic_input_cost_myr) +
                Number(row.anthropic_output_cost_myr);
              return (
                <tr
                  key={row.tenant_id}
                  data-band={row.alert_band}
                  className={cn(
                    "border-t border-border transition",
                    rowBg(row.alert_band),
                    "hover:brightness-95",
                  )}
                >
                  <td className="px-4 py-2.5 text-xs">
                    <span className="font-medium">
                      {row.tenant_name || row.tenant_id.slice(0, 8) + "…"}
                    </span>
                  </td>
                  <td className="px-4 py-2.5 text-right font-mono tabular-nums text-xs">
                    {formatMyr(Number(row.tenant_mrr_myr))}
                  </td>
                  <td className="px-4 py-2.5 text-right font-mono tabular-nums text-xs">
                    {formatMyr(anthropicCost)}
                  </td>
                  <td className="px-4 py-2.5 text-right font-mono tabular-nums text-xs">
                    {formatMyr(Number(row.cohere_rerank_cost_myr))}
                  </td>
                  <td className="px-4 py-2.5 text-right font-mono tabular-nums text-xs text-muted-foreground/60">
                    {formatMyr(Number(row.supabase_storage_cost_myr))}
                  </td>
                  <td className="px-4 py-2.5 text-right font-mono tabular-nums text-xs font-medium">
                    {formatMyr(Number(row.total_cost_myr))}
                  </td>
                  <td className="px-4 py-2.5 text-right font-mono tabular-nums text-xs">
                    {(row.cost_pct_of_mrr * 100).toFixed(1)}%
                  </td>
                  <td
                    className={cn(
                      "px-4 py-2.5 text-right font-mono tabular-nums text-xs font-medium",
                      Number(row.contribution_margin_myr) < 0
                        ? "text-destructive"
                        : "text-green-600 dark:text-green-400",
                    )}
                  >
                    {formatMyr(Number(row.contribution_margin_myr))}
                  </td>
                  <td className="px-4 py-2.5" data-band={row.alert_band}>
                    {alertBadge(row.alert_band)}
                  </td>
                  <td className="px-4 py-2.5">
                    <Link
                      href={`/cost/${row.tenant_id}`}
                      className="text-xs text-primary underline-offset-2 hover:underline"
                    >
                      View
                    </Link>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </Card>
  );
}
