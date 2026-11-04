"use client";

/**
 * Channels client wrapper (Phase 8 PR-5).
 *
 * Renders one ChannelTile per customer-facing channel as a read-only
 * status grid. Channels connect automatically once set up, so there is
 * no self-serve paste or contact-support modal here.
 */

import * as React from "react";
import { type LucideIcon } from "lucide-react";

import { ChannelTile } from "@/components/channels/ChannelTile";
import { useTenantContext } from "@/components/providers/tenant-provider";
import { meetsTier } from "@/lib/tier-gate";
import {
  CHANNEL_KIND_ICONS,
  CHANNEL_KIND_LABELS,
  type ChannelKind as CanonicalChannelKind,
} from "@/lib/channel-kinds";

// Legacy "instagram" included: tenant_channels rows provisioned before the
// Instagram Login migration can still carry the legacy FB-page kind — this
// tile-folding view must render old rows without crashing. See plan
// Decision #2.
type ChannelKind = CanonicalChannelKind | "instagram";

const KIND_META: Record<ChannelKind, { label: string; icon: LucideIcon }> = {
  whatsapp: {
    label: CHANNEL_KIND_LABELS.whatsapp,
    icon: CHANNEL_KIND_ICONS.whatsapp,
  },
  // Instagram tile's canonical entry (Decision #2) — the tenant sees one
  // "Instagram" tile regardless of legacy-FB-page vs Login underneath.
  instagram: {
    label: "Instagram",
    icon: CHANNEL_KIND_ICONS.instagram_login,
  },
  // instagram_login (graph.instagram.com) — not its own tile; folded into the
  // Instagram tile's count. Present here so the per-kind Record stays total.
  instagram_login: {
    label: CHANNEL_KIND_LABELS.instagram_login,
    icon: CHANNEL_KIND_ICONS.instagram_login,
  },
  messenger: {
    label: "Facebook Messenger",
    icon: CHANNEL_KIND_ICONS.messenger,
  },
  telegram: {
    label: CHANNEL_KIND_LABELS.telegram,
    icon: CHANNEL_KIND_ICONS.telegram,
  },
  email: { label: CHANNEL_KIND_LABELS.email, icon: CHANNEL_KIND_ICONS.email },
};

export function ChannelsClient({
  perKind,
}: {
  perKind: Record<ChannelKind, number>;
}) {
  const { plan_tier } = useTenantContext();

  // One tile per customer-facing channel. instagram_login has no tile of its
  // own — its inboxes are counted under the "Instagram" tile (the tenant sees
  // one Instagram, regardless of the underlying Login vs FB-page API).
  const order: ChannelKind[] = [
    "whatsapp",
    "instagram",
    "messenger",
    "telegram",
    "email",
  ];

  return (
    <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
      {order.map((kind) => {
        const meta = KIND_META[kind];
        // Email is available on all tiers; IG/Messenger/Telegram are Pro+.
        const tierLocked =
          (kind === "instagram" ||
            kind === "messenger" ||
            kind === "telegram") &&
          !meetsTier(plan_tier, "pro");
        const inboxCount =
          kind === "instagram"
            ? (perKind.instagram ?? 0) + (perKind.instagram_login ?? 0)
            : (perKind[kind] ?? 0);
        return (
          <ChannelTile
            key={kind}
            kind={kind}
            label={meta.label}
            icon={meta.icon}
            inboxCount={inboxCount}
            tierLocked={tierLocked}
          />
        );
      })}
    </div>
  );
}
