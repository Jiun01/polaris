/**
 * Sales quotes list page (Phase 9 PR-9).
 * Server component. Fetches all quotes and renders a table.
 */

import Link from "next/link";
import { Card } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { adminServerFetch } from "@/lib/api/server";
import { adminRoutes } from "@/lib/api/routes";
import { ApiError } from "@/lib/api/error";
import { formatMytDate } from "@/lib/format";
import type { SalesQuote } from "@/lib/types/billing";

interface QuotesResponse {
  items: SalesQuote[];
  total: number;
}

async function fetchQuotes(): Promise<QuotesResponse | { error: string }> {
  try {
    const res = await adminServerFetch<QuotesResponse>(
      adminRoutes.quotes.list(),
    );
    return res ?? { items: [], total: 0 };
  } catch (err) {
    if (err instanceof ApiError) {
      if (err.status === 404 || err.status === 501)
        return { items: [], total: 0 };
      return { error: err.message };
    }
    throw err;
  }
}

export default async function QuotesPage() {
  const data = await fetchQuotes();

  return (
    <div className="mx-auto max-w-7xl space-y-6">
      <div className="flex items-end justify-between">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">Sales Quotes</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Generate and track prospect quotes with proration pricing.
          </p>
        </div>
        <Link href="/quotes/new">
          <Button size="sm">Generate quote</Button>
        </Link>
      </div>

      {"error" in data ? (
        <Card className="border-destructive/30 bg-destructive/5 p-4 text-sm text-destructive">
          Failed to load quotes: {data.error}
        </Card>
      ) : data.items.length === 0 ? (
        <Card className="border-dashed p-6 text-sm text-muted-foreground">
          No quotes yet. Generate one to send to a prospect.
        </Card>
      ) : (
        <Card className="overflow-hidden">
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="bg-muted/50 text-left text-xs uppercase tracking-wider text-muted-foreground">
                <tr>
                  <th scope="col" className="px-4 py-2.5 font-medium">
                    Quote code
                  </th>
                  <th scope="col" className="px-4 py-2.5 font-medium">
                    Prospect
                  </th>
                  <th scope="col" className="px-4 py-2.5 font-medium">
                    Tier
                  </th>
                  <th scope="col" className="px-4 py-2.5 font-medium">
                    Signup date
                  </th>
                  <th scope="col" className="px-4 py-2.5 font-medium">
                    Expires
                  </th>
                  <th scope="col" className="px-4 py-2.5 font-medium">
                    Status
                  </th>
                  <th scope="col" className="px-4 py-2.5 font-medium">
                    Actions
                  </th>
                </tr>
              </thead>
              <tbody>
                {data.items.map((q) => (
                  <tr
                    key={q.id}
                    className="border-t border-border transition hover:bg-muted/40"
                  >
                    <td className="px-4 py-2.5 font-mono text-xs">
                      {q.quote_code}
                    </td>
                    <td className="px-4 py-2.5">
                      <div className="flex flex-col">
                        <span className="font-medium">
                          {q.prospect_company ?? "—"}
                        </span>
                        <span className="text-xs text-muted-foreground">
                          {q.prospect_email ?? ""}
                        </span>
                      </div>
                    </td>
                    <td className="px-4 py-2.5">
                      <Badge variant="outline" className="text-xs capitalize">
                        {q.target_tier}
                      </Badge>
                    </td>
                    <td className="px-4 py-2.5 text-xs text-muted-foreground whitespace-nowrap">
                      {formatMytDate(q.intended_signup_date)}
                    </td>
                    <td className="px-4 py-2.5 text-xs text-muted-foreground whitespace-nowrap">
                      {formatMytDate(q.expires_at)}
                    </td>
                    <td className="px-4 py-2.5">
                      {q.converted_to_tenant_id ? (
                        <Badge
                          variant="secondary"
                          className="text-xs text-success border-success/30 bg-success/10"
                        >
                          Converted
                        </Badge>
                      ) : (
                        <Badge
                          variant="outline"
                          className="text-xs text-muted-foreground"
                        >
                          Unconverted
                        </Badge>
                      )}
                    </td>
                    <td className="px-4 py-2.5">
                      <a
                        href={adminRoutes.quotes.pdf(q.id)}
                        target="_blank"
                        rel="noreferrer"
                        className="text-xs text-primary underline-offset-2 hover:underline"
                      >
                        PDF
                      </a>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}
    </div>
  );
}
