/**
 * Plan-change request queue table (Phase 9 PR-8).
 *
 * Server-compatible (no "use client" — pure render, no hooks).
 * Displays pending plan-change requests with: tenant name, tier
 * transition, billing cycle change, requested-at timestamp, and a
 * "Review" link to the detail page.
 */

import Link from "next/link";
import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { formatMytDate } from "@/lib/format";
import type { PlanChangeRequest } from "@/lib/types/billing";

function tierBadge(tier: string) {
  return (
    <Badge variant="outline" className="text-xs capitalize">
      {tier}
    </Badge>
  );
}

interface Props {
  items: PlanChangeRequest[];
}

export function RequestQueue({ items }: Props) {
  if (items.length === 0) {
    return (
      <Card className="flex flex-col items-center justify-center gap-1 border-dashed py-12 text-center">
        <p className="text-sm font-medium">No pending plan-change requests.</p>
        <p className="text-sm text-muted-foreground">
          Requests appear here when tenants submit a tier upgrade or downgrade.
        </p>
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
              <th scope="col" className="px-4 py-2.5 font-medium">
                Current tier
              </th>
              <th scope="col" className="px-4 py-2.5 font-medium">
                Target tier
              </th>
              <th scope="col" className="px-4 py-2.5 font-medium">
                Billing cycle
              </th>
              <th scope="col" className="px-4 py-2.5 font-medium">
                Requested at
              </th>
              <th scope="col" className="px-4 py-2.5 font-medium">
                Action
              </th>
            </tr>
          </thead>
          <tbody>
            {items.map((req) => (
              <tr
                key={req.id}
                className="border-t border-border transition hover:bg-muted/40"
              >
                <td className="px-4 py-2.5">
                  <div className="flex flex-col">
                    <span className="font-medium">
                      {req.tenant_name ?? "—"}
                    </span>
                    <span className="font-mono text-xs text-muted-foreground">
                      {req.tenant_id.slice(0, 8)}…
                    </span>
                  </div>
                </td>
                <td className="px-4 py-2.5">{tierBadge(req.current_tier)}</td>
                <td className="px-4 py-2.5">{tierBadge(req.target_tier)}</td>
                <td className="px-4 py-2.5 text-xs text-muted-foreground">
                  {req.current_billing_cycle !== req.target_billing_cycle ? (
                    <>
                      {req.current_billing_cycle}{" "}
                      <span className="text-foreground">→</span>{" "}
                      {req.target_billing_cycle}
                    </>
                  ) : (
                    req.target_billing_cycle
                  )}
                </td>
                <td className="px-4 py-2.5 text-xs text-muted-foreground whitespace-nowrap">
                  <div className="flex flex-col gap-0.5">
                    <span>{formatMytDate(req.requested_at)}</span>
                    <span className="font-mono">{req.id.slice(0, 8)}</span>
                  </div>
                </td>
                <td className="px-4 py-2.5">
                  <Link
                    href={`/plan-changes/${req.id}`}
                    aria-label={`Review plan change for ${req.tenant_name ?? req.tenant_id.slice(0, 8)}`}
                    className="text-xs font-medium text-primary underline-offset-2 hover:underline"
                  >
                    Review
                  </Link>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  );
}
