/**
 * Conversations list table (Phase 8 PR-6).
 *
 * Renders a paginated table of conversations with channel icon, customer
 * name, last activity, intent badge, and message count. Each row links
 * to the detail page. Pagination is cursor-based: ``Older`` advances
 * the cursor in the URL.
 *
 * Server Component — no client state needed since channel + cursor live
 * in the URL.
 */

import Link from "next/link";
import { Sparkles } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { formatRelative } from "@/lib/format";
import { cn } from "@/lib/utils";
import {
  CHANNEL_KIND_ICONS,
  CHANNEL_KIND_LABELS,
  type ChannelKind,
} from "@/lib/channel-kinds";
import type { ConversationListItemDTO as SharedConversationListItemDTO } from "@/lib/types/conversations";

// "demo" is a portal-only display label for demo-mode conversations
// (ADR-027), a real value in ConversationChannel (the conversations table's
// backing Literal) but never a real API ChannelKind (the 5-kind
// entitlement vocabulary channel-kinds.ts exports) — see plan Decision #5.
type Channel = ChannelKind | "demo";

type ConversationListItemDTO = Omit<
  SharedConversationListItemDTO,
  "channel"
> & {
  channel: Channel;
};

const CHANNEL_META: Record<
  Channel,
  { label: string; Icon: React.ComponentType<{ className?: string }> }
> = {
  whatsapp: {
    label: CHANNEL_KIND_LABELS.whatsapp,
    Icon: CHANNEL_KIND_ICONS.whatsapp,
  },
  // Instagram-API-with-Instagram-Login (graph.instagram.com) is the only
  // Instagram channel a conversation row can carry post-migration-109
  // (legacy FB-page 'instagram' was dropped from the conversations.channel
  // CHECK constraint — see lib/types/conversations.ts header comment).
  instagram_login: {
    label: CHANNEL_KIND_LABELS.instagram_login,
    Icon: CHANNEL_KIND_ICONS.instagram_login,
  },
  messenger: {
    label: CHANNEL_KIND_LABELS.messenger,
    Icon: CHANNEL_KIND_ICONS.messenger,
  },
  telegram: {
    label: CHANNEL_KIND_LABELS.telegram,
    Icon: CHANNEL_KIND_ICONS.telegram,
  },
  email: { label: CHANNEL_KIND_LABELS.email, Icon: CHANNEL_KIND_ICONS.email },
  demo: { label: "Demo", Icon: Sparkles },
};

// Defensive fallback so a future/unmapped channel value can never crash the
// table render (the missing-channel-in-CHANNEL_META destructure was the
// instagram_login regression; mirrors the earlier telegram-icon crash).
const CHANNEL_FALLBACK = {
  label: "Channel",
  Icon: CHANNEL_KIND_ICONS.whatsapp,
} as const;

// Semantic/n-* tokens only (F4) — no raw Tailwind palette classes, no
// dark: overrides (n-* and HSL semantic tokens are already dark-mode-aware
// via globals.css's .dark block).
const INTENT_TONE: Record<string, string> = {
  greeting: "bg-muted text-muted-foreground",
  faq: "bg-n-blue-3 text-n-blue-11",
  booking: "bg-success/10 text-success",
  handoff: "bg-warning/10 text-warning-foreground",
  promo: "bg-n-violet-3 text-n-violet-11",
  other: "bg-muted text-muted-foreground",
};

export function ConversationsTable({
  items,
  nextCursor,
  activeChannel,
}: {
  items: ConversationListItemDTO[];
  nextCursor: string | null;
  activeChannel: Channel | null;
}) {
  if (items.length === 0) {
    return null;
  }

  const nextHref = nextCursor ? buildNextHref(activeChannel, nextCursor) : null;

  return (
    <div className="overflow-hidden rounded-xl border bg-card">
      <table className="w-full text-sm">
        <caption className="sr-only">
          Conversations sorted by latest activity
        </caption>
        <thead className="border-b bg-muted/40 text-left text-xs uppercase text-muted-foreground">
          <tr>
            <th scope="col" className="px-4 py-2.5 font-medium">
              Customer
            </th>
            <th scope="col" className="px-4 py-2.5 font-medium">
              Channel
            </th>
            <th scope="col" className="px-4 py-2.5 font-medium">
              Last activity
            </th>
            <th scope="col" className="px-4 py-2.5 font-medium">
              Last intent
            </th>
            <th scope="col" className="px-4 py-2.5 text-right font-medium">
              Messages
            </th>
          </tr>
        </thead>
        <tbody>
          {items.map((c) => {
            const { Icon, label } = CHANNEL_META[c.channel] ?? CHANNEL_FALLBACK;
            return (
              <tr
                key={c.id}
                className="border-b last:border-b-0 hover:bg-accent/30"
              >
                <td className="px-4 py-3">
                  <Link
                    href={`/conversations/${c.id}`}
                    className="font-medium text-foreground hover:underline"
                  >
                    {c.customer_display_name}
                  </Link>
                  <div className="text-xs text-muted-foreground">
                    {c.channel_user_id}
                  </div>
                </td>
                <td className="px-4 py-3">
                  <div className="flex items-center gap-2 text-muted-foreground">
                    <Icon className="h-4 w-4" aria-hidden />
                    <span>{label}</span>
                  </div>
                </td>
                <td className="px-4 py-3 text-muted-foreground">
                  {c.last_message_at ? formatRelative(c.last_message_at) : "—"}
                </td>
                <td className="px-4 py-3">
                  {c.last_intent ? (
                    <Badge
                      variant="outline"
                      className={cn(
                        "border-0 font-normal capitalize",
                        INTENT_TONE[c.last_intent] ?? INTENT_TONE.other,
                      )}
                    >
                      {c.last_intent}
                    </Badge>
                  ) : (
                    <span className="text-muted-foreground">—</span>
                  )}
                </td>
                <td className="px-4 py-3 text-right tabular-nums text-foreground">
                  {c.message_count}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>

      {nextHref ? (
        <div className="flex justify-end border-t bg-card px-4 py-2">
          <Link
            href={nextHref}
            className="text-sm font-medium text-foreground hover:underline"
          >
            Older →
          </Link>
        </div>
      ) : null}
    </div>
  );
}

function buildNextHref(channel: Channel | null, cursor: string): string {
  const params = new URLSearchParams();
  if (channel) params.set("channel", channel);
  params.set("cursor", cursor);
  return `/conversations?${params.toString()}`;
}
