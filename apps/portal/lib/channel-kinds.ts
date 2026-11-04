/**
 * Channel-kind vocabulary shared across the portal (dashboard, channels,
 * conversations, broadcasts, templates).
 *
 * Mirrors `apps/admin/lib/channel-kinds.ts`, which mirrors the API's
 * `_CANONICAL_CHANNEL_KINDS` (admin_tenants.py) — the 5-kind *entitlement*
 * vocabulary. Stored value is `instagram_login` (not legacy `instagram`).
 *
 * Sites that must additionally accept the legacy `"instagram"` value for
 * display/back-compat, or portal's `"demo"` pseudo-channel, extend
 * `ChannelKind` LOCALLY at the call site with a one-line comment — they do
 * NOT get added here. See
 * ../docs/internal-ops/PRPs/plans/completed/audit-batch-f-frontend-consolidation.plan.md
 * "Decisions locked" #2 (legacy instagram) and #5 (demo).
 */

import {
  Camera,
  Mail,
  MessageCircle,
  Send,
  SendHorizontal,
  type LucideIcon,
} from "lucide-react";

export const ALL_CHANNEL_KINDS = [
  "whatsapp",
  "instagram_login",
  "messenger",
  "telegram",
  "email",
] as const;

export type ChannelKind = (typeof ALL_CHANNEL_KINDS)[number];

export const CHANNEL_KIND_LABELS: Record<ChannelKind, string> = {
  whatsapp: "WhatsApp",
  instagram_login: "Instagram (Login)",
  messenger: "Messenger DM",
  telegram: "Telegram",
  email: "Email",
};

// lucide-react dropped its Instagram glyph (legal); Camera is the closest
// semantic proxy until the platform brand-asset SVG ships (carried forward
// from ChannelsClient.tsx's pre-existing comment).
export const CHANNEL_KIND_ICONS: Record<ChannelKind, LucideIcon> = {
  whatsapp: MessageCircle,
  instagram_login: Camera,
  messenger: Send,
  // Telegram's logo is a paper plane; SendHorizontal is the closest proxy
  // and stays visually distinct from Messenger's Send.
  telegram: SendHorizontal,
  email: Mail,
};

/**
 * Broadcast audience filters are backend-restricted to 2 kinds — see
 * packages/shared-types/python/polaris_shared_types/broadcast.py:106-117
 * (`FilterChannel.channels: list[Literal["whatsapp","messenger"]]`,
 * max_length=2; code comment there notes Telegram is "forward-compat,
 * rejected at the validation boundary today"). Derived here so
 * BroadcastWizard's option list can't offer a kind the API will 422 on.
 */
export const BROADCAST_CAPABLE_KINDS = [
  "whatsapp",
  "messenger",
] as const satisfies readonly ChannelKind[];
