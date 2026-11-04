/**
 * Chain-integrity verification status banner (Phase 9 PR-5).
 *
 * Displays the result of the latest audit_chain_verifications run.
 * Green = intact, red = broken (investigation needed), yellow = errored.
 */

import type { VerificationResult } from "@/lib/impersonation/audit-helpers";

interface Props {
  result: VerificationResult | null;
}

function fmtDate(iso: string): string {
  try {
    return new Date(iso).toLocaleString("en-MY", {
      dateStyle: "short",
      timeStyle: "medium",
    });
  } catch {
    return iso;
  }
}

export function VerificationBanner({ result }: Props) {
  if (!result) {
    return (
      <div
        data-testid="verification-banner"
        role="status"
        className="rounded-md border border-border bg-muted/40 px-4 py-3 text-sm text-muted-foreground"
      >
        No audit chain verification has been run yet. Use the button below to
        trigger an on-demand check.
      </div>
    );
  }

  const colors = {
    intact:
      "border-emerald-300 bg-emerald-50 text-emerald-800 dark:border-emerald-700 dark:bg-emerald-950/30 dark:text-emerald-300",
    broken:
      "border-red-300 bg-red-50 text-red-800 dark:border-red-700 dark:bg-red-950/30 dark:text-red-300",
    errored:
      "border-amber-300 bg-amber-50 text-amber-800 dark:border-amber-700 dark:bg-amber-950/30 dark:text-amber-300",
  } as const;

  const labels = {
    intact: "Chain intact",
    broken: "Chain BROKEN — investigate immediately",
    errored: "Verification error — re-run or check API logs",
  } as const;

  return (
    <div
      data-testid="verification-banner"
      role={result.status === "broken" ? "alert" : "status"}
      className={`rounded-md border px-4 py-3 text-sm ${colors[result.status]}`}
    >
      <div className="flex items-start justify-between gap-4">
        <div>
          <p className="font-semibold">
            <span className="sr-only">
              {result.status === "broken" ? "[BROKEN] " : ""}
            </span>
            {labels[result.status]}
          </p>
          <p className="mt-0.5 text-xs opacity-80">
            Last checked {fmtDate(result.checked_at)} &middot;{" "}
            {result.rows_scanned.toLocaleString()} rows &middot;{" "}
            {result.duration_ms} ms &middot; triggered by{" "}
            {result.triggered_by === "scheduled" ? "scheduler" : "admin"}
          </p>
          {result.status === "broken" && result.broken_at_id && (
            <p className="mt-1.5 rounded bg-red-100/60 px-2 py-1 font-mono text-xs dark:bg-red-900/30">
              Broken at row {result.broken_at_id}
              {result.broken_at_created_at
                ? ` (${fmtDate(result.broken_at_created_at)})`
                : ""}
            </p>
          )}
        </div>
      </div>
    </div>
  );
}
