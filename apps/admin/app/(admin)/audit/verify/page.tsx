/**
 * Audit chain verification detail page (Phase 9 PR-5).
 *
 * Shows the latest chain verification result in detail, with broken-row
 * coordinates when status=broken. Links back to the main audit log.
 */

import Link from "next/link";
import { ArrowLeft } from "lucide-react";

import { adminServerFetch } from "@/lib/api/server";
import { adminRoutes } from "@/lib/api/routes";
import { ApiError } from "@/lib/api/error";
import { VerificationBanner } from "@/components/audit/VerificationBanner";
import { OnDemandReverify } from "@/components/audit/OnDemandReverify";
import type { VerificationResult } from "@/lib/impersonation/audit-helpers";

async function fetchLatestVerification(): Promise<VerificationResult | null> {
  try {
    const res = await adminServerFetch<VerificationResult>(
      adminRoutes.audit.latestVerification(),
    );
    return res ?? null;
  } catch (err) {
    if (err instanceof ApiError && err.status === 404) return null;
    // eslint-disable-next-line no-console
    console.error("[audit/verify page]:", err);
    return null;
  }
}

export default async function AuditVerifyPage() {
  const result = await fetchLatestVerification();

  return (
    <div className="mx-auto max-w-3xl space-y-6">
      <div className="flex items-center gap-2">
        <Link
          href="/audit"
          className="flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
        >
          <ArrowLeft className="h-4 w-4" aria-hidden />
          Audit log
        </Link>
      </div>

      <div>
        <h1 className="text-xl font-semibold tracking-tight">
          Chain Integrity Verification
        </h1>
        <p className="mt-1 text-sm text-muted-foreground">
          SHA-256 hash-chain verification across all tenant audit logs. Runs
          nightly at 04:00 MYT; trigger on-demand below.
        </p>
      </div>

      <VerificationBanner result={result} />

      {result && (
        <div className="rounded-md border border-border bg-background p-4 text-sm space-y-2">
          <h2 className="font-semibold">Last run details</h2>
          <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs">
            <dt className="text-muted-foreground">Status</dt>
            <dd className="font-mono">{result.status}</dd>
            <dt className="text-muted-foreground">Rows scanned</dt>
            <dd className="font-mono">
              {result.rows_scanned.toLocaleString()}
            </dd>
            <dt className="text-muted-foreground">Duration</dt>
            <dd className="font-mono">{result.duration_ms} ms</dd>
            <dt className="text-muted-foreground">Triggered by</dt>
            <dd className="font-mono">{result.triggered_by}</dd>
            {result.triggered_by_admin_id && (
              <>
                <dt className="text-muted-foreground">Admin ID</dt>
                <dd className="font-mono">{result.triggered_by_admin_id}</dd>
              </>
            )}
            {result.broken_at_id && (
              <>
                <dt className="text-muted-foreground">Broken at row</dt>
                <dd className="font-mono text-red-600 dark:text-red-400">
                  {result.broken_at_id}
                </dd>
                <dt className="text-muted-foreground">Broken prev hash</dt>
                <dd className="break-all font-mono text-xs text-muted-foreground">
                  {result.broken_prev_hash_hex ?? "—"}
                </dd>
                <dt className="text-muted-foreground">Actual hash</dt>
                <dd className="break-all font-mono text-xs text-muted-foreground">
                  {result.broken_actual_hash_hex ?? "—"}
                </dd>
              </>
            )}
          </dl>
        </div>
      )}

      <OnDemandReverify />
    </div>
  );
}
