"use client";

/**
 * Sources table for the Knowledge screen.
 *
 * Renders one row per KnowledgeSource (the new declarative desired-state
 * model). Each row shows the source label + type badge, status, chunk count
 * (for synced rows), last-synced time, and a delete (×) button.
 *
 * Delete → DELETE /api/tenant/knowledge/sources/{id} → router.refresh().
 * A per-row confirm prompt and pending state are shown while the request
 * is in flight. PolarisApiError is caught and surfaced inline.
 *
 * Client Component: the delete action mutates state and calls router.refresh().
 */

import * as React from "react";
import { useRouter } from "next/navigation";
import { FileText, Globe, Pencil, X } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { formatRelative } from "@/lib/format";
import { apiFetch } from "@/lib/api/client";
import { PolarisApiError } from "@/lib/api/errors";
import { TenantRoutes } from "@/lib/api/routes";
import { cn } from "@/lib/utils";

// ---------------------------------------------------------------------------
// Local interface — mirrors KnowledgeSourceResponse from the backend.
// Defined inline per portal convention (e.g. SignUploadResponse in the dialog).
// ---------------------------------------------------------------------------
export interface KnowledgeSource {
  id: string;
  tenant_id: string;
  tenant_branch_id: string | null;
  source_type: "url" | "file" | "text";
  source_url: string | null;
  storage_path: string | null;
  source_label: string;
  status: "pending" | "syncing" | "synced" | "failed";
  chunk_count: number;
  last_synced_at: string | null;
  created_at: string;
  updated_at: string;
}

// ---------------------------------------------------------------------------
// Type metadata (icon + label badge)
// ---------------------------------------------------------------------------
const TYPE_META: Record<
  KnowledgeSource["source_type"],
  { Icon: React.ComponentType<{ className?: string }>; label: string }
> = {
  url: { Icon: Globe, label: "Web" },
  file: { Icon: FileText, label: "Document" },
  text: { Icon: Pencil, label: "Text" },
};

// ---------------------------------------------------------------------------
// Status styling
// ---------------------------------------------------------------------------
const STATUS_TONE: Record<KnowledgeSource["status"], string> = {
  pending: "bg-muted text-muted-foreground",
  syncing: "bg-blue-100 text-blue-900 dark:bg-blue-950 dark:text-blue-100",
  synced:
    "bg-emerald-100 text-emerald-900 dark:bg-emerald-950 dark:text-emerald-100",
  failed: "bg-destructive/15 text-destructive",
};

// ---------------------------------------------------------------------------
// Delete button — per-row client action with confirm + pending state
// ---------------------------------------------------------------------------
function DeleteSourceButton({ id }: { id: string }) {
  const router = useRouter();
  const [confirming, setConfirming] = React.useState(false);
  const [deleting, setDeleting] = React.useState(false);
  const [deleteError, setDeleteError] = React.useState<string | null>(null);

  async function handleDelete() {
    if (!confirming) {
      setConfirming(true);
      return;
    }
    setDeleting(true);
    setDeleteError(null);
    try {
      await apiFetch<void>(TenantRoutes.knowledgeSource(id), {
        method: "DELETE",
      });
      router.refresh();
    } catch (err) {
      setDeleteError(
        err instanceof PolarisApiError
          ? err.message
          : "Delete failed. Try again.",
      );
      setConfirming(false);
    } finally {
      setDeleting(false);
    }
  }

  if (deleteError) {
    return (
      <span className="text-xs text-destructive" title={deleteError}>
        {deleteError}
      </span>
    );
  }

  if (confirming) {
    return (
      <div className="flex items-center gap-1">
        <span className="text-xs text-muted-foreground whitespace-nowrap">
          Remove?
        </span>
        <Button
          variant="destructive"
          size="sm"
          onClick={handleDelete}
          disabled={deleting}
          className="h-6 px-2 text-xs"
        >
          {deleting ? "…" : "Yes"}
        </Button>
        <Button
          variant="ghost"
          size="sm"
          onClick={() => setConfirming(false)}
          disabled={deleting}
          className="h-6 px-2 text-xs text-muted-foreground"
        >
          No
        </Button>
      </div>
    );
  }

  return (
    <Button
      variant="ghost"
      size="sm"
      onClick={handleDelete}
      aria-label="Remove knowledge source"
      title="Remove this source? Its knowledge will be deleted on the next sync."
      className="h-7 w-7 p-0 text-muted-foreground hover:text-destructive"
    >
      <X className="h-3.5 w-3.5" aria-hidden />
    </Button>
  );
}

// ---------------------------------------------------------------------------
// Main table
// ---------------------------------------------------------------------------
export function KnowledgeSourcesTable({
  sources,
}: {
  sources: KnowledgeSource[];
}) {
  return (
    <div className="overflow-hidden rounded-xl border bg-card">
      <table className="w-full text-sm">
        <caption className="sr-only">Knowledge sources</caption>
        <thead className="border-b bg-muted/40 text-left text-xs uppercase text-muted-foreground">
          <tr>
            <th scope="col" className="px-4 py-2.5 font-medium">
              Label
            </th>
            <th scope="col" className="px-4 py-2.5 font-medium">
              Status
            </th>
            <th scope="col" className="px-4 py-2.5 text-right font-medium">
              Chunks
            </th>
            <th scope="col" className="px-4 py-2.5 font-medium">
              Last synced
            </th>
            <th scope="col" className="px-4 py-2.5 font-medium">
              <span className="sr-only">Actions</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {sources.map((s) => {
            const { Icon, label: typeLabel } = TYPE_META[s.source_type];
            return (
              <tr
                key={s.id}
                className="border-b last:border-b-0 hover:bg-accent/30"
              >
                {/* Label + type badge */}
                <td className="px-4 py-3">
                  <div className="flex items-center gap-2">
                    <Icon
                      className="h-4 w-4 shrink-0 text-muted-foreground"
                      aria-hidden
                    />
                    <span className="font-medium">{s.source_label}</span>
                    <Badge
                      variant="outline"
                      className="border-0 bg-muted px-1.5 py-0 text-xs font-normal text-muted-foreground"
                    >
                      {typeLabel}
                    </Badge>
                  </div>
                </td>

                {/* Status */}
                <td className="px-4 py-3">
                  <Badge
                    variant="outline"
                    className={cn(
                      "border-0 font-normal capitalize",
                      STATUS_TONE[s.status],
                    )}
                  >
                    {s.status}
                  </Badge>
                </td>

                {/* Chunk count — only meaningful when synced */}
                <td className="px-4 py-3 text-right tabular-nums">
                  {s.status === "synced" ? s.chunk_count : "—"}
                </td>

                {/* Last synced */}
                <td className="px-4 py-3 text-muted-foreground">
                  {s.last_synced_at ? formatRelative(s.last_synced_at) : "—"}
                </td>

                {/* Actions */}
                <td className="px-4 py-3">
                  <DeleteSourceButton id={s.id} />
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
