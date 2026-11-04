/**
 * Shared Conversations-viewer DTOs — deduplicates the byte-identical
 * `ConversationListItemDTO` previously declared independently in both
 * `components/conversations/ConversationsTable.tsx` and
 * `app/(portal)/conversations/page.tsx` (findings item 3).
 *
 * Re-exported from @polaris/shared-types. Confirmed a true 1:1 SSOT match
 * during audit-batch-f-frontend-consolidation (2026-07):
 * tenant_conversations.py's GET /api/tenant/conversations/ uses
 * ConversationListResponse (containing ConversationListItem rows)
 * directly as response_model.
 *
 * IMPORTANT — deviates from the plan's original Decision #2 for THIS
 * surface specifically: `ConversationChannel` (the real backing Literal,
 * conversation_view.py) is `"whatsapp" | "messenger" | "demo" | "telegram"
 * | "instagram_login"` — legacy "instagram" is NOT in it. Migration 109
 * (packages/db-migrations/109_drop_legacy_instagram_channel.sql) already
 * narrowed the `conversations.channel` CHECK constraint to drop
 * 'instagram', and tenant_conversations.py's `_ALLOWED_CHANNELS =
 * frozenset(get_args(ConversationChannel))` means the API itself 422s a
 * `?channel=instagram` filter today. The plan's Decision #2 (legacy
 * instagram must stay for pre-migration rows) is stale for this
 * conversations surface — re-verified against the live router + migration
 * during implementation and corrected here. Decision #2 DOES still apply
 * to the separate `tenant_channels` surface (channel CONNECTIONS, a
 * different table/router — see apps/admin/components/tenants/types.ts and
 * portal/components/channels/*), which was NOT touched by migration 109's
 * ChannelKind Literal in tenant_channels.py (a pre-existing, unrelated
 * drift outside this batch's scope).
 */

export type {
  ConversationListItem,
  ConversationListResponse,
} from "@polaris/shared-types";
export type { ConversationListItem as ConversationListItemDTO } from "@polaris/shared-types";
