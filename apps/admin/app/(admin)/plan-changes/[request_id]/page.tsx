/**
 * Plan change detail page (Phase 9 PR-8).
 * Server component. Fetches the request and renders ProrationRecomputeForm.
 */

import Link from "next/link";
import { notFound } from "next/navigation";
import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { adminServerFetch } from "@/lib/api/server";
import { adminRoutes } from "@/lib/api/routes";
import { ApiError } from "@/lib/api/error";
import { ProrationRecomputeForm } from "@/components/plan-changes/ProrationRecomputeForm";
import { formatMytDate } from "@/lib/format";
import type { PlanChangeRequest } from "@/lib/types/billing";

interface PlanChangesResponse {
  items: PlanChangeRequest[];
  total: number;
}

async function fetchRequest(
  requestId: string,
): Promise<PlanChangeRequest | null> {
  try {
    // Use the individual GET endpoint so the page keeps working after
    // approval/rejection (the pending list only returns status=pending rows).
    return (
      (await adminServerFetch<PlanChangeRequest>(
        adminRoutes.planChanges.get(requestId),
      )) ?? null
    );
  } catch (err) {
    if (err instanceof ApiError && (err.status === 404 || err.status === 501))
      return null;
    throw err;
  }
}

function statusVariant(status: PlanChangeRequest["status"]) {
  if (status === "approved") return "secondary";
  if (status === "rejected") return "destructive";
  return "default";
}

export default async function PlanChangeDetailPage({
  params,
}: {
  params: { request_id: string };
}) {
  const req = await fetchRequest(params.request_id);
  if (!req) notFound();

  return (
    <div className="mx-auto max-w-3xl space-y-6">
      <Link
        href="/plan-changes"
        className="text-xs text-muted-foreground hover:text-foreground hover:underline"
      >
        Back to Plan Changes
      </Link>

      <div className="flex items-start justify-between">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">
            Review plan change
          </h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Review and approve or reject the tenant tier-change request.
          </p>
        </div>
        <Badge variant={statusVariant(req.status)} className="capitalize">
          {req.status}
        </Badge>
      </div>

      <Card className="divide-y divide-border text-sm">
        {[
          ["Tenant", req.tenant_name ?? req.tenant_id],
          [
            "Tenant ID",
            <span key="tid" className="font-mono text-xs">
              {req.tenant_id}
            </span>,
          ],
          [
            "Current tier",
            <span key="ct" className="capitalize">
              {req.current_tier}
            </span>,
          ],
          [
            "Target tier",
            <span key="tt" className="capitalize font-semibold">
              {req.target_tier}
            </span>,
          ],
          [
            "Billing cycle",
            req.current_billing_cycle !== req.target_billing_cycle
              ? req.current_billing_cycle + " → " + req.target_billing_cycle
              : req.target_billing_cycle,
          ],
          ["Requested at", formatMytDate(req.requested_at)],
        ].map(([label, value]) => (
          <div key={String(label)} className="flex justify-between px-4 py-2.5">
            <span className="text-muted-foreground text-xs uppercase tracking-wider">
              {label}
            </span>
            <span className="font-medium">{value}</span>
          </div>
        ))}
      </Card>

      <ProrationRecomputeForm request={req} />
    </div>
  );
}
