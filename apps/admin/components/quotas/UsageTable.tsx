/**
 * Quota usage table (Phase 9 PR-4).
 *
 * Columns: Tenant, Bot Replies used/cap with usage bar, Seats (included +
 * extra), Status indicator, link to override page.
 *
 * Usage bar colour bands:
 *   < 50%   — green (text-success)
 *   50-79%  — amber (text-warning)
 *   >= 80%  — red (text-destructive)
 */

import Link from "next/link";
import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import { formatInt } from "@/lib/format";

export interface QuotaUsageRow {
  tenant_id: string;
  tenant_name: string;
  plan_tier: string | null;
  bot_replies: number;
  bot_replies_period_cap: number;
  usage_bot_replies_this_period: number;
  usage_pct: number;
  agent_seats_included: number;
  agent_seats_extra: number;
  outbound_broadcast_enabled: boolean;
  multi_branch_enabled: boolean;
}

function usageBandClass(pct: number): string {
  if (pct >= 80) return "bg-destructive";
  if (pct >= 50) return "bg-warning";
  return "bg-success";
}

function usageTextClass(pct: number): string {
  if (pct >= 80) return "text-destructive";
  if (pct >= 50) return "text-warning";
  return "text-success";
}

function UsageBar({ pct }: { pct: number }) {
  const clamped = Math.min(100, Math.max(0, pct));
  return (
    <div className="flex items-center gap-2">
      <div className="h-1.5 w-24 overflow-hidden rounded-full bg-muted">
        <div
          className={cn(
            "h-full rounded-full transition-all",
            usageBandClass(pct),
          )}
          style={{ width: `${clamped}%` }}
          aria-hidden
        />
      </div>
      <span
        className={cn("text-xs tabular-nums font-mono", usageTextClass(pct))}
      >
        {pct.toFixed(1)}%
      </span>
    </div>
  );
}

export function UsageTable({ items }: { items: QuotaUsageRow[] }) {
  if (items.length === 0) {
    return (
      <Card className="border-dashed p-6 text-sm text-muted-foreground">
        No tenants with quota rows yet.
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
                Bot Replies (cap)
              </th>
              <th scope="col" className="px-4 py-2.5 font-medium">
                Usage
              </th>
              <th scope="col" className="px-4 py-2.5 text-right font-medium">
                Seats
              </th>
              <th scope="col" className="px-4 py-2.5 font-medium">
                Tier
              </th>
              <th scope="col" className="px-4 py-2.5"></th>
            </tr>
          </thead>
          <tbody>
            {items.map((row) => (
              <tr
                key={row.tenant_id}
                className="border-t border-border transition hover:bg-muted/40"
              >
                <td className="px-4 py-2.5 font-medium">{row.tenant_name}</td>
                <td className="px-4 py-2.5 font-mono tabular-nums">
                  {formatInt(row.usage_bot_replies_this_period)}
                  <span className="text-muted-foreground">
                    {" "}
                    / {formatInt(row.bot_replies_period_cap)}
                  </span>
                </td>
                <td className="px-4 py-2.5">
                  <UsageBar pct={row.usage_pct} />
                </td>
                <td className="px-4 py-2.5 text-right font-mono tabular-nums">
                  {row.agent_seats_included + row.agent_seats_extra}
                </td>
                <td className="px-4 py-2.5 capitalize">
                  <Badge variant="outline" className="text-xs capitalize">
                    {row.plan_tier ?? "—"}
                  </Badge>
                </td>
                <td className="px-4 py-2.5 text-right">
                  <Link
                    href={`/quotas/${row.tenant_id}`}
                    className="text-xs text-primary underline-offset-2 hover:underline"
                  >
                    Override
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
