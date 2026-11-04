/**
 * Shared cursor-pagination state machine (extracted from AuditTable +
 * InvoiceTableWithLoadMore, which each hand-rolled the same
 * items/cursor/loading trio and concurrent-fetch guard).
 *
 * The hook owns ONLY the generic bits: accumulated items, the next cursor,
 * the loading flag, and the double-click / concurrent-fetch guard. URL
 * construction (endpoint, filter querystring, cursor param name) stays with
 * the caller via the `fetchPage` callback — each consumer keeps its own
 * request shape unchanged, just relocated into that callback.
 */

import { useState } from "react";

export interface CursorPage<T> {
  items: T[];
  nextCursor: string | null;
}

export interface UseCursorLoadMoreResult<T> {
  items: T[];
  loading: boolean;
  hasMore: boolean;
  loadMore: () => Promise<void>;
  reset: (items: T[], nextCursor: string | null) => void;
}

export function useCursorLoadMore<T>(
  initialItems: T[],
  initialNextCursor: string | null,
  fetchPage: (cursor: string) => Promise<CursorPage<T> | null | undefined>,
): UseCursorLoadMoreResult<T> {
  const [items, setItems] = useState<T[]>(initialItems);
  const [cursor, setCursor] = useState<string | null>(initialNextCursor);
  const [loading, setLoading] = useState(false);

  async function loadMore() {
    if (!cursor || loading) return;
    setLoading(true);
    try {
      const page = await fetchPage(cursor);
      if (page) {
        setItems((prev) => [...prev, ...page.items]);
        setCursor(page.nextCursor);
      }
    } finally {
      setLoading(false);
    }
  }

  function reset(nextItems: T[], nextCursor: string | null) {
    setItems(nextItems);
    setCursor(nextCursor);
  }

  return { items, loading, hasMore: !!cursor, loadMore, reset };
}
