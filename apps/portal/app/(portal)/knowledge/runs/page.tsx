/**
 * Knowledge — Runs tab (Phase 8 PR-6).
 *
 * Server Component. Same upstream as the Sources tab but presented as
 * a status-focused timeline of in-flight + recent ingestion requests
 * (including cancelled / failed). The Sources tab hides those — Runs
 * is where an operator goes to diagnose "why didn't my upload finish?"
 *
 * Per portal_design_prompt.md §3 "Special": do NOT fake real-time
 * updates. The desktop ingester polls + transitions status on its own
 * schedule; the portal just shows what's in the DB at request time.
 */

import { apiFetchServer } from "@/lib/api/server";
import { PolarisApiError } from "@/lib/api/errors";
import { TenantRoutes } from "@/lib/api/routes";
import { Badge } from "@/components/ui/badge";
import { formatAbsolute, formatRelative } from "@/lib/format";
import { cn } from "@/lib/utils";

interface IngestionRequestResponseDTO {
  id: string;
  source_type: "url" | "file" | "text";
  status:
    | "pending"
    | "processing"
    | "partial"
    | "completed"
    | "failed"
    | "cancelled";
  submitted_at: string;
  started_at: string | null;
  completed_at: string | null;
  chunks_added: number | null;
  chunks_skipped: number | null;
  chunks_deleted: number | null;
  error: { message?: string; code?: string } | null;
  eta: string;
  status_url: string;
}

const STATUS_TONE: Record<IngestionRequestResponseDTO["status"], string> = {
  pending: "bg-muted text-muted-foreground",
  processing: "bg-blue-100 text-blue-900 dark:bg-blue-950 dark:text-blue-100",
  partial: "bg-amber-100 text-amber-900 dark:bg-amber-950 dark:text-amber-100",
  completed:
    "bg-emerald-100 text-emerald-900 dark:bg-emerald-950 dark:text-emerald-100",
  failed: "bg-destructive/15 text-destructive dark:bg-destructive/20",
  cancelled: "bg-muted text-muted-foreground",
};

async function loadRuns(): Promise<{
  runs: IngestionRequestResponseDTO[];
  error?: string;
}> {
  try {
    const res = await apiFetchServer<IngestionRequestResponseDTO[]>(
      TenantRoutes.knowledgeRequests(50),
    );
    return { runs: res ?? [] };
  } catch (err) {
    const code =
      err instanceof PolarisApiError
        ? `${err.status} ${err.code}`
        : String(err);
    // eslint-disable-next-line no-console
    console.error(`[portal /knowledge/runs] fetch failed: ${code}`, err);
    return {
      runs: [],
      error: "We're having trouble loading ingestion runs.",
    };
  }
}

export default async function KnowledgeRunsPage() {
  const { runs, error } = await loadRuns();

  if (error) {
    return (
      <div
        role="status"
        className="rounded-lg border border-warning bg-warning/10 px-4 py-3 text-sm text-warning"
      >
        {error}
      </div>
    );
  }

  if (runs.length === 0) {
    return (
      <div className="rounded-xl border border-dashed bg-muted/30 p-8 text-center">
        <h2 className="text-base font-semibold">No ingestion runs yet</h2>
        <p className="mt-1 text-sm text-muted-foreground">
          Once you add a source, the ingestion run will appear here with status.
          Ingestion typically completes within 6 hours.
        </p>
      </div>
    );
  }

  return (
    <div className="overflow-hidden rounded-xl border bg-card">
      <table className="w-full text-sm">
        <caption className="sr-only">Recent ingestion runs</caption>
        <thead className="border-b bg-muted/40 text-left text-xs uppercase text-muted-foreground">
          <tr>
            <th scope="col" className="px-4 py-2.5 font-medium">
              Source type
            </th>
            <th scope="col" className="px-4 py-2.5 font-medium">
              Submitted
            </th>
            <th scope="col" className="px-4 py-2.5 font-medium">
              Status
            </th>
            <th scope="col" className="px-4 py-2.5 font-medium">
              Chunks
            </th>
            <th scope="col" className="px-4 py-2.5 font-medium">
              Notes
            </th>
          </tr>
        </thead>
        <tbody>
          {runs.map((r) => (
            <tr key={r.id} className="border-b last:border-b-0">
              <td className="px-4 py-3 capitalize">{r.source_type}</td>
              <td className="px-4 py-3 text-muted-foreground">
                <div>{formatRelative(r.submitted_at)}</div>
                <div className="text-[11px]">
                  {formatAbsolute(r.submitted_at)}
                </div>
              </td>
              <td className="px-4 py-3">
                <Badge
                  variant="outline"
                  className={cn(
                    "border-0 font-normal capitalize",
                    STATUS_TONE[r.status],
                  )}
                >
                  {r.status}
                </Badge>
              </td>
              <td className="px-4 py-3 tabular-nums text-muted-foreground">
                {r.chunks_added !== null ? (
                  <>
                    +{r.chunks_added}
                    {r.chunks_skipped ? ` · ${r.chunks_skipped} skipped` : null}
                    {r.chunks_deleted ? ` · ${r.chunks_deleted} removed` : null}
                  </>
                ) : (
                  "—"
                )}
              </td>
              <td className="px-4 py-3 text-muted-foreground">
                {r.error?.message ??
                  (r.status === "pending"
                    ? `ETA ${formatRelative(r.eta)}`
                    : "—")}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
