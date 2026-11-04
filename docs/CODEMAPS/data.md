
# Data Model Codemap

## Migration History (as-built 2026-07-02)

| Range | Area |
|---|---|
| 001–010 | Core: extensions, platform_health, tenants, users, channels, quotas, secrets, audit_logs, billing |
| 011–030 | Audience, broadcasts, flows, bookings, knowledge_base, messages, opt-out |
| 031–053 | Phase 4–8: LLM intent, rate-limit, notifications, plan_change_requests |
| 054–070 | Phase 9: platform_admins, tenant_invoices, audit_chain_verifications, sales_quotes, portal quota cols |
| 071–088 | Phase 10–14: Telegram, website widget, voice notes, email channel |
| 089–090 | Phase 15: tenant_products, product_images_total_mb quota col |
| 091–092 | AI follow-ups: followup_config, followup_log |
| 093 | platform_mrr_snapshots (append-only, ADR-062) |
| 094–096 | Telegram secret CHECK fix + website widget grants + reconcile |
| 097 | email in tenant_channels CHECK + email_drafts table |
| 098 | knowledge_gap_misses + knowledge_gap_tiers (Ultra-only) |
| 099 | Telegram quota backfill (pro/ultra/enterprise allowed_channel_kinds) |
| 100/101 | admin_hard_delete_tenant() SECURITY DEFINER fn + chatwoot_account_id col; 101 REVOKEs DELETE on demo_sessions/tenant_invoices/audience_members from polaris_service |
| 102–103 | webhook_events (source,received) index; audit_log retention default-off |
| 104 | **instagram_login** in tenant_channels + conversations + bookings CHECK; allowed_channel_kinds backfill (pro/ultra/ent) — PR #91, v1.0.23 |
| 105 | **polaris-internal persona seed** — UPDATE tenants SET system_prompt (idempotent, WHERE NULL) so the demo bot keeps its product knowledge after the default persona went generic — PR #99 (ADR-063) |
| 106 | **conversations_channel_id** — adds `channel_id UUID` to `conversations` referencing `tenant_channels`; used by `reply_dispatch` to pin WABA token to originating channel (prevents multi-WABA misroute) — 9e41bf1 |
| 107 | **RLS on service-only tables** — deny-all RLS on 11 service tables + email_drafts (tenant policy) + flow_templates (permissive); defense-in-depth vs Supabase anon/PostgREST — `5b12463` |
| 108 | **webhook_events chatwoot-native statuses** — widen `webhook_events.status` CHECK with `human_handling`, `no_recipient`, `chatwoot_deferred` — PR #102 |
| 109 | **drop legacy FB-page instagram** — narrow `tenant_channels.channel` / `conversations.channel` / `bookings.channel` CHECKs to remove `instagram`; only `instagram_login` remains — PR #102 |
| 110 | **GRANT UPDATE(metadata) on messages → polaris_anon** — PR #106 |
| 111 | **repair audience_members.metadata JSONB double-encode** — PR #109 |
| 112 | **repair tenants.settings JSONB double-encode** — PR #110 |
| **113** | **hybrid_search_or_semantics_and_distance** — BM25 leg → OR-semantics (`plainto_tsquery` + ` & `→` | `); adds `vector_distance REAL` output to both hybrid RPCs; vector-proximity tiebreaker. Applies to `match_knowledge_chunks_hybrid` + `match_products_hybrid`. PR #115, v1.0.39. ✅ LIVE |
| **114** | **demo_tenant_model** — `tenants` += `is_demo BOOL DEFAULT FALSE`, `demo_duration_hours INT`, `demo_max_messages INT`, `demo_code TEXT`, `UNIQUE lower(demo_code)` index; `demo_sessions` += `lead_flagged_at TIMESTAMPTZ`; backfills `polaris-internal` tenant with `is_demo=true`. Reuses 008b `demo_sessions` table. v1.0.45, ADR-066. ✅ LIVE |
| **115** | **website_leads** — creates `website_leads` table for Website Channel lead-capture form submissions. ✅ LIVE (v1.0.62) |
| **116** | **bookings_channel_widen** — widens `bookings.channel` CHECK to add `telegram`+`email`. Ships v1.0.63. |
| **117** | **bookings_ea_ids** — adds nullable `ea_service_id INT` + `ea_provider_id INT` to `bookings`; stored by `create_pending_booking` so the short-link redirect can re-mint the EA wizard URL. Ships v1.0.64. |
| **118** | **knowledge_sources** — creates the declarative `knowledge_sources` desired-state ledger (reconciler redesign): `desired_hash` vs `last_synced_hash` diffing, `status` (pending/syncing/synced/failed), soft-delete via `deleted_at`. Adds `knowledge_chunks.source_id` FK (`ON DELETE CASCADE`) and relaxes `knowledge_chunks.request_id` to nullable — the imperative `ingestion_requests` queue is deprecated in favor of this ledger. ✅ shipped v1.0.78. See Knowledge Sources Reconciler. |
| **119** | **knowledge_sources_grants** — RLS/GRANT companion migration for 118. ✅ shipped v1.0.78. |
| **121** | **NEXT** |

## Core Tenant Tables (all RLS-enabled)

```
tenants               id, display_name, plan_tier, status, settings JSONB, chatwoot_account_id,
                      is_demo BOOL (mig 114; hidden demo brands excluded from MRR/health/tenant list),
                      demo_code TEXT (UNIQUE lower()), demo_duration_hours INT, demo_max_messages INT
                      settings JSONB keys: booking_config, followup_config, email_draft_prompt,
                      assistant_name, company_name, vertical, agent_config (PR #99: {enabled,
                      instructions, enabled_tools[], max_tool_iterations}, default disabled for
                      existing tenants; NEW tenants seeded enabled=True + 2 tools, v1.0.55),
                      persona_blocks (v1.0.55: list[{display_name,slug,body}], max 10;
                      NO migration — JSONB key in existing settings column)
                      booking_config sub-keys: tenant_timezone, {confirmation,reminder,
                      cancellation,reschedule}_template_name+_language, confirmation_message,
                      inchat_picker BOOL (default false; v1.0.63 — enables Phase 2 NL slot
                      picker; false = link-mode only; v1.0.64 — surfaced as BookingConfig field
                      + portal Bookings page Switch)
tenant_users          tenant_id, user_id, role (tenant_admin|agent)
tenant_channels       tenant_id, provider, waba_id, phone_number_id, chatwoot_inbox_id, ...
tenant_quotas         tenant_id (1:1 PK), 16 locked columns (ADR-017)
tenant_secrets        tenant_id, service, ciphertext (PyNaCl)
tenant_products       tenant_id, name, price, image_url (Phase 15)
messages              tenant_id, direction, channel, content, created_at
conversations         tenant_id, contact_id, chatwoot_conversation_id, status
audit_logs            tenant_id, actor_id, action, before/after JSONB, row_hash, prev_hash (append-only)
followup_log          tenant_id, contact_id, sent_at, type
email_drafts          tenant_id, conversation_id, draft_text, chatwoot_note_id
knowledge_gap_misses  tenant_id, signal, query_text (Ultra-only)
```

## Platform Tables (no RLS)

```
platform_admins            id, email, name
platform_mrr_snapshots     snapped_at, mrr_myr, tenant_count (append-only)
tenant_invoices            tenant_id, status, pdf_url, amount_myr
sales_quotes               tenant_id, quote_pdf_url
audit_chain_verifications  last_verified_at, result
plan_change_requests       tenant_id, from_tier, to_tier, status, decided_by
```

## tenant_quotas — 16 Locked Columns (ADR-017)

```
bot_replies                int    monthly display cap  (enterprise default = 0, admin must set)
bot_replies_period_cap     int    Redis real-time hard cap
vision_calls_per_month     int
conversation_retention_days int
knowledge_chunks_total_mb  int
rate_limit_per_minute      int
monthly_llm_tokens_in      int
monthly_llm_tokens_out     int
monthly_ingestion_pages    int
monthly_broadcast_recipients int
monthly_bookings           int
agent_seats_included       int
agent_seats_extra          int
outbound_broadcast_enabled bool
multi_branch_enabled       bool
allowed_channel_kinds      text[]  (read-only in admin UI — requires migration to change)
```

## Tier Defaults (seeded on tenant create)

| Column | Starter | Pro | Ultra | Enterprise |
|---|---|---|---|---|
| bot_replies | 5,000 | 25,000 | 60,000 | **0** (custom) |
| rate_limit_per_minute | 60 | 120 | 300 | 600 |
| knowledge_chunks_total_mb | 100 | 500 | 2,000 | 10,000 |
| outbound_broadcast_enabled | false | true | true | true |
| multi_branch_enabled | false | false | true | true |
