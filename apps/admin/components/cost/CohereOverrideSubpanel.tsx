"use client";

/**
 * Cohere-override subpanel (Phase 9 PR-10).
 *
 * Shows only tenants with cohere_rerank_enabled IS NOT NULL.
 * Alerts (amber row) when Cohere cost > 5% of that tenant's MRR.
 */

import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import { formatMyr } from "@/lib/format";
import type { CohereOverrideCost } from "@/lib/types/billing";

interface Props {
  items: CohereOverrideCost[];
}

export function CohereOverrideSubpanel({ items }: Props) {
  if (items.length === 0) {
    return (
      <Card className="border-dashed p-6 text-sm text-muted-foreground">
        No tenants with Cohere rerank override configured.
      </Card>
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
                MRR
              </th>
              <th scope="col" className="px-4 py-2.5 text-right font-medium">
                Cohere Cost
              </th>
              <th scope="col" className="px-4 py-2.5 text-right font-medium">
                % of MRR
              </th>
              <th scope="col" className="px-4 py-2.5 font-medium">
                Alert
              </th>
            </tr>
          </thead>
          <tbody>
            {items.map((row) => (
              <tr
                key={row.tenant_id}
                className={cn(
                  "border-t border-border transition",
                  row.alert
                    ? "bg-amber-50 hover:brightness-95"
                    : "hover:bg-muted/40",
                )}
              >
                <td className="px-4 py-2.5 font-mono text-xs text-muted-foreground">
                  {row.tenant_id.slice(0, 8)}…
                </td>
                <td className="px-4 py-2.5 text-right font-mono tabular-nums text-xs">
                  {formatMyr(Number(row.tenant_mrr_myr))}
                </td>
                <td className="px-4 py-2.5 text-right font-mono tabular-nums text-xs">
                  {formatMyr(Number(row.cohere_rerank_cost_myr))}
                </td>
                <td className="px-4 py-2.5 text-right font-mono tabular-nums text-xs">
                  {(row.pct_of_mrr * 100).toFixed(2)}%
                </td>
                <td className="px-4 py-2.5">
                  {row.alert ? (
                    <Badge
                      variant="secondary"
                      className="text-xs text-warning border-warning/30 bg-amber-50"
                    >
                      &gt;5% MRR
                    </Badge>
                  ) : (
                    <span className="text-xs text-muted-foreground">—</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  );
}
