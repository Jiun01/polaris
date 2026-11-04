/**
 * Plan-change request form — Screen #13 sub-page (Phase 8 PR-10).
 *
 * Server Component shell hands off to a client form. POSTs to
 * `/api/tenant/plan-change-requests/` which writes a pending row;
 * Phase 10 admin approves at GA (operator-manual flip until then).
 */

import Link from "next/link";
import { ArrowLeft } from "lucide-react";

import { PlanChangeForm } from "@/components/billing/PlanChangeForm";

export default function PlanChangePage({
  searchParams,
}: {
  searchParams: Record<string, string | string[] | undefined>;
}) {
  const target =
    typeof searchParams.target === "string" ? searchParams.target : "pro";
  return (
    <div className="space-y-6">
      <div>
        <Link
          href="/billing"
          className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
        >
          <ArrowLeft className="h-3.5 w-3.5" aria-hidden />
          Billing
        </Link>
      </div>

      <header className="space-y-2">
        <h1 className="text-3xl font-semibold tracking-tight">Change plan</h1>
        <p className="text-sm text-muted-foreground">
          Plan changes are admin-approved at GA. You&apos;ll get a notification
          when your request goes through (typically within one business day).
        </p>
      </header>

      <PlanChangeForm initialTarget={target} />
    </div>
  );
}
