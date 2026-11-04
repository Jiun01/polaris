/**
 * Audit log table component (Phase 9 PR-5).
 *
 * Renders a list of AuditLogItem rows in a dense scrollable table.
 * Highlights impersonated writes with a subtle ring colour.
 */

"use client";

import { adminFetch } from "@/lib/api/client";
import { adminRoutes } from "@/lib/api/routes";
import { useCursorLoadMore } from "@/lib/hooks/useCursorLoadMore";
import type {
  AuditLogItem,
  AuditLogsResponse,
} from "@/lib/impersonation/audit-helpers";

interface Props {
  initialItems: AuditLogItem[];
  initialNextCursor: string | null;
  tenantId?: string;
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

export function AuditTable({
  initialItems,
  initialNextCursor,
  tenantId,
}: Props) {
  const { items, loading, hasMore, loadMore } = useCursorLoadMore<AuditLogItem>(
    initialItems,
    initialNextCursor,
    async (cursor) => {
      const params = new URLSearchParams();
      params.set("cursor", cursor);
      if (tenantId) params.set("tenant_id", tenantId);
      const data = await adminFetch<AuditLogsResponse>(
        `${adminRoutes.audit.logs()}?${params.toString()}`,
      );
      return data ? { items: data.items, nextCursor: data.next_cursor } : null;
    },
  );

  return (
    <div className="space-y-2">
      <div className="overflow-x-auto rounded-md border border-border">
        <table className="w-full text-xs">
          <thead className="border-b border-border bg-muted/40">
            <tr>
              <th
                scope="col"
                className="px-3 py-2 text-left font-semibold text-muted-foreground"
              >
                Time
              </th>
              <th
                scope="col"
                className="px-3 py-2 text-left font-semibold text-muted-foreground"
              >
                Action
              </th>
              <th
                scope="col"
                className="px-3 py-2 text-left font-semibold text-muted-foreground"
              >
                Actor
              </th>
              <th
                scope="col"
                className="px-3 py-2 text-left font-semibold text-muted-foreground"
              >
                Impersonating
              </th>
              <th
                scope="col"
                className="px-3 py-2 text-left font-semibold text-muted-foreground"
              >
                Tenant
              </th>
            </tr>
          </thead>
          <tbody>
            {items.length === 0 && (
              <tr>
                <td
                  colSpan={5}
                  className="px-3 py-6 text-center text-muted-foreground"
                >
                  No audit log entries found.
                </td>
              </tr>
            )}
            {items.map((item) => (
              <tr
                key={item.id}
                aria-label={
                  item.impersonating_id ? "Impersonated action" : undefined
                }
                className={
                  item.impersonating_id
                    ? "border-b border-border bg-amber-50/40 dark:bg-amber-900/10"
                    : "border-b border-border hover:bg-muted/20"
                }
              >
                <td className="whitespace-nowrap px-3 py-2 font-mono text-muted-foreground">
                  {fmtDate(item.created_at)}
                </td>
                <td className="px-3 py-2 font-mono">{item.action}</td>
                <td className="px-3 py-2 font-mono text-muted-foreground">
                  {item.actor ? item.actor.slice(0, 8) + "…" : "—"}
                </td>
                <td className="px-3 py-2 font-mono text-amber-700 dark:text-amber-400">
                  {item.impersonating_id
                    ? `${item.impersonating_kind ?? "?"} / ${item.impersonating_id.slice(0, 8)}…`
                    : "—"}
                </td>
                <td className="px-3 py-2 font-mono text-muted-foreground">
                  {item.tenant_id.slice(0, 8)}…
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {hasMore && (
        <button
          onClick={loadMore}
          disabled={loading}
          className="w-full rounded-md border border-border py-1.5 text-xs text-muted-foreground hover:bg-muted disabled:opacity-50"
        >
          {loading ? "Loading…" : "Load more"}
        </button>
      )}
    </div>
  );
}
