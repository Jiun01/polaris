/**
 * Invoice detail page (Phase 9 PR-7).
 *
 * Server component. Fetches invoice list and finds the one matching the
 * invoice_id param. Shows full details + MarkPaidButton + PDF download.
 */

import Link from "next/link";
import { notFound } from "next/navigation";
import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { adminServerFetch } from "@/lib/api/server";
import { adminRoutes } from "@/lib/api/routes";
import { ApiError } from "@/lib/api/error";
import { MarkPaidButton } from "@/components/billing/MarkPaidButton";
import { VoidButton } from "@/components/billing/VoidButton";
import { formatMyr, formatMytDate, formatMyt } from "@/lib/format";
import type { TenantInvoice } from "@/lib/types/billing";

interface InvoicesResponse {
  items: TenantInvoice[];
  total: number;
}

async function fetchInvoice(invoiceId: string): Promise<TenantInvoice | null> {
  try {
    const res = await adminServerFetch<InvoicesResponse>(
      adminRoutes.billing.invoices() + "?id=" + encodeURIComponent(invoiceId),
    );
    if (!res) return null;
    return res.items.find((i) => i.id === invoiceId) ?? null;
  } catch (err) {
    if (err instanceof ApiError && (err.status === 404 || err.status === 501))
      return null;
    throw err;
  }
}

function StatusBadge({ status }: { status: TenantInvoice["status"] }) {
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

export default async function InvoiceDetailPage({
  params,
}: {
  params: { invoice_id: string };
}) {
  const invoice = await fetchInvoice(params.invoice_id);
  if (!invoice) notFound();

  return (
    <div className="mx-auto max-w-3xl space-y-6">
      <Link
        href="/billing"
        className="text-xs text-muted-foreground hover:text-foreground hover:underline"
      >
        Back to Billing
      </Link>

      <div className="flex items-start justify-between">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">
            {invoice.invoice_number}
          </h1>
          <p className="mt-1 text-sm text-muted-foreground">Invoice detail</p>
        </div>
        <div className="flex items-center gap-2">
          {(invoice.status === "issued" || invoice.status === "overdue") && (
            <>
              <MarkPaidButton invoiceId={invoice.id} />
              <VoidButton invoiceId={invoice.id} />
            </>
          )}
          <a
            href={adminRoutes.billing.invoicePdf(invoice.id)}
            target="_blank"
            rel="noreferrer"
          >
            <Button variant="outline" size="sm">
              Download PDF
            </Button>
          </a>
        </div>
      </div>

      <Card className="divide-y divide-border text-sm">
        {[
          ["Status", <StatusBadge key="s" status={invoice.status} />],
          ["Invoice #", invoice.invoice_number],
          [
            "Tenant",
            invoice.tenant_name ? (
              <span key="t" className="text-right">
                {invoice.tenant_name}
                <span className="block font-mono text-[10px] text-muted-foreground normal-case">
                  {invoice.tenant_id}
                </span>
              </span>
            ) : (
              <span key="t" className="font-mono text-xs">
                {invoice.tenant_id}
              </span>
            ),
          ],
          ["Kind", invoice.invoice_kind.replace(/_/g, " ")],
          [
            "Period",
            formatMytDate(invoice.period_start) +
              " – " +
              formatMytDate(invoice.period_end),
          ],
          ["Subtotal", formatMyr(invoice.subtotal_myr)],
          ["Tax", formatMyr(invoice.tax_myr)],
          [
            "Total",
            <span key="tot" className="font-semibold font-mono">
              {formatMyr(invoice.total_myr)}
            </span>,
          ],
          ["Issued at", formatMyt(invoice.issued_at)],
          ["Due at", formatMytDate(invoice.due_at)],
          [
            "Paid at",
            invoice.paid_at ? (
              formatMyt(invoice.paid_at)
            ) : (
              <span key="p" className="text-muted-foreground">
                —
              </span>
            ),
          ],
        ].map(([label, value]) => (
          <div key={String(label)} className="flex justify-between px-4 py-2.5">
            <span className="text-muted-foreground text-xs uppercase tracking-wider">
              {label}
            </span>
            <span className="font-medium capitalize">{value}</span>
          </div>
        ))}
      </Card>
    </div>
  );
}
