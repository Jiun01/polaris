"use client";

/**
 * Channel filter chips for the Conversations list (Phase 8 PR-6).
 *
 * Renders as a horizontal row of buttons — "All", WhatsApp, Instagram,
 * Messenger, Demo. Selecting one rewrites the URL (?channel=...) so
 * the page is re-fetched server-side with the filter applied. We do
 * not store state locally — URL is the source of truth so refresh /
 * share / browser-back behave consistently.
 *
 * Demo is included because demo-mode conversations (ADR-027) land in
 * the same table and operators occasionally need to inspect them when
 * a prospect's demo session fails.
 */

import * as React from "react";
import Link from "next/link";

import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import { CHANNEL_KIND_LABELS, type ChannelKind } from "@/lib/channel-kinds";

// "demo" is a portal-only display label for demo-mode conversations
// (ADR-027), never a real API ChannelKind; see plan Decision #5. Legacy
// "instagram" is intentionally NOT included — ConversationChannel (the
// conversations table's real backing Literal) dropped it in migration 109;
// see lib/types/conversations.ts for the full note.
type Channel = ChannelKind | "demo";

const CHANNELS: ReadonlyArray<{ value: Channel | null; label: string }> = [
  { value: null, label: "All" },
  { value: "whatsapp", label: CHANNEL_KIND_LABELS.whatsapp },
  { value: "instagram_login", label: CHANNEL_KIND_LABELS.instagram_login },
  { value: "messenger", label: CHANNEL_KIND_LABELS.messenger },
  { value: "telegram", label: CHANNEL_KIND_LABELS.telegram },
  { value: "demo", label: "Demo" },
];

export function ChannelFilterChips({
  activeChannel,
}: {
  activeChannel: Channel | null;
}) {
  return (
    <nav
      className="flex flex-wrap items-center gap-2"
      aria-label="Filter conversations by channel"
    >
      {CHANNELS.map((c) => {
        const active = c.value === activeChannel;
        // Strip the cursor when switching channels — pagination is
        // anchored to the filter, so a stale cursor from the previous
        // filter would point at a row that's no longer in the result
        // set and return inconsistent next-page state.
        const href =
          c.value === null
            ? "/conversations"
            : `/conversations?channel=${c.value}`;
        return (
          <Link
            key={c.value ?? "all"}
            href={href}
            aria-pressed={active}
            className={cn(
              "rounded-full border px-3 py-1 text-sm transition-colors",
              active
                ? "border-foreground bg-foreground text-background"
                : "border-border bg-card text-muted-foreground hover:bg-accent/50 hover:text-foreground",
            )}
          >
            <Badge
              variant="outline"
              className={cn(
                "border-0 bg-transparent p-0 text-inherit",
                active && "text-background",
              )}
            >
              {c.label}
            </Badge>
          </Link>
        );
      })}
    </nav>
  );
}
