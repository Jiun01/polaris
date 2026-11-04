/**
 * Cursor-paginated wrapper around InvoiceTable (frontend-chips-followup).
 *
 * Mirrors AuditTable's load-more pattern: the server page passes the first
 * page + next_cursor; this component owns the accumulated rows and appends
 * further pages via adminFetch. InvoiceTable itself stays presentational
 * (sort-only), receiving the accumulated array.
 *
 * `filterQs` carries the active tab's filter (status / year_month) so every
 * load-more request stays inside the same filter view — switching tabs is a
 * full navigation (new searchParams), which remounts this component with a
 * fresh first page.
 */

"use client";

import { adminFetch } from "@/lib/api/client";
import { adminRoutes } from "@/lib/api/routes";
import { InvoiceTable } from "@/components/billing/InvoiceTable";
import { useCursorLoadMore } from "@/lib/hooks/useCursorLoadMore";
import type { TenantInvoice } from "@/lib/types/billing";

interface InvoicesResponse {
  items: TenantInvoice[];
  total: number;
  next_cursor: string | null;
}

interface Props {
  initialItems: TenantInvoice[];
  initialNextCursor: string | null;
  /** Pre-built query string for the active filter (may be empty). */
  filterQs: string;
}

export function InvoiceTableWithLoadMore({
  initialItems,
  initialNextCursor,
  filterQs,
}: Props) {
  const { items, loading, hasMore, loadMore } =
    useCursorLoadMore<TenantInvoice>(
      initialItems,
      initialNextCursor,
      async (cursor) => {
        const params = new URLSearchParams(filterQs);
        params.set("cursor", cursor);
        const data = await adminFetch<InvoicesResponse>(
          `${adminRoutes.billing.invoices()}?${params.toString()}`,
        );
        return data
          ? { items: data.items, nextCursor: data.next_cursor }
          : null;
      },
    );

  return (
    <div className="space-y-2">
      <InvoiceTable items={items} />
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
