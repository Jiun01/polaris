
# Database Migrations Codemap

**Last Updated:** 2026-07-02
**Applied on main:** 001–120 (all idempotent)
**Next migration number:** 121
**Note (119):** `knowledge_sources_grants` — RLS/GRANT companion migration for 118 (pairs with the `_grants` convention seen at 011/012, 014, 045, 050, 077, 100). See `packages/db-migrations/119_knowledge_sources_grants.sql`.
**Note (118):** `knowledge_sources` — creates the declarative `knowledge_sources` desired-state ledger (reconciler redesign): `id, tenant_id, tenant_branch_id, source_type, source_url, storage_path, source_text, source_label, desired_hash, last_synced_hash, status, chunk_count, error, created_at, updated_at, last_synced_at, deleted_at`. Adds `knowledge_chunks.source_id UUID NULL REFERENCES knowledge_sources(id) ON DELETE CASCADE` and relaxes `knowledge_chunks.request_id` to nullable (the imperative `ingestion_requests` queue is deprecated in favor of the reconciler). RLS mirrors 018 (`knowledge_sources_self_select/_insert/_update`; no tenant-facing DELETE — soft-delete only, worker hard-deletes via BYPASSRLS). Ships with services/knowledge_sources.py + `POST/GET /api/tenant/knowledge/sources` + `DELETE /sources/{id}` in `routers/tenant_knowledge.py`. Shipped v1.0.78. See Knowledge Sources Reconciler.
**Note (117):** `bookings_ea_ids` — adds `ea_service_id INT` and `ea_provider_id INT` columns to `bookings`. Required by the `GET /b/{code}` short-link redirect (`routers/booking_redirect.py`) to reconstruct the EA wizard URL deterministically; pre-117 rows return 410. **MUST be applied to Supabase Cloud manually** before deploying v1.0.64. Ships with v1.0.64 booking-link-fixes.
**Note (116):** `bookings_channel_widen` — widens `bookings.channel` CHECK from `('whatsapp','messenger','instagram_login')` to add `'telegram'` and `'email'`. Enables `create_pending_booking` for Telegram/email channel customers (link-mode new-booking Phase 1b). Idempotent: DROP+ADD CONSTRAINT. Ships with v1.0.63 / EA r13. Rollback in SQL comment.
**Note (115):** see prior entry; applied + LIVE.
**Note (113):** `hybrid_search_or_semantics_and_distance` — rewrites BOTH `match_knowledge_chunks_hybrid` and `match_products_hybrid` RPCs. (F4) BM25 leg switches from AND-semantics (`websearch_to_tsquery`, required every query word, fired on ~4/16 natural questions) to **OR-semantics** (`plainto_tsquery` + replace ` & ` → ` | `); a chunk matching ANY query term is now a candidate; `ts_rank` still orders hits and RRF fuses with the vector leg. (F2) both functions now also return raw **`vector_distance`** (`REAL`, cosine, lower=closer; `NULL` for BM25-only hits that the vector leg didn't surface) as a confidence signal alongside `rrf_score`. Vector-proximity tiebreaker added (`ORDER BY rrf_score DESC, vector_distance ASC NULLS LAST`) to handle the richer RRF ties that OR-semantics introduces. Return-type change requires DROP + CREATE (not CREATE OR REPLACE). Ships in PR #115 / v1.0.39.
**Note (112):** `repair_tenants_settings_jsonb_double_encode` — repairs the corrupted `tenants.settings` JSONB row (one instance found post-merge). Applies `jsonb_encode_repair` helper. PR #110 systemic fix.
**Note (111):** `repair_audience_members_metadata_jsonb_double_encode` — repairs `audience_members.metadata` rows corrupted by double-encoding. Migration applies `jsonb_encode_repair` (idempotent; detects and fixes JSON-encoded strings). PR #109.
**Note (110):** `grant_messages_metadata_update` — adds `GRANT UPDATE (metadata) ON messages TO polaris_anon` (column-scoped, mirrors migration 031's provider_message_id grant). Fixes `InsufficientPrivilegeError` when dispatch-failure recorder stamps `messages.metadata`. PR #106.
**Note (109):** `drop_legacy_instagram_channel` — narrows `tenant_channels.channel`, `conversations.channel`, `bookings.channel` CHECKs to REMOVE legacy FB-page `instagram` (only `instagram_login` remains). Safe under clean-wipe (no live `instagram` rows). PR #102.
**Note (108):** `webhook_events_chatwoot_native_status` — widens `webhook_events.status` CHECK with the Chatwoot-native consumer statuses `human_handling` + `no_recipient` + `chatwoot_deferred`. PR #102.
**Note (107):** `rls_service_tables` — enables RLS (deny-all) on 11 service-only tables + email_drafts (tenant policy) + flow_templates (permissive) as defense-in-depth vs the Supabase anon/PostgREST surface. Direct-to-main `5b12463`.
**Note (106):** `conversations_channel_id` — adds `channel_id` column to `conversations` table; used by `reply_dispatch` to pin the WABA token to the conversation's originating channel (prevents wrong-WABA send on multi-WABA tenants). Shipped with the outbound dispatch hardening commit (9e41bf1).
**Note (105):** `polaris-internal persona seed` — `UPDATE tenants SET system_prompt = <Polaris persona> WHERE id='000…001' AND (system_prompt IS NULL OR btrim=''))`. Idempotent. B1 de-Polaris-ified the DEFAULT persona template, so the demo/marketing tenant gets its product knowledge via its own `system_prompt` (the non-overridable `_SAFETY_GUARDS` + channel format + RAG guard still compose around it). PR #99 (ADR-063).
**Note (104):** `instagram_login` first-class channel — widens `tenant_channels.channel` + `conversations.channel` + `bookings.channel` CHECKs to admit `instagram_login`, and backfills `allowed_channel_kinds` for pro/ultra/enterprise. Ships with PR #91 (v1.0.23). Distinct from legacy FB-page `instagram`; graph.instagram.com send path.
**Note (100/101):** admin hard-delete — 100 grants (superseded), 101 the `admin_hard_delete_tenant(UUID)` SECURITY DEFINER fn that preserves the audit_logs append-only ceiling **and REVOKEs DELETE on demo_sessions/tenant_invoices/audience_members from polaris_service** (test cleanups must use the superuser DSN — see test_rls_phase_1_pr5.py).

## Phase 0 — Foundations (001–005)

| # | File | Purpose | Status |
|---|------|---------|--------|
| **001** | `tenants` | Core tenant row | ✅ Applied |
| **002** | `tenant_quotas` | Quota tracking per tier | ✅ Applied |
| **003** | `audit_logs` | Immutable append-only ledger | ✅ Applied |
| **004** | `tenant_secrets` | Encrypted KMS storage | ✅ Applied |
| **005** | `platform_admins` | Admin user registry | ✅ Applied |

---

## Phase 1 — Auth & Secrets (006–013)

| # | File | Purpose | Status |
|---|------|---------|--------|
| **006** | `messages` base | Tenant-local message table (RLS) | ✅ Applied |
| **007** | `auth.users` FK | Link messages → portal user | ✅ Applied |
| **008** | `tenant_usage` | Usage counters (bot_replies, broadcasts) | ✅ Applied |
| **009** | `webhook_events` | Inbound event log | ✅ Applied |
| **010** | `outbound_events` | Outbound tracking | ✅ Applied |
| **011** | `audiences` | Saved customer segments | ✅ Applied |
| **012** | `audience_members` | Customer contact list (partial unique idx on active) | ✅ Applied |
| **013** | `conversations` | Tenant-local conversation rows (RLS) | ✅ Applied |

---

## Phase 2 — Webhooks & Dispatcher (014–019)

| # | File | Purpose | Status |
|---|------|---------|--------|
| **014** | `channels` | Channel registry (WhatsApp, IG, Messenger, Telegram, Widget) | ✅ Applied |
| **015** | `tenant_channels` | Tenant-channel binding (RLS, channel-specific cols) | ✅ Applied |
| **016** | `meta_waba_mapping` | Phone ID → WABA ID lookup | ✅ Applied |
| **017** | `chatwoot_accounts` | Chatwoot instance binding | ✅ Applied |
| **018** | `chatwoot_conversations` | Conversation ↔ CW ID mapping | ✅ Applied |
| **019** | `webhook_handlers` | Handler registry + pause logic | ✅ Applied |

---

## Phase 3 — Knowledge Ingestion (020–025)

| # | File | Purpose | Status |
|---|------|---------|--------|
| **020** | `ingestion_requests` | File upload queue | ✅ Applied |
| **021** | `knowledge_chunks` | Semantic chunks + pgvector embeddings | ✅ Applied |
| **022** | `embedding_models` | Model metadata | ✅ Applied |
| **023** | `ingestion_queue_consumer_state` | Rebalancing state | ✅ Applied |
| **024** | `knowledge_chunk_stats` | Chunk quality metrics | ✅ Applied |
| **025** | `knowledge_search_logs` | Search analytics | ✅ Applied |

---

## Phase 4 — LLM & RAG (026–031)

| # | File | Purpose | Status |
|---|------|---------|--------|
| **026** | `flow_runs` | Flow execution trace (with delete-order fix Phase 20) | ✅ Applied |
| **027** | `llm_cache_hits` | Cache analytics | ✅ Applied |
| **028** | `intent_classifications` | Intent trace (classify_intent result) | ✅ Applied |
| **029** | `rag_searches` | RAG search trace + rerank scores | ✅ Applied |
| **030** | `llm_responses` | LLM generation trace (intent + model used) | ✅ Applied |
| **031** | `demo_session_templates` | Demo mode session data | ✅ Applied |

---

## Phase 5 — Flow Runtime (032–038)

| # | File | Purpose | Status |
|---|------|---------|--------|
| **032** | `flow_templates` | Bundled flow definitions | ✅ Applied |
| **033** | `flows` | Tenant-local custom flows (RLS) | ✅ Applied |
| **034** | `flow_versions` | Flow history (immutable per publish) | ✅ Applied |
| **035** | `flow_variables` | Global + instance-local variable state | ✅ Applied |
| **036** | `bookings` | Booking records (Phase 7 + v2.1 link-mode) | ✅ Applied |
| **037** | `booking_reminders` | Reminder schedule per booking | ✅ Applied |
| **038** | `flow_template_versions` | Bundled template history | ✅ Applied |

---

## Phase 6 — Outbound & Broadcasts (039–047)

| # | File | Purpose | Status |
|---|------|---------|--------|
| **039** | `broadcasts` | Scheduled broadcast campaigns (RLS) | ✅ Applied |
| **040** | `broadcast_recipients` | Broadcast send list + status | ✅ Applied |
| **041** | `tenant_templates` | WhatsApp message templates (approved) | ✅ Applied |
| **042** | `template_approvals` | Template status history | ✅ Applied |
| **043** | `broadcast_worker_state` | Redis-backed state (soft-deleted) | ✅ Applied |
| **044** | `meta_rate_limit_snapshots` | Rate-limit header tracking (migration 085) | ✅ Applied |
| **045** | `outbound_message_cache` | Message ID dedup | ✅ Applied |
| **047** | `broadcast_scheduling_logs` | Broadcast lifecycle audit | ✅ Applied |

---

## Phase 7 — Handoff & Chatwoot (048–053)

| # | File | Purpose | Status |
|---|------|---------|--------|
| **048** | `chatwoot_labels` | Label registry (human-handling, etc.) | ✅ Applied |
| **049** | `conversation_notes` | Internal notes (non-customer-visible) | ✅ Applied |
| **050** | `agent_forward_events` | Chatwoot agent → Meta send tracking | ✅ Applied |
| **051** | `handoff_metadata` | Handoff context (team, queue, etc.) | ✅ Applied |
| **052** | `chatwoot_sync_state` | CW mirror rebalancing state | ✅ Applied |
| **053** | `chatwoot_conversation_locks` | Optimistic locking for CW ops | ✅ Applied |

---

## Phase 8 — Portal & Admin UI (054–061)

| # | File | Purpose | Status |
|---|------|---------|--------|
| **054** | `tenant_users` | Portal user registry (RLS) | ✅ Applied |
| **055** | `tenant_billing_contacts` | Billing contact info | ✅ Applied |
| **056** | `portal_permissions` | Role-based access (editor, viewer, etc.) | ✅ Applied |
| **057** | `admin_sessions` | Admin portal auth state | ✅ Applied |
| **058** | `tenant_plan_change_requests` | Plan upgrade/downgrade workflow | ✅ Applied |
| **059** | `tenant_settings` | JSONB settings (booking_config, followup_config, etc.) | ✅ Applied |
| **060** | `tenant_branches` | Branch-centric routing (v2.1 EA bindings) | ✅ Applied |
| **061** | `promotions` | Promotional codes + tier limits | ✅ Applied |

---

## Phase 9 — Admin Suite & Billing (062–088)

| # | File | Purpose | Status |
|---|------|---------|--------|
| **062** | `invoices` | Invoice records (migration 083: richer model) | ✅ Applied |
| **063** | `invoice_line_items` | Invoice details (refunds, prorations, etc.) | ✅ Applied |
| **064** | `quotes` | Sales quotes (PDF, conversion to invoice) | ✅ Applied |
| **065** | `quote_line_items` | Quote line details | ✅ Applied |
| **066** | `platform_fixed_costs` | Flat monthly costs per entity | ✅ Applied |
| **067** | `cost_ceilings` | Per-tenant LLM cost caps | ✅ Applied |
| **068** | `tenant_cost_projections` | Monthly cost forecast | ✅ Applied |
| **069** | `admin_audit_logs` | Admin action audit trail | ✅ Applied |
| **070** | `daily_token_usage` | Daily token count (per LLM) | ✅ Applied |
| **071** | `email_logs` | Alert email history | ✅ Applied |
| **072** | `sso_sessions` | Admin SSO token state | ✅ Applied |
| **073** | `financial_statements` | Aggregated financials | ✅ Applied |
| **074** | `tenant_quotas_archive` | Quota change history | ✅ Applied |
| **075** | `platform_events` | Platform-wide event stream | ✅ Applied |
| **076** | `retention_schedules` | Data retention policy per tenant | ✅ Applied |
| **077** | `demo_sessions` (Phase 14a) | Demo customer session tracking (expiry-cap fix) | ✅ Applied |
| **078** | `audience_members.opt_in_source` | Audience join signal (inbound vs import) | ✅ Applied |
| **079** | `conversations.tenant_branch_id` (v2.1) | Branch-centric routing | ✅ Applied |
| **080** | `tenant_branches.ea_provider_id` (v2.1 PR #68) | EA binding per branch | ✅ Applied |
| **081** | `bookings.link_mode` + `pending_until` (v2.1) | Link-mode bookings + HMAC token | ✅ Applied |
| **082** | `flow_templates.description` (v2.1) | Whitelabel leak fix (lead-capture-handoff) | ✅ Applied |
| **083** | `invoices` richer model (v2.1) | period TEXT, amount_myr NUMERIC (removed period_start/end/subtotal) | ✅ Applied |
| **084** | `booking_reminders` anon GRANT (v2.1) | UPDATE permission for anon role | ✅ Applied |
| **085** | `meta_rate_limit_snapshots` (Phase 10) | Meta rate-limit header tracking | ✅ Applied |
| **086** | `platform_fixed_costs` (Phase 10) | Flat monthly infrastructure costs | ✅ Applied |
| **087** | `constraints widening` (Phase 10 QA) | Loose constraints to match actual schema | ✅ Applied |
| **088** | `tenant_primary_user` SECURITY DEFINER (Phase 10 QA) | Lookup function for admin tenant detail | ✅ Applied |

---

## Phase 15 — Product Catalog (089–090)

| # | File | Purpose | Status |
|---|------|---------|--------|
| **089** | `tenant_products` | Product registry + image blobs (Supabase) | ✅ Applied |
| **090** | `product_search_vectors` + `tenant_quotas.product_images_total_mb_cap` | Embedding search + quota | ✅ Applied |

---

## Phase 13 — AI Follow-ups (091–092)

| # | File | Purpose | Status |
|---|------|---------|--------|
| **091** | `scheduled_followups` | Pending follow-up records | ✅ Applied |
| **092** | `scheduled_followups` indexes | Indexes for cross-tenant tick (status, send_at) | ✅ Applied |

---

## Phase 10 & PR #80 — MRR Persistence (093)

| # | File | Purpose | Status |
|---|------|---------|--------|
| **093** | `platform_mrr_snapshots` | Append-only MRR history (replaces in-memory deque; PR #80 ADR-062) | ✅ Applied |

---

## Off-Meta Channels (094–096)

| # | File | Purpose | Status |
|---|------|---------|--------|
| **094** | `telegram_channel` | Telegram bot registry (bot_id, username, webhook_secret, partial-unique idx, charset CHECK) | ✅ Applied |
| **095** | `tenant_widget_config` | Website widget (read-only allowlist, tenant_widget_enabled CHECK) | ✅ Applied |
| **096** | `widget_readonly_reconcile` | Constraint audit (also adds 094 secret-CHECK fix) | ✅ Applied |

---

## Email Channel (097)

| # | File | Purpose | Status |
|---|------|---------|--------|
| **097** | `email_channel` | `email` in tenant_channels CHECK + `email_drafts` table (Chatwoot-native bot-assist; human-handling tag) | ✅ Applied |

---

## Knowledge Gap Detection (098)

| # | File | Purpose | Status |
|---|------|---------|--------|
| **098** | `knowledge_gap_misses` | KGD M1 capture table (signals, customer_question, created_at) | ✅ Applied |

---

## Admin Fixes — Quota Defaults (099)

| # | File | Purpose | Status |
|---|------|---------|--------|
| **099** | `backfill_allowed_channel_kinds` | Backfill `allowed_channel_kinds` for pro/ultra/enterprise from `['whatsapp']` to `['whatsapp','instagram','messenger','telegram','email']`; Starter stays `['whatsapp']` | ✅ Applied |

---

## Admin Hard-Delete (100–101)

| # | File | Purpose | Status |
|---|------|---------|--------|
| **100** | `hard_delete_grants` | Granted DELETE to `polaris_service` on the non-CASCADE tenant tables for the admin purge endpoint. **Superseded by 101** (the audit_logs grant violated the append-only ceiling). | ✅ Applied |
| **101** | `admin_hard_delete_fn` | `admin_hard_delete_tenant(UUID)` SECURITY DEFINER fn — purges audit_logs → broadcast_recipients → audience_members → demo_sessions → tenant_invoices → tenants as the owner, so `polaris_service` keeps NO direct DELETE on the append-only ledger (ADR-030 / migration 012). REVOKEs 100's grants; EXECUTE granted to `polaris_service`. Endpoint calls `SELECT admin_hard_delete_tenant($1)`. | ✅ Applied |

**Why a function, not a grant:** migration 012 REVOKEs UPDATE/DELETE on `audit_logs` from every role to keep the hash-chain immutable. A SECURITY DEFINER fn runs as its owner, so the one sanctioned purge path works without re-granting standing DELETE. Same pattern as `audit_log_prev_hash` (012).

---

## Indexing + Audit Retention (102–103)

| # | File | Purpose | Status |
|---|------|---------|--------|
| **102** | `webhook_events_source_received_index` | Composite index on `webhook_events(source, received_at)` — speeds up the inbound event dedup lookup | ✅ Applied |
| **103** | `audit_log_retention_default_off` | Sets `retention_enabled = false` as default on `retention_schedules` — audit logs never auto-purged unless explicitly enabled | ✅ Applied |

---

## Instagram-Login Channel (104)

| # | File | Purpose | Status |
|---|------|---------|--------|
| **104** | `instagram_login_channel` | Widens `tenant_channels.channel`, `conversations.channel`, `bookings.channel` CHECKs to admit `instagram_login`; backfills `allowed_channel_kinds` for pro/ultra/enterprise. PR #91, v1.0.23. | ✅ Applied |

---

## Agent Tool Surface + Persona Seed (105)

| # | File | Purpose | Status |
|---|------|---------|--------|
| **105** | `polaris_internal_persona` | `UPDATE tenants SET system_prompt = <Polaris persona>` on the demo/marketing tenant (idempotent WHERE NULL/blank). Demo bot keeps product knowledge after default persona went generic. PR #99 (ADR-063). | ✅ Applied |

---

## Conversations Channel ID (106)

| # | File | Purpose | Status |
|---|------|---------|--------|
| **106** | `conversations_channel_id` | Adds `channel_id UUID` column to `conversations` referencing `tenant_channels`. Used by `reply_dispatch` to pin the WABA token to the conversation's originating channel — prevents wrong-WABA misroute on multi-WABA tenants. Ships with outbound dispatch hardening (9e41bf1). | ✅ Applied |

---

## RLS on Service Tables (107)

| # | File | Purpose | Status |
|---|------|---------|--------|
| **107** | `rls_service_tables` | Enables RLS (deny-all) on 11 service-only tables + `email_drafts` (tenant policy) + `flow_templates` (permissive) as defense-in-depth vs the Supabase anon/PostgREST surface. `schema_migrations` excluded. Tenant isolation was already solid — this hardens the exposed surface. Direct-to-main `5b12463`. | ✅ LIVE (v1.0.39) |

---

## Chatwoot-Native Rehaul (108–109) — PR #102

| # | File | Purpose | Status |
|---|------|---------|--------|
| **108** | `webhook_events_chatwoot_native_status` | Widens `webhook_events.status` CHECK (from migrations 015+017) with the Chatwoot-native consumer statuses: `human_handling` (a human owns the CW conversation — bot stays silent), `no_recipient` (couldn't resolve `conversation.contact_inbox.source_id`), `chatwoot_deferred` (vestigial — the Meta-router defer branch is deleted). Drop+re-add CHECK, atomic in apply.py's per-migration txn. | ✅ LIVE (v1.0.31) |
| **109** | `drop_legacy_instagram_channel` | Narrows the `tenant_channels.channel`, `conversations.channel`, `bookings.channel` CHECKs to REMOVE legacy FB-page `instagram` — only `instagram_login` (graph.instagram.com, migration 104) remains. Safe under clean-wipe (no live `instagram` rows; any would need migrating to `instagram_login`/`messenger` first). PR #102. | ✅ LIVE (v1.0.31) |

---

## Post-Cutover Fixes (110–112) — PRs #103–#110

| # | File | Purpose | Status |
|---|------|---------|--------|
| **110** | `grant_messages_metadata_update` | Adds `GRANT UPDATE (metadata) ON messages TO polaris_anon` (column-scoped, mirrors migration 031's provider_message_id grant). Fixes `InsufficientPrivilegeError` when the pipeline's dispatch-failure recorder stamps `messages.metadata`. PR #106. | ✅ LIVE (v1.0.31) |
| **111** | `repair_audience_members_metadata_jsonb_double_encode` | Repairs `audience_members.metadata` rows corrupted by double-encoding (JSON string wrapped in JSONB). Applies `jsonb_encode_repair` helper (idempotent; detects and fixes JSON-encoded strings). Root cause: `json.dumps(metadata)` passed directly to `::jsonb` param (should pass dict directly). PR #109. | ✅ LIVE (v1.0.31) |
| **112** | `repair_tenants_settings_jsonb_double_encode` | Repairs the corrupted `tenants.settings` JSONB row (one instance found in discovery). Applies `jsonb_encode_repair` helper. Same root cause as 111 (double-encode pattern across 14 writer sites). PR #110. | ✅ LIVE (v1.0.31) |

---

## Hybrid Search Recall + Confidence (113) — PR #115

| # | File | Purpose | Status |
|---|------|---------|--------|
| **113** | `hybrid_search_or_semantics_and_distance` | Rewrites `match_knowledge_chunks_hybrid` + `match_products_hybrid` (DROP+CREATE — return type extended). **(F4)** BM25 leg: `websearch_to_tsquery` (AND-semantics, ~4/16 natural questions matched) → `plainto_tsquery` + ` & `→` | ` replacement so any matching term qualifies; ts_rank + RRF still order results. **(F2)** New `vector_distance REAL` output column (cosine distance, lower=closer; NULL for BM25-only hits) — consumed by knowledge-gap capture and the relevance floor. Vector-proximity tiebreaker on equal `rrf_score`. Grants to `polaris_anon` + `polaris_service` preserved. | ✅ LIVE (v1.0.39, operator-confirmed) |

---

## Demo System v3 + Website Leads (114–115)

| # | File | Purpose | Status |
|---|------|---------|--------|
| **114** | `demo_tenant_model` | Adds `is_demo BOOL DEFAULT false` + `demo_code TEXT UNIQUE` + `demo_duration_hours INT` + `demo_max_messages INT` to `tenants`. Demo brands excluded from MRR/health/tenant list. ADR-066. | ✅ LIVE (v1.0.45) |
| **115** | `website_leads` | Creates `website_leads` table for the Website Channel lead-capture form submissions. | ✅ LIVE (v1.0.62) |

---

## Booking Channel Widen (116) — v1.0.63

| # | File | Purpose | Status |
|---|------|---------|--------|
| **116** | `bookings_channel_widen` | Widens `bookings.channel` CHECK constraint to admit `'telegram'` and `'email'` (was `whatsapp`, `messenger`, `instagram_login` only). Required for Phase 1b of the inline conversational booking router — `create_pending_booking` can now insert for Telegram/Email-channel customers. Idempotent (DROP IF EXISTS + ADD). | Deploy status: verify operationally — this codemap tracks `main`, not the live-prod-applied set (v1.0.63) |

---

## Booking EA IDs (117) — v1.0.64

| # | File | Purpose | Status |
|---|------|---------|--------|
| **117** | `bookings_ea_ids` | Adds `ea_service_id INT` and `ea_provider_id INT` columns to `bookings`. Required by the `GET /b/{code}` short-link redirect to reconstruct the EA wizard URL deterministically — rows where both are NULL return 410 (pre-117 rows). Ships with booking-link-fixes, commit `3d0f4cb`. | Deploy status: verify operationally (v1.0.64) |

---

## Knowledge Sources Reconciler (118–119) — v1.0.78

| # | File | Purpose | Status |
|---|------|---------|--------|
| **118** | `knowledge_sources` | Creates the declarative `knowledge_sources` desired-state ledger replacing the imperative `ingestion_requests` queue model: `id, tenant_id, tenant_branch_id, source_type CHECK(url\|file\|text), source_url, storage_path, source_text, source_label, desired_hash, last_synced_hash, status CHECK(pending\|syncing\|synced\|failed), chunk_count, error JSONB, created_at, updated_at, last_synced_at, deleted_at`. Adds `knowledge_chunks.source_id UUID NULL REFERENCES knowledge_sources(id) ON DELETE CASCADE` and relaxes `knowledge_chunks.request_id` to nullable. Source-shape CHECK mirrors 018 (exactly one of url/file/text populated; text ≤ 100 KiB). RLS: `knowledge_sources_self_select/_insert/_update` (tenant-scoped); no tenant DELETE policy — soft-delete only (`deleted_at`), worker hard-deletes via BYPASSRLS `polaris_service`. | ✅ shipped v1.0.78 |
| **119** | `knowledge_sources_grants` | RLS/GRANT companion migration for 118 (pairs with the established `_grants` convention — see 011/012, 045, 050, 077, 100). | ✅ shipped v1.0.78 |

Backend: `services/knowledge_sources.py` (`insert_source`, `list_sources`, `soft_delete_source`); router `POST/GET /api/tenant/knowledge/sources` + `DELETE /sources/{id}` in `routers/tenant_knowledge.py`. The reconciler worker (external, per ADR-057) diffs `desired_hash` vs `last_synced_hash` and syncs `knowledge_chunks` on each Sync run. The legacy imperative `POST/GET/DELETE /api/tenant/knowledge/requests` endpoints are **retained for backward compatibility but marked `.. deprecated::` in the router docstrings** — new portal code targets `/sources`. See Knowledge Sources Reconciler and `docs/CODEMAPS/SHIPPED_FEATURES.md`.

---

## Audit-Remediation Indexes (120) — 2026-07-02

| # | File | Purpose | Status |
|---|------|---------|--------|

Ref: audit findings `07-db-migrations.md` #5 and #6 (2026-07-02 audit-remediation pass, 6 merges `2c4ccb9..bf3fee5`). No column/table/CHECK changes in this migration — index-only. No other schema changes shipped in the surrounding audit-remediation batches (Batches A–G are code-only).

---

## Schema Invariants (All Phases)

### RLS Policies (Per-tenant data isolation)

**Tenant-local tables** (RLS enforced):
- `messages`, `conversations`, `flows`, `broadcasts`, `audience_members`, `tenant_channels`, `tenant_quotas`, `bookings`, `scheduled_followups`, `knowledge_chunks`, `flow_runs`, `tenant_users`, etc.

**RLS policies:**
- `anon` role → filtered via `auth.uid()` → `tenant_users` → `tenant_id`
- `service` role (BYPASSRLS) → used for admin ops, internal E2E, scheduler jobs
- `platform_admin` role → admin-only endpoints

**Critical:** Migration 088 added `SECURITY DEFINER public.get_tenant_primary_user(tenant_id)` to avoid direct auth.users access (Supabase limitation).

### Key Relationships (Direction: parent → child)

| Parent | Child | Type | Note |
|--------|-------|------|------|
| `tenants` | `tenant_channels`, `tenant_quotas`, `flows`, `conversations` | 1..* | Core multi-tenancy FK |
| `channels` | `tenant_channels` | 1..* | Channel type lookup |
| `tenant_channels` | `conversations`, `webhook_events` | 1..* | Inbound routing |
| `conversations` | `messages`, `flow_runs` | 1..* | Conversation history |
| `messages` | `outbound_events`, `intent_classifications` | 1..* | Message trace |
| `flows` | `flow_runs`, `flow_versions` | 1..* | Flow versioning |
| `flows` | `broadcasts` | 1..* | Broadcast template |
| `tenant_templates` | `broadcasts` | 1..* | Template approval |
| `bookings` | `booking_reminders` | 1..* | Reminder schedule |
| `tenant_products` | `product_search_vectors` | 1..* | Product embedding |
| `scheduled_followups` | (self) | NULL | Singleton pending per conversation |
| `tenant_branches` | `conversations` | 1..* | v2.1 branch routing |
| `tenant_branches` | (ea_provider_id foreign_key) | NULL | EA binding (soft FK, no constraint) |
| `platforms.admins` | `audit_logs` | 1..* | Audit actor |
| `audit_logs` | (self via before/after) | N/A | Hash-chain immutability |

### Unique Constraints (Dedup prevention)

| Table | Constraint | Purpose |
|-------|-----------|---------|
| `tenant_channels` | `(tenant_id, channel, channel_id)` | Prevent duplicate channel binding |
| `audience_members` | Partial: `(tenant_id, channel, channel_user_id) WHERE opted_out_at IS NULL` | Prevent duplicate active members |
| `flows` | `(tenant_id, slug)` | Flow slug unique per tenant |
| `tenant_templates` | `(tenant_id, name, language)` | Template name unique per tenant per language |
| `broadcasts` | (none; rely on ULID) | Broadcasts ID unique via ULID |
| `bookings` | `(tenant_id, customer_handle, pending_until)` | One pending link-mode booking per customer |
| `broadcast_recipients` | Unique index `(broadcast_id, wa_message_id)` | Prevent duplicate sends (critical for E2E re-seeding) |
| `scheduled_followups` | `(conversation_id)` | One pending follow-up per conversation |

### CHECK Constraints (Domain validation)

| Table | Constraint | Purpose |
|-------|-----------|---------|
| `tenant_quotas` | `(plan_tier IN ('starter', 'pro', 'ultra', 'enterprise'))` | Valid plan tier |
| `tenant_channels` | `channel IN ('whatsapp','messenger','telegram','email','instagram_login')` | Valid channel — legacy FB-page `instagram` DROPPED (migration 109) |
| `conversations` | `channel IN ('whatsapp','messenger','demo','telegram','instagram_login')` | Valid conversation channel (migration 109) |
| `webhook_events` | `status IN (...,'human_handling','no_recipient','chatwoot_deferred')` | Chatwoot-native consumer statuses (migration 108) |
| `tenant_channels` | `(status IN ('active', 'inactive', 'deleted'))` | Channel lifecycle |
| `tenant_channels` | Telegram-specific: `(telegram_username ~ '^[A-Za-z0-9_-]+$' AND char_length(telegram_username) <= 256)` | Telegram username format (regex {1,256} capped at 255 by Postgres) |
| `broadcasts` | `(status IN ('draft', 'scheduled', 'running', 'completed', 'paused', 'cancelled', 'failed'))` | Broadcast state |
| `conversation_status` | `(status IN ('active', 'archived', 'handoff', 'closed'))` | Conversation lifecycle |
| `bookings` | `(status IN ('pending', 'confirmed', 'cancelled', 'completed'))` | Booking state |
| `tenant_widget_config` | `(tenant_widget_enabled IN (true, false))` | Widget enable/disable (migration 095) |

### Indexes (Query optimization)

| Table | Index | Reason |
|-------|-------|--------|
| `messages` | `(tenant_id, conversation_id, created_at DESC)` | Conversation message list |
| `messages` | `(role, created_at)` (partial: bot replies) | Bot reply quota flush |
| `conversations` | `(tenant_id, last_message_at DESC)` | List recent conversations |
| `conversations` | `(tenant_branch_id)` (v2.1) | Branch-centric routing |
| `flows` | `(tenant_id, status, created_at DESC)` | List active flows |
| `broadcasts` | `(tenant_id, status, scheduled_at)` | Scheduler tick |
| `broadcast_recipients` | `(broadcast_id, status)` | Recipient status aggregation |
| `broadcast_recipients` | `(audience_member_id)` (migration 120, apply-pending) | Per-contact broadcast-history lookup — was a bare FK with no index |
| `scheduled_followups` | `(status, send_at, tenant_id)` | Nightly worker tick (cross-tenant) |
| `audience_members` | Partial: `(tenant_id, opted_out_at) WHERE opted_out_at IS NULL` | Active member lookup |
| `knowledge_chunks` | `(tenant_id, embedding)` using pgvector | Semantic search |
| `bookings` | `(tenant_id, customer_handle)` | Link-mode lookup |
| `booking_reminders` | `(status, send_at)` | Nightly reminder tick |
| `booking_reminders` | `(tenant_id)` (migration 120, apply-pending) | RLS-filtered on every tenant-scoped query — was unindexed |
| `audit_logs` | `(entity_id, entity_type, created_at DESC)` | Chain verification |
| `product_search_vectors` | `(tenant_id, embedding)` using pgvector | Product search |

---

## Migration Apply Checklist

**Before deployment:**
1. Backup Supabase Cloud project 
2. Test migrations locally: `uv run python packages/db-migrations/apply.py`
3. Verify RLS policies applied: `SELECT * FROM pg_policies WHERE schemaname = 'public'`
4. Verify GRANT audit: `SELECT * FROM information_schema.role_table_grants WHERE grantee != 'postgres'`

---

## When to Add a New Migration

- **New table/column/constraint required?** → New numbered .sql file
- **Backward-compat data migration?** → New numbered .sql file with idempotent guardrail
- **Schema drift fix?** → New numbered .sql file (never edit prior migrations)
- **No DB changes?** → Skip migration (config-only or code-only change)

**File naming:** `NNN_descriptor.sql` (always idempotent: `CREATE TABLE IF NOT EXISTS`, `ADD COLUMN IF NOT EXISTS`, etc.)

