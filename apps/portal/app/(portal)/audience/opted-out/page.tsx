/**
 * Audience — Opted-out tab (Phase 8 PR-8).
 *
 * GA-BLOCKING PDPA compliance surface per
 * docs/Polaris V2/02_SCOPE_FEATURES_LIMITATIONS.md §6.
 *
 * UI rules from portal_design_prompt.md §6:
 *   - Persistent warning banner: "Customers who opted out — do not
 *     message them manually."
 *   - Subdued styling — NOT styled as a normal contact list.
 *   - CSV export button (downloads `/api/tenant/audience/opted-out.csv`).
 *
 * Server Component. Fetches `/api/tenant/audience/opted-out`.
 */

import Link from "next/link";

import { apiFetchServer } from "@/lib/api/server";
import { PolarisApiError } from "@/lib/api/errors";
import { TenantRoutes } from "@/lib/api/routes";
import { Button } from "@/components/ui/button";
import { formatAbsolute } from "@/lib/format";

interface OptedOutRow {
  id: string;
  channel: string;
  channel_user_id: string;
  channel_user_name: string | null;
  opted_out_at: string;
  opted_out_reason: string | null;
  opted_out_at_channel: string | null;
}

interface OptedOutPage {
  rows: OptedOutRow[];
  next_cursor: string | null;
  total_count: number;
}

async function loadOptedOut(
  channel: string | null,
  cursor: string | null,
): Promise<{ data: OptedOutPage; error?: string }> {
  const params = new URLSearchParams();
  if (channel) params.set("channel", channel);
  if (cursor) params.set("cursor", cursor);

  try {
    const res = await apiFetchServer<OptedOutPage>(
      TenantRoutes.audienceOptedOutList(params.toString()),
    );
    return { data: res ?? { rows: [], next_cursor: null, total_count: 0 } };
  } catch (err) {
    // eslint-disable-next-line no-console
    console.error("[portal /audience/opted-out] fetch failed", err);
    return {
      data: { rows: [], next_cursor: null, total_count: 0 },
      error:
        err instanceof PolarisApiError
          ? err.message
          : "Couldn't load opt-out list.",
    };
  }
}

export default async function OptedOutPage({
  searchParams,
}: {
  searchParams: Record<string, string | string[] | undefined>;
}) {
  const channel =
    typeof searchParams.channel === "string" ? searchParams.channel : null;
  const cursor =
    typeof searchParams.cursor === "string" ? searchParams.cursor : null;
  const { data, error } = await loadOptedOut(channel, cursor);

  return (
    <div className="space-y-4">
      {/* Persistent PDPA disclaimer — required by 02 §6. Announced to
          screen readers via role="alert" so the warning is also surfaced
          off-visual. */}
      <div
        role="alert"
        className="rounded-lg border border-amber-300 bg-amber-50 px-4 py-3 text-sm text-amber-900 dark:border-amber-900 dark:bg-amber-950 dark:text-amber-100"
      >
        <strong className="font-semibold">PDPA notice.</strong> These customers
        have opted out. Do not message them manually or via broadcast.
        Re-contacting an opted-out customer is a compliance breach. This list
        exists for record-keeping and CSV export only.
      </div>

      <div className="flex items-center justify-between">
        <p className="text-sm text-muted-foreground">
          {data.rows.length} opted-out customer
          {data.rows.length === 1 ? "" : "s"} on this page.
        </p>
        <Button asChild variant="outline">
          <a href={TenantRoutes.audienceOptedOutCsv()} download="opted-out.csv">
            Export CSV
          </a>
        </Button>
      </div>

      {error ? (
        <div
          role="status"
          className="rounded-lg border border-warning bg-warning/10 px-4 py-3 text-sm text-warning"
        >
          {error}
        </div>
      ) : null}

      {data.rows.length === 0 && !error ? (
        <div className="rounded-xl border border-dashed bg-muted/30 p-8 text-center">
          <h2 className="text-base font-semibold">No opt-outs yet</h2>
          <p className="mt-1 text-sm text-muted-foreground">
            Customers who reply STOP to a broadcast (or use the opt-out link)
            will appear here.
          </p>
        </div>
      ) : (
        <div className="overflow-hidden rounded-xl border border-muted-foreground/20 bg-muted/10">
          <table className="w-full text-sm">
            <caption className="sr-only">
              Opted-out contacts, do not message
            </caption>
            <thead className="border-b border-muted-foreground/20 bg-muted/30 text-left text-xs uppercase text-muted-foreground">
              <tr>
                <th scope="col" className="px-4 py-2.5 font-medium">
                  Contact
                </th>
                <th scope="col" className="px-4 py-2.5 font-medium">
                  Channel
                </th>
                <th scope="col" className="px-4 py-2.5 font-medium">
                  Reason
                </th>
                <th scope="col" className="px-4 py-2.5 font-medium">
                  Opted-out
                </th>
              </tr>
            </thead>
            <tbody>
              {data.rows.map((r) => (
                <tr
                  key={r.id}
                  className="border-b border-muted-foreground/10 text-muted-foreground last:border-b-0"
                >
                  <td className="px-4 py-3">
                    <div>{r.channel_user_name ?? r.channel_user_id}</div>
                    <div className="text-xs">{r.channel_user_id}</div>
                  </td>
                  <td className="px-4 py-3 capitalize">{r.channel}</td>
                  <td className="px-4 py-3 capitalize">
                    {r.opted_out_reason ?? "—"}
                  </td>
                  <td className="px-4 py-3">
                    {formatAbsolute(r.opted_out_at)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>

          {data.next_cursor ? (
            <div className="flex justify-end border-t border-muted-foreground/20 bg-muted/20 px-4 py-2">
              <Link
                href={`/audience/opted-out?cursor=${encodeURIComponent(data.next_cursor)}${channel ? `&channel=${channel}` : ""}`}
                className="text-sm font-medium hover:underline"
              >
                Older →
              </Link>
            </div>
          ) : null}
        </div>
      )}
    </div>
  );
}
