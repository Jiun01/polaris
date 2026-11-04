/**
 * Knowledge — Chunks drill-down (Phase 8 PR-6).
 *
 * Server Component. Looks up one ingestion request by ID and surfaces
 * its run metadata as a stand-in for the (deferred) per-chunk inspector
 * — `knowledge_chunks` has no portal-side listing endpoint at GA
 * because the use case is rare (per portal_design_prompt.md §3
 * "Special") and emitting chunk text would balloon payloads.
 *
 * When a deeper chunk inspector lands in v3.0+, this route is the
 * mount point. For GA we render the run header + a placeholder that
 * keeps the navigation contract intact (the Sources table links here).
 */

import Link from "next/link";
import { notFound } from "next/navigation";
import { ArrowLeft } from "lucide-react";

import { apiFetchServer } from "@/lib/api/server";
import { PolarisApiError } from "@/lib/api/errors";
import { TenantRoutes } from "@/lib/api/routes";
import { Badge } from "@/components/ui/badge";
import { formatAbsolute } from "@/lib/format";

interface IngestionRequestResponseDTO {
  id: string;
  source_type: "url" | "file" | "text";
  ingest_mode: "add" | "sync" | "replace";
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
  error: { message?: string } | null;
}

async function loadRequest(
  id: string,
): Promise<IngestionRequestResponseDTO | null> {
  try {
    const res = await apiFetchServer<IngestionRequestResponseDTO>(
      TenantRoutes.knowledgeRequest(id),
    );
    return res ?? null;
  } catch (err) {
    if (err instanceof PolarisApiError && err.status === 404) {
      return null;
    }
    // eslint-disable-next-line no-console
    console.error(`[portal /knowledge/chunks/${id}] fetch failed`, err);
    throw err;
  }
}

export default async function KnowledgeChunksPage({
  params,
}: {
  params: { doc_id: string };
}) {
  const request = await loadRequest(params.doc_id);
  if (!request) {
    notFound();
  }

  return (
    <div className="space-y-6">
      <div>
        <Link
          href="/knowledge"
          className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
        >
          <ArrowLeft className="h-3.5 w-3.5" aria-hidden />
          All sources
        </Link>
      </div>

      <header className="space-y-2">
        <div className="flex flex-wrap items-center gap-2">
          <h2 className="text-xl font-semibold">Source details</h2>
          <Badge variant="outline" className="font-normal capitalize">
            {request.source_type}
          </Badge>
          <Badge variant="outline" className="font-normal capitalize">
            {request.status}
          </Badge>
        </div>
        <p className="text-sm text-muted-foreground">
          Submitted {formatAbsolute(request.submitted_at)}
          {request.completed_at
            ? ` · completed ${formatAbsolute(request.completed_at)}`
            : ""}
        </p>
      </header>

      <dl className="grid grid-cols-1 gap-4 rounded-xl border bg-card p-5 sm:grid-cols-3">
        <div>
          <dt className="text-xs uppercase text-muted-foreground">
            Chunks added
          </dt>
          <dd className="mt-1 text-2xl font-semibold tabular-nums">
            {request.chunks_added ?? "—"}
          </dd>
        </div>
        <div>
          <dt className="text-xs uppercase text-muted-foreground">
            Chunks skipped
          </dt>
          <dd className="mt-1 text-2xl font-semibold tabular-nums">
            {request.chunks_skipped ?? "—"}
          </dd>
        </div>
        <div>
          <dt className="text-xs uppercase text-muted-foreground">
            Chunks removed
          </dt>
          <dd className="mt-1 text-2xl font-semibold tabular-nums">
            {request.chunks_deleted ?? "—"}
          </dd>
        </div>
      </dl>

      {request.error?.message ? (
        <div
          role="status"
          className="rounded-lg border border-destructive/40 bg-destructive/5 px-4 py-3 text-sm text-destructive"
        >
          <strong className="font-semibold">Ingestion error: </strong>
          {request.error.message}
        </div>
      ) : null}

      <div className="rounded-xl border border-dashed bg-muted/20 p-6 text-sm text-muted-foreground">
        Per-chunk inspection (text preview, embedding vector, retrieval scores)
        is an advanced surface scheduled for a later release. At GA the
        run-level counters above tell you whether ingestion succeeded. Contact
        support if you need to inspect specific chunks.
      </div>
    </div>
  );
}
