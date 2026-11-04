/**
 * Billing list page (Phase 9 PR-7).
 *
 * Server component. Fetches invoice list from GET /api/admin/billing/invoices.
 * Tab bar: All / Overdue / This Month (via searchParams).
 * Renders InvoiceTable + ArchiveMonthPicker.
 */

import Link from "next/link";
import { Suspense } from "react";
import { Card } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { adminServerFetch } from "@/lib/api/server";
import { adminRoutes } from "@/lib/api/routes";
import { ApiError } from "@/lib/api/error";
import { InvoiceTableWithLoadMore } from "@/components/billing/InvoiceTableWithLoadMore";
import { ArchiveMonthPicker } from "@/components/billing/ArchiveMonthPicker";
import type { TenantInvoice } from "@/lib/types/billing";

type TabKey = "all" | "overdue" | "this_month";

const TABS: { key: TabKey; label: string }[] = [
  { key: "all", label: "All" },
  { key: "overdue", label: "Overdue" },
  { key: "this_month", label: "This Month" },
];

interface InvoicesResponse {
  items: TenantInvoice[];
  total: number;
  next_cursor: string | null;
}

/** Active-tab filter query string — shared by the first-page fetch and every
 *  client-side load-more request (InvoiceTableWithLoadMore re-sends it). */
function buildFilterQs(tab: TabKey, yearMonth?: string): string {
  const qs = new URLSearchParams();
  if (tab === "overdue") qs.set("status", "overdue");
  // API filters by ?year_month=YYYY-MM (column `period`), not ?period=.
  if (tab === "this_month") {
    const now = new Date();
    qs.set("year_month", now.toISOString().slice(0, 7));
  }
  if (yearMonth && /^\d{4}-\d{2}$/.test(yearMonth)) {
    qs.set("year_month", yearMonth);
  }
  return qs.toString();
}

const EMPTY_RESPONSE: InvoicesResponse = {
  items: [],
  total: 0,
  next_cursor: null,
};

async function fetchInvoices(
  filterQs: string,
): Promise<InvoicesResponse | { error: string }> {
  try {
    const path = filterQs
      ? adminRoutes.billing.invoices() + "?" + filterQs
      : adminRoutes.billing.invoices();
    const res = await adminServerFetch<InvoicesResponse>(path);
    return res ?? EMPTY_RESPONSE;
  } catch (err) {
    if (err instanceof ApiError) {
      if (err.status === 404 || err.status === 501) return EMPTY_RESPONSE;
      return { error: err.message };
    }
    throw err;
  }
}

export default async function BillingPage({
  searchParams,
}: {
  searchParams?: { tab?: string; year_month?: string };
}) {
  const tab = (searchParams?.tab ?? "all") as TabKey;
  const validTab = TABS.some((t) => t.key === tab) ? tab : "all";
  const filterQs = buildFilterQs(validTab, searchParams?.year_month);
  const data = await fetchInvoices(filterQs);

  return (
    <div className="mx-auto max-w-7xl space-y-6">
      <div className="flex items-end justify-between">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">Billing</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Invoice management — issue, mark paid, and download PDFs.
          </p>
        </div>
        <div className="flex items-center gap-3">
          <Suspense fallback={null}>
            <ArchiveMonthPicker />
          </Suspense>
          <Link href="/billing/new/select">
            <Button size="sm">Issue invoice</Button>
          </Link>
        </div>
      </div>

      <div className="flex gap-1 border-b border-border">
        {TABS.map((t) => (
          <Link
            key={t.key}
            href={t.key === "all" ? "/billing" : "/billing?tab=" + t.key}
            className={[
              "px-4 py-2 text-sm font-medium transition-colors",
              validTab === t.key
                ? "border-b-2 border-primary text-foreground"
                : "text-muted-foreground hover:text-foreground",
            ].join(" ")}
          >
            {t.label}
          </Link>
        ))}
      </div>

      {"error" in data ? (
        <Card className="border-destructive/30 bg-destructive/5 p-4 text-sm text-destructive">
          Failed to load invoices: {data.error}
        </Card>
      ) : (
        <InvoiceTableWithLoadMore
          initialItems={data.items}
          initialNextCursor={data.next_cursor ?? null}
          filterQs={filterQs}
        />
      )}
    </div>
  );
}
