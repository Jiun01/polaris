"use client";

/**
 * Invoice table component (Phase 9 PR-7).
 *
 * Renders a dense, sortable table of TenantInvoice rows. Status badges
 * use Tailwind custom colour utilities to mirror the cost-band pattern
 * established in earlier PRs.
 *
 * Sorting is local (client-side) — the server already returns all
 * invoices for the selected tab filter; sorting within that set is fine
 * at this data volume.
 */

import { useMemo, useState } from "react";
import Link from "next/link";
import { ArrowUpDown } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { adminRoutes } from "@/lib/api/routes";
import { cn } from "@/lib/utils";
import { formatMyr, formatMytDate } from "@/lib/format";
import type { TenantInvoice } from "@/lib/types/billing";
import { MarkPaidButton } from "@/components/billing/MarkPaidButton";

type SortKey = "issued_at" | "due_at" | "total_myr" | "invoice_number";
type SortDir = "asc" | "desc";

function statusBadge(status: TenantInvoice["status"]) {
  switch (status) {
    case "issued":
      return (
        <Badge variant="default" className="text-xs">
          Issued
        </Badge>
      );
    case "paid":
      return (
        <Badge
          variant="secondary"
          className="text-xs text-success border-success/30 bg-success/10"
        >
          Paid
        </Badge>
      );
    case "overdue":
      return (
        <Badge variant="destructive" className="text-xs">
          Overdue
        </Badge>
      );
    case "void":
      return (
        <Badge variant="outline" className="text-xs text-muted-foreground">
          Void
        </Badge>
      );
  }
}

function kindLabel(kind: TenantInvoice["invoice_kind"]): string {
  switch (kind) {
    case "monthly_subscription":
      return "Monthly sub";
    case "annual_subscription":
      return "Annual sub";
    case "plan_change_proration":
      return "Proration";
    case "addon_seat":
      return "Addon seat";
  }
}

interface Props {
  items: TenantInvoice[];
}

export function InvoiceTable({ items }: Props) {
  const [sortKey, setSortKey] = useState<SortKey>("issued_at");
  const [sortDir, setSortDir] = useState<SortDir>("desc");

  function toggleSort(key: SortKey) {
    if (sortKey === key) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortKey(key);
      setSortDir("desc");
    }
  }

  const sorted = useMemo(
    () =>
      [...items].sort((a, b) => {
        let cmp = 0;
        switch (sortKey) {
          case "issued_at":
            cmp = a.issued_at.localeCompare(b.issued_at);
            break;
          case "due_at":
            cmp = a.due_at.localeCompare(b.due_at);
            break;
          case "total_myr":
            cmp = a.total_myr - b.total_myr;
            break;
          case "invoice_number":
            cmp = a.invoice_number.localeCompare(b.invoice_number);
            break;
        }
        return sortDir === "asc" ? cmp : -cmp;
      }),
    [items, sortKey, sortDir],
  );

  if (sorted.length === 0) {
    return (
      <Card className="border-dashed p-6 text-sm text-muted-foreground">
        No invoices found.
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
                <SortBtn col="invoice_number">Invoice #</SortBtn>
              </th>
              <th scope="col" className="px-4 py-2.5 font-medium">
                Tenant
              </th>
              <th scope="col" className="px-4 py-2.5 font-medium">
                Kind
              </th>
              <th scope="col" className="px-4 py-2.5 font-medium">
                Period
              </th>
              <th scope="col" className="px-4 py-2.5 text-right font-medium">
                <SortBtn col="total_myr">Amount</SortBtn>
              </th>
              <th scope="col" className="px-4 py-2.5 font-medium">
                Status
              </th>
              <th scope="col" className="px-4 py-2.5 font-medium">
                <SortBtn col="issued_at">Issued</SortBtn>
              </th>
              <th scope="col" className="px-4 py-2.5 font-medium">
                <SortBtn col="due_at">Due</SortBtn>
              </th>
              <th scope="col" className="px-4 py-2.5 font-medium">
                Actions
              </th>
            </tr>
          </thead>
          <tbody>
            {sorted.map((inv) => (
              <tr
                key={inv.id}
                className="border-t border-border transition hover:bg-muted/40"
              >
                <td className="px-4 py-2.5 font-mono text-xs">
                  {inv.invoice_number}
                </td>
                <td className="px-4 py-2.5 text-xs">
                  {inv.tenant_name ? (
                    <span title={inv.tenant_id}>{inv.tenant_name}</span>
                  ) : (
                    <span
                      className="font-mono text-muted-foreground"
                      title={inv.tenant_id}
                    >
                      {inv.tenant_id.slice(0, 8)}…
                    </span>
                  )}
                </td>
                <td className="px-4 py-2.5 text-xs text-muted-foreground">
                  {kindLabel(inv.invoice_kind)}
                </td>
                <td className="px-4 py-2.5 whitespace-nowrap text-xs text-muted-foreground">
                  {formatMytDate(inv.period_start)} –{" "}
                  {formatMytDate(inv.period_end)}
                </td>
                <td className="px-4 py-2.5 text-right font-mono tabular-nums font-medium">
                  {formatMyr(inv.total_myr)}
                </td>
                <td className="px-4 py-2.5">{statusBadge(inv.status)}</td>
                <td className="px-4 py-2.5 text-xs text-muted-foreground whitespace-nowrap">
                  {formatMytDate(inv.issued_at)}
                </td>
                <td className="px-4 py-2.5 text-xs text-muted-foreground whitespace-nowrap">
                  {formatMytDate(inv.due_at)}
                </td>
                <td className="px-4 py-2.5">
                  <div className="flex items-center gap-2">
                    <Link
                      href={`/billing/${inv.id}`}
                      className="text-xs text-primary underline-offset-2 hover:underline"
                    >
                      View
                    </Link>
                    {(inv.status === "issued" || inv.status === "overdue") && (
                      <MarkPaidButton invoiceId={inv.id} />
                    )}
                    <a
                      // Same-origin PDF streamer — a raw /api/admin/... href
                      // 404s on the admin origin (see adminRoutes.billing.invoicePdf).
                      href={adminRoutes.billing.invoicePdf(inv.id)}
                      target="_blank"
                      rel="noreferrer"
                      className="text-xs text-muted-foreground underline-offset-2 hover:underline hover:text-foreground"
                    >
                      PDF
                    </a>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  );
}
