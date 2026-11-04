/**
 * Conversations viewer — Screen #2 (Phase 8 PR-6).
 *
 * Server Component. Fetches a page of conversations from
 * `/api/tenant/conversations`, hydrates the channel filter chips +
 * paginated table on the client.
 *
 * READ-ONLY at GA per ADR-030 — reply UI lives in Chatwoot. The detail
 * pane surfaces a "Reply in Chatwoot" CTA that deep-links to
 * `chat.getpolaris.my` so the operator switches tabs to act.
 *
 * Channel filter + cursor live in the URL query string so a refreshed
 * tab restores the same view + the Sidebar's active-route check works
 * even on deep filtered URLs.
 */

import Link from "next/link";

import { apiFetchServer } from "@/lib/api/server";
import { PolarisApiError } from "@/lib/api/errors";
import { TenantRoutes } from "@/lib/api/routes";
import { ChannelFilterChips } from "@/components/conversations/ChannelFilterChips";
import { ConversationsTable } from "@/components/conversations/ConversationsTable";
import { type ChannelKind } from "@/lib/channel-kinds";
import type {
  ConversationListItemDTO,
  ConversationListResponse,
} from "@/lib/types/conversations";

// ConversationChannel (conversation_view.py, the real backing Literal for
// the `conversations` table) is {whatsapp, messenger, demo, telegram,
// instagram_login} — legacy "instagram" is NOT a member. Migration 109
// (packages/db-migrations/109_drop_legacy_instagram_channel.sql) dropped
// it from the conversations.channel CHECK constraint, and
// tenant_conversations.py's `_ALLOWED_CHANNELS =
// frozenset(get_args(ConversationChannel))` means the API 422s a
// `?channel=instagram` filter today. (Deviates from the plan's original
// Decision #2 for this surface — re-verified against the live router +
// migration during implementation; see lib/types/conversations.ts for the
// full note. Decision #2 still applies to the separate tenant_channels
// surface, e.g. components/channels/*.)
const ALLOWED_CHANNELS = [
  "whatsapp",
  "instagram_login",
  "messenger",
  "telegram",
  "demo",
] as const;
type Channel = ChannelKind | "demo";

type ConversationListResponseDTO = ConversationListResponse;

const EMPTY_RESPONSE: ConversationListResponseDTO = {
  items: [],
  next_cursor: null,
};

function parseChannel(raw: string | string[] | undefined): Channel | null {
  if (typeof raw !== "string") return null;
  // Type-narrow at the boundary — anything else is a 400 from the API
  // anyway, but we want a clean undefined-or-string union here.
  return (ALLOWED_CHANNELS as readonly string[]).includes(raw)
    ? (raw as Channel)
    : null;
}

function parseCursor(raw: string | string[] | undefined): string | null {
  if (typeof raw !== "string" || !raw) return null;
  return raw;
}

async function loadConversations(
  channel: Channel | null,
  cursor: string | null,
): Promise<{ data: ConversationListResponseDTO; error?: string }> {
  const params = new URLSearchParams();
  if (channel) params.set("channel", channel);
  if (cursor) params.set("cursor", cursor);
  // Keep the page size conservative so the LATERAL count subquery on
  // the backend stays inside its budget.
  params.set("page_size", "25");

  const path = TenantRoutes.conversationsList(params.toString());
  try {
    const res = await apiFetchServer<ConversationListResponseDTO>(path);
    return { data: res ?? EMPTY_RESPONSE };
  } catch (err) {
    const code =
      err instanceof PolarisApiError
        ? `${err.status} ${err.code}`
        : String(err);
    // eslint-disable-next-line no-console
    console.error(`[portal /conversations] fetch failed: ${code}`, err);
    return {
      data: EMPTY_RESPONSE,
      error: "We're having trouble loading your conversations.",
    };
  }
}

export default async function ConversationsPage({
  searchParams,
}: {
  searchParams: Record<string, string | string[] | undefined>;
}) {
  const channel = parseChannel(searchParams.channel);
  const cursor = parseCursor(searchParams.cursor);
  const { data, error } = await loadConversations(channel, cursor);

  return (
    <div className="space-y-6">
      <header className="flex flex-col gap-2">
        <h1 className="text-3xl font-semibold tracking-tight">Conversations</h1>
        <p className="text-sm text-muted-foreground">
          A read-only view of every conversation your bot has handled. Reply
          from{" "}
          <a
            className="underline"
            href="https://chat.getpolaris.my"
            target="_blank"
            rel="noreferrer"
          >
            your messaging inbox
          </a>
          . The agent dashboard signs in separately.
        </p>
      </header>

      <ChannelFilterChips activeChannel={channel} />

      {error ? (
        <div
          role="status"
          className="rounded-lg border border-warning bg-warning/10 px-4 py-3 text-sm text-warning"
        >
          {error}
        </div>
      ) : null}

      {/* next_cursor is `str | None = None` in Pydantic (nullable AND
          defaulted), so the generated type marks it optional (`?:`) on top
          of the correct `string | null`; always present on the wire. */}
      <ConversationsTable
        items={data.items}
        nextCursor={data.next_cursor ?? null}
        activeChannel={channel}
      />

      {data.items.length === 0 && !error ? (
        <div className="rounded-xl border border-dashed bg-muted/30 p-8 text-center">
          <h2 className="text-base font-semibold">No conversations yet</h2>
          <p className="mt-1 text-sm text-muted-foreground">
            Conversations will appear here as soon as a customer sends their
            first message on a connected channel.
          </p>
          <p className="mt-4 text-sm">
            <Link className="underline" href="/channels">
              Check your channel connections →
            </Link>
          </p>
        </div>
      ) : null}
    </div>
  );
}
