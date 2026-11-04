/**
 * Audit log viewer page (Phase 9 PR-5).
 *
 * Server Component — fetches the first page of audit log entries and
 * the latest chain verification result from the Polaris API, then
 * hands them to client components for rendering + cursor pagination.
 */

import { adminServerFetch } from "@/lib/api/server";
import { adminRoutes } from "@/lib/api/routes";
import { ApiError } from "@/lib/api/error";
import { AuditTable } from "@/components/audit/AuditTable";
import { VerificationBanner } from "@/components/audit/VerificationBanner";
import { OnDemandReverify } from "@/components/audit/OnDemandReverify";
import type {
  AuditLogsResponse,
  VerificationResult,
} from "@/lib/impersonation/audit-helpers";

async function fetchAuditLogs(
  tenantId?: string,
): Promise<AuditLogsResponse | null> {
  try {
    const qs = tenantId ? `?tenant_id=${encodeURIComponent(tenantId)}` : "";
    const res = await adminServerFetch<AuditLogsResponse>(
      `${adminRoutes.audit.logs()}${qs}`,
    );
    return res ?? null;
  } catch (err) {
    if (err instanceof ApiError && err.status === 404) return null;
    // eslint-disable-next-line no-console
    console.error("[audit page] fetchAuditLogs:", err);
    return null;
  }
}

async function fetchLatestVerification(): Promise<VerificationResult | null> {
  try {
    const res = await adminServerFetch<VerificationResult>(
      adminRoutes.audit.latestVerification(),
    );
    return res ?? null;
  } catch (err) {
    if (err instanceof ApiError && err.status === 404) return null;
    // eslint-disable-next-line no-console
    console.error("[audit page] fetchLatestVerification:", err);
    return null;
  }
}

export default async function AuditPage({
  searchParams,
}: {
  searchParams?: { tenant_id?: string };
}) {
  const tenantId = searchParams?.tenant_id;

  const [logsData, verificationData] = await Promise.all([
    fetchAuditLogs(tenantId),
    fetchLatestVerification(),
  ]);

  return (
    <div className="mx-auto max-w-7xl space-y-6">
      <div className="flex items-end justify-between">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">Audit Log</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Hash-chained tamper-evident log of all platform writes.
            {tenantId && (
              <span className="ml-2 font-mono text-xs">
                Filtered: tenant {tenantId}
              </span>
            )}
          </p>
        </div>
        <OnDemandReverify />
      </div>

      <VerificationBanner result={verificationData} />

      <AuditTable
        initialItems={logsData?.items ?? []}
        initialNextCursor={logsData?.next_cursor ?? null}
        tenantId={tenantId}
      />
    </div>
  );
}
