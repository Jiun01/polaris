/**
 * Website leads viewer page.
 *
 * Server Component — fetches the first page of NEW leads (the default "All"
 * inbox) from the Polaris API and hands them to the client table for chip
 * filtering, mark-contacted, cursor pagination, and CSV export.
 */

import { adminServerFetch } from "@/lib/api/server";
import { adminRoutes } from "@/lib/api/routes";
import { ApiError } from "@/lib/api/error";
import { LeadsTable } from "@/components/leads/LeadsTable";
import type { WebsiteLeadsResponse } from "@/lib/leads/types";

async function fetchLeads(): Promise<WebsiteLeadsResponse | null> {
  try {
    const res = await adminServerFetch<WebsiteLeadsResponse>(
      adminRoutes.leads.list(),
    );
    return res ?? null;
  } catch (err) {
    if (err instanceof ApiError && err.status === 404) return null;
    // eslint-disable-next-line no-console
    console.error("[leads page] fetchLeads:", err);
    return null;
  }
}

export default async function LeadsPage() {
  const data = await fetchLeads();

  return (
    <div className="mx-auto max-w-7xl space-y-6">
      <div>
        <h1 className="text-xl font-semibold tracking-tight">Website Leads</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Marketing-site form submissions. New leads are the to-do inbox;
          marking a lead contacted moves it to the Contacted tab.
        </p>
      </div>

      <LeadsTable
        initialItems={data?.items ?? []}
        initialNextCursor={data?.next_cursor ?? null}
      />
    </div>
  );
}
