/**
 * Issue invoice page (Phase 9 PR-7).
 *
 * Server component wrapping the IssueInvoiceForm client component.
 * Pre-populates tenant_id from the route param.
 */

import Link from "next/link";
import { IssueInvoiceForm } from "@/components/billing/IssueInvoiceForm";

export default function NewInvoicePage({
  params,
}: {
  params: { tenant_id: string };
}) {
  return (
    <div className="mx-auto max-w-2xl space-y-6">
      <Link
        href="/billing"
        className="text-xs text-muted-foreground hover:text-foreground hover:underline"
      >
        Back to Billing
      </Link>
      <div>
        <h1 className="text-xl font-semibold tracking-tight">Issue invoice</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Create a new invoice for tenant{" "}
          <span className="font-mono text-xs">{params.tenant_id}</span>.
        </p>
      </div>
      <IssueInvoiceForm tenantId={params.tenant_id} />
    </div>
  );
}
