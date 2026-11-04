/**
 * Channel-kind vocabulary shared by the tenant wizards (CreateWizard +
 * AddChannelModal).
 *
 * Mirrors the API's `_CANONICAL_CHANNEL_KINDS` and the per-tier
 * `_TIER_QUOTA_DEFAULTS[tier].allowed_channel_kinds` in `admin_tenants.py`.
 * Stored value is `instagram_login` (not legacy `instagram`).
 */

import type { PlanTier } from "@/components/tenants/types";

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

/**
 * Default channel entitlements for a tier. Mirrors
 * `_TIER_QUOTA_DEFAULTS[tier].allowed_channel_kinds`: Starter gets WhatsApp
 * only; every other tier (Pro/Ultra/Enterprise) gets all GA kinds. A null tier
 * (unknown) is treated as non-Starter so the admin can pick any GA kind.
 */
export function tierDefaultChannels(tier: PlanTier | null): ChannelKind[] {
  if (tier === "starter") return ["whatsapp"];
  return [...ALL_CHANNEL_KINDS];
}
