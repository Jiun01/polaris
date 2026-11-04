/**
 * Audience — Members tab (Phase 8 PR-8).
 *
 * Server Component. Fetches `/api/tenant/audience/?channel=...&q=...`
 * with cursor pagination. Excludes opted-out members — those live in
 * the dedicated /audience/opted-out tab so the PDPA viewer is the
 * only entry point for opt-out data.
 */

import Link from "next/link";

import { apiFetchServer } from "@/lib/api/server";
import { PolarisApiError } from "@/lib/api/errors";
import { TenantRoutes } from "@/lib/api/routes";
import { Badge } from "@/components/ui/badge";
import { formatAbsolute } from "@/lib/format";

interface MemberDTO {
  id: string;
  channel: string;
  channel_user_id: string;
  channel_user_name: string | null;
  phone_number_e164: string | null;
  email: string | null;
  locale: string | null;
  tags: string[];
  opt_in_source: string;
  opted_in_at: string;
  tenant_branch_id: string | null;
}

interface MemberListResponse {
  items: MemberDTO[];
  next_cursor: string | null;
}

const CHANNEL_LABEL: Record<string, string> = {
  whatsapp: "WhatsApp",
  instagram: "Instagram",
  instagram_login: "Instagram",
  messenger: "Messenger",
  telegram: "Telegram",
  email: "Email",
  demo: "Demo",
};

async function loadMembers(
  channel: string | null,
  q: string | null,
  intent: string | null,
  cursor: string | null,
): Promise<{ data: MemberListResponse; error?: string }> {
  const params = new URLSearchParams();
  if (channel) params.set("channel", channel);
  if (q) params.set("q", q);
  if (intent) params.set("intent", intent);
  if (cursor) params.set("cursor", cursor);
  params.set("page_size", "50");

  try {
    const res = await apiFetchServer<MemberListResponse>(
      TenantRoutes.audienceList(params.toString()),
    );
    return { data: res ?? { items: [], next_cursor: null } };
  } catch (err) {
    // eslint-disable-next-line no-console
    console.error("[portal /audience] fetch failed", err);
    return {
      data: { items: [], next_cursor: null },
      error:
        err instanceof PolarisApiError
          ? err.message
          : "Couldn't load audience members.",
    };
  }
}

export default async function AudienceMembersPage({
  searchParams,
}: {
  searchParams: Record<string, string | string[] | undefined>;
}) {
  const channel =
    typeof searchParams.channel === "string" ? searchParams.channel : null;
  const q = typeof searchParams.q === "string" ? searchParams.q : null;
  const intent =
    typeof searchParams.intent === "string" ? searchParams.intent : null;
  const cursor =
    typeof searchParams.cursor === "string" ? searchParams.cursor : null;

  const { data, error } = await loadMembers(channel, q, intent, cursor);

  return (
    <div className="space-y-4">
      <form
        className="flex flex-wrap gap-2"
        method="GET"
        action="/audience"
        role="search"
      >
        <select
          name="channel"
          defaultValue={channel ?? ""}
          aria-label="Filter by channel"
          className="rounded-lg bg-background px-2 py-1.5 text-sm outline outline-1 outline-input transition-colors hover:outline-n-slate-6"
        >
          <option value="">All channels</option>
          <option value="whatsapp">WhatsApp</option>
          <option value="instagram_login">Instagram</option>
          <option value="instagram">Instagram (FB Page)</option>
          <option value="messenger">Messenger</option>
          <option value="telegram">Telegram</option>
          <option value="email">Email</option>
          <option value="demo">Demo</option>
        </select>
        <select
          name="intent"
          defaultValue={intent ?? ""}
          aria-label="Filter by intent"
          className="rounded-lg bg-background px-2 py-1.5 text-sm outline outline-1 outline-input transition-colors hover:outline-n-slate-6"
        >
          <option value="">All intents</option>
          <option value="subscription_intent">Subscription intent</option>
        </select>
        <input
          name="q"
          type="search"
          placeholder="Search name, phone, email…"
          defaultValue={q ?? ""}
          className="flex-1 rounded-lg bg-background px-2 py-1.5 text-sm outline outline-1 outline-input transition-colors hover:outline-n-slate-6"
        />
        <button
          type="submit"
          className="rounded-xl bg-card px-3 py-1.5 text-sm outline outline-1 -outline-offset-1 outline-border hover:bg-accent/30"
        >
          Filter
        </button>
      </form>

      {error ? (
        <div
          role="status"
          className="rounded-lg border border-warning bg-warning/10 px-4 py-3 text-sm text-warning"
        >
          {error}
        </div>
      ) : null}

      {data.items.length === 0 && !error ? (
        <div className="rounded-xl border border-dashed bg-muted/30 p-8 text-center">
          <h2 className="text-base font-semibold">No members yet</h2>
          <p className="mt-1 text-sm text-muted-foreground">
            Customers who message your bot and consent to follow-ups will appear
            here. You can also import existing contacts via CSV.
          </p>
          <p className="mt-4 text-sm">
            <Link className="underline" href="/audience/import">
              Import a CSV →
            </Link>
          </p>
        </div>
      ) : (
        <div className="overflow-hidden rounded-xl border bg-card">
          <table className="w-full text-sm">
            <caption className="sr-only">Opted-in audience members</caption>
            <thead className="border-b bg-muted/40 text-left text-xs uppercase text-muted-foreground">
              <tr>
                <th scope="col" className="px-4 py-2.5 font-medium">
                  Contact
                </th>
                <th scope="col" className="px-4 py-2.5 font-medium">
                  Channel
                </th>
                <th scope="col" className="px-4 py-2.5 font-medium">
                  Locale
                </th>
                <th scope="col" className="px-4 py-2.5 font-medium">
                  Tags
                </th>
                <th scope="col" className="px-4 py-2.5 font-medium">
                  Opted-in
                </th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((m) => (
                <tr key={m.id} className="border-b last:border-b-0">
                  <td className="px-4 py-3">
                    <div className="font-medium">
                      {m.channel_user_name ?? m.channel_user_id}
                    </div>
                    <div className="text-xs text-muted-foreground">
                      {m.phone_number_e164 ?? m.email ?? m.channel_user_id}
                    </div>
                  </td>
                  <td className="px-4 py-3 text-muted-foreground">
                    {CHANNEL_LABEL[m.channel] ?? m.channel}
                  </td>
                  <td className="px-4 py-3 font-mono text-xs">
                    {m.locale ?? "—"}
                  </td>
                  <td className="px-4 py-3">
                    <div className="flex flex-wrap gap-1">
                      {m.tags.length === 0 ? (
                        <span className="text-xs text-muted-foreground">—</span>
                      ) : (
                        m.tags.map((t) => (
                          <Badge
                            key={t}
                            variant="outline"
                            className="text-[10px] font-normal"
                          >
                            {t}
                          </Badge>
                        ))
                      )}
                    </div>
                  </td>
                  <td className="px-4 py-3 text-muted-foreground">
                    {formatAbsolute(m.opted_in_at)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>

          {data.next_cursor ? (
            <div className="flex justify-end border-t bg-card px-4 py-2">
              <Link
                href={buildNextHref(channel, q, intent, data.next_cursor)}
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

function buildNextHref(
  channel: string | null,
  q: string | null,
  intent: string | null,
  cursor: string,
): string {
  const params = new URLSearchParams();
  if (channel) params.set("channel", channel);
  if (q) params.set("q", q);
  if (intent) params.set("intent", intent);
  params.set("cursor", cursor);
  return `/audience?${params.toString()}`;
}
