/**
 * Tenants list table (Phase 9 PR-3, Deliverable #1).
 *
 * Dense, operator-grade table. Columns prioritise scan-friendliness:
 * status pill first so suspended rows pop, then identity, then
 * commercial signals (tier + MRR), then activity (user_count, channels,
 * last_inbound_at).
 *
 * Plain HTML <table> with shadcn tokens — no Radix primitive needed for
 * a read-only list. Sort/filter is handled by the parent server
 * component via searchParams.
 */

import Link from "next/link";
import { ChevronRight } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Card } from "@/components/ui/card";
import { formatMyr, formatMyt, formatInt } from "@/lib/format";
import { cn } from "@/lib/utils";
import type { TenantStatus, TenantSummary } from "@/components/tenants/types";

const STATUS_BADGE: Record<TenantStatus, string> = {
  active: "bg-success/15 text-success border-success/30",
  suspended: "bg-warning/15 text-warning border-warning/30",
  deleted: "bg-muted text-muted-foreground border-border",
};

function StatusBadge({
  status,
  reason,
}: {
  status: TenantStatus;
  reason: string | null;
}) {
  return (
    <Badge
      variant="outline"
      className={cn("font-medium capitalize", STATUS_BADGE[status])}
    >
      {status === "suspended" && reason
        ? `${status}: ${reason.replace("_", " ")}`
        : status}
    </Badge>
  );
}

export function TenantsTable({ items }: { items: TenantSummary[] }) {
  if (items.length === 0) {
    return (
      <Card className="flex flex-col items-center justify-center gap-2 border-dashed py-12 text-center">
        <p className="text-sm font-medium">No tenants yet.</p>
        <p className="text-sm text-muted-foreground">
          Create the first one to send a welcome magic-link.
        </p>
        <Link
          href="/tenants/new"
          className="mt-2 text-sm font-medium text-primary underline-offset-4 hover:underline"
        >
          New tenant →
        </Link>
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
                Status
              </th>
              <th scope="col" className="px-4 py-2.5 font-medium">
                Tenant
              </th>
              <th scope="col" className="px-4 py-2.5 font-medium">
                Tier
              </th>
              <th scope="col" className="px-4 py-2.5 text-right font-medium">
                MRR
              </th>
              <th scope="col" className="px-4 py-2.5 text-right font-medium">
                Users
              </th>
              <th scope="col" className="px-4 py-2.5 text-right font-medium">
                Channels
              </th>
              <th scope="col" className="px-4 py-2.5 font-medium">
                Last inbound
              </th>
              <th scope="col" className="px-4 py-2.5"></th>
            </tr>
          </thead>
          <tbody>
            {items.map((t) => (
              <tr
                key={t.id}
                className="border-t border-border transition hover:bg-muted/40"
              >
                <td className="px-4 py-2.5">
                  <StatusBadge status={t.status} reason={t.suspended_reason} />
                </td>
                <td className="px-4 py-2.5">
                  <div className="flex flex-col">
                    <Link
                      href={`/tenants/${t.id}`}
                      className="font-medium text-foreground hover:underline"
                    >
                      {t.display_name}
                    </Link>
                    <span className="text-xs text-muted-foreground">
                      {t.slug}
                    </span>
                  </div>
                </td>
                <td className="px-4 py-2.5 capitalize">{t.plan_tier ?? "—"}</td>
                <td className="px-4 py-2.5 text-right font-mono tabular-nums">
                  {t.mrr_myr === null ? "—" : formatMyr(t.mrr_myr)}
                </td>
                <td className="px-4 py-2.5 text-right font-mono tabular-nums">
                  {formatInt(t.user_count)}
                </td>
                <td className="px-4 py-2.5 text-right font-mono tabular-nums">
                  {formatInt(t.channel_count)}
                </td>
                <td className="px-4 py-2.5 text-muted-foreground">
                  {formatMyt(t.last_inbound_at)}
                </td>
                <td className="px-4 py-2.5 text-right">
                  <Link
                    href={`/tenants/${t.id}`}
                    aria-label={`Open ${t.display_name}`}
                    className="inline-flex items-center text-muted-foreground hover:text-foreground"
                  >
                    <ChevronRight className="h-4 w-4" aria-hidden />
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
