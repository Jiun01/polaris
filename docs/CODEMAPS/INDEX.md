# Polaris Codemaps Index

Architecture snapshots of the Polaris codebase. These are generated reference
docs — use them as a map; the source is ground truth.

## Recent Major Changes

- **Reliability (Batch A):** new `scheduled/reconcile_channels.py` — hourly self-heal for (1) unattached agent-bots after a lazy-provision blip, (2) missing channel-secret gaps (logged, not auto-fixed); `llm.generate_with_tools` exception taxonomy + OpenRouter fallback; `rag.py` Cohere network-degrade (distinct from the existing disabled-key degrade); `weekly_digest` SMTP moved off the event loop via `asyncio.to_thread`; recovery-note HTTP call deferred out of its DB transaction; inbound queue `put_nowait` + 503 backpressure; `grafana_cost.py` parse warnings; new `metrics.record_channel_reconcile_fix`.
- **Cleanup (Batch C+G):** `admin_ea.py`'s `POST /{tenant_id}/ea-providers/sync` endpoint **DELETED** (dead code, zero UI callers) — create/delete EA account endpoints remain; dead UI files removed (admin `dropdown-menu`, portal `tooltip`/`skeleton`); admin deps `react-hook-form`/`@hookform/zod` pruned; new migration **120** `broadcast_and_reminder_indexes.sql` (indexes on `broadcast_recipients.audience_member_id` + `booking_reminders.tenant_id`) — **Cloud apply pending**; `admin_billing.py` keyset pagination + explicit column lists; new `apps/api/scripts/lint_rls_grants.py` + test (pairs with the existing `lint_service_pool.py` convention).
- **Flow runtime retirement (Batch D):** `services/flow_runtime/` **fully deleted** (0 prod usage confirmed at retirement) along with routers `tenant_flows.py`/`tenant_flow_templates.py`, seed scripts, and 22 test files (net −11.6k LOC). Load-bearing pieces relocated, not deleted: `load_tenant_snapshot` → `services/tenant_snapshot.py`; `Ports`/adapters/`build_ports` → `services/agent/ports.py`; `business_hours.is_within_schedule` → `services/chatwoot/business_hours.py`; `{{var}}` `substitute()` → `services/chatwoot/templates.py`. ADR-031 model-tier routing (Haiku vs Sonnet) wired into the agent tool loop (`agent/loop.py` + `agent/pipeline_integration.py`). Flow DB tables (`flow_definitions`/`flow_versions`/`flow_runs`/`flow_templates`) intentionally left in place — a DROP-table migration is a separate manual follow-up.
- **Pipeline hot-path (Batch B):** tenant snapshot fetched once per turn and threaded through, not re-fetched; products/promos RAG calls gated behind a **default-OFF** `simple_tier_skip_rag` flag (`config.py`) for simple-intent turns; agent per-turn embed/search memoization; merged `classify_and_condense` single Haiku call (was two); quota-cap Redis cache with a 300s tier TTL; new `apps/api/polaris_api/http_client.py` — lifespan-shared `httpx.AsyncClient` singleton (mirrors the Redis singleton pattern), adopted by `meta_whatsapp.py`/`chatwoot/client.py`/`booking/client.py`/`media_download.py`; scheduled-job connection scoping tightened (`ea_settings_sync`, `retention_sweep` chunked deletes, `cost_ceiling`, `quota_flush` UNNEST batch, `invoice_overdue` bulk-fetch + per-row audit tx); `db/pools.py` service pool `_SERVICE_MAX_SIZE` raised 4→**6**.
- **Shared-types + frontend wiring (Batch F):** shared-types Pydantic→TS codegen pipeline **fixed** (`packages/shared-types/scripts/export.py` — `$defs` hoisting bug: nested `$defs` from sub-models were landing at `$defs.<Model>.$defs.<Nested>` while `$ref` strings still pointed at the top level; now hoisted correctly); `packages/shared-types/typescript/polaris-shared-types.ts` now generated **and actually consumed** by both `apps/portal` and `apps/admin` via `@polaris/shared-types`; new `apps/portal/lib/channel-kinds.ts` — canonical channel-kind vocabulary (fixes the "~10+ hand-rolled collections" drift risk flagged in SHIPPED_FEATURES.md's Instagram-Login retro) consumed by 11 portal call sites + admin's `OverrideForm`; portal `TenantRoutes` (`lib/api/routes.ts`) `templates()`/`broadcasts()` trailing-slash fix (zero live callers of the buggy no-slash form) + `products()`/`productImages()`/`knowledgeSources()` entries confirmed present; `BroadcastWizard` payload bug fixed; admin audit-verify hex-suffix field fix; new `apps/portal/lib/types/conversations.ts`.
- **Structural splits, behavior-preserving (Batch E):** `services/booking/conversational.py` (2564→**367** lines, now a thin façade) split into `intent_detect.py` / `formatting.py` / `cancel_reschedule_flow.py` / `picker_statemachine.py`; the NLU portion of `llm.py` moved to `booking/nlu.py`; `booking/provisioning.py` → `booking/branch_bindings.py`; `admin_tenants.py` (2010→**1093** lines) split out `admin_tenant_channels.py` + `admin_tenant_email.py`; `pipeline.py` (2965→**2437** lines) split out `pipeline_voice.py` + `pipeline_quota_escalation.py` + `pipeline_cw_mirror.py`; `routers/internal_e2e.py` → `polaris_api/e2e/router.py` (still mounted at `/internal/e2e`, still gated on `POLARIS_E2E_INTERNAL_SECRET`); new `utils/errors.py::api_error` helper — standardizes the `{"detail":{"code":...}}` envelope, applied at 5 `admin_billing.py` call sites.

**Booking picker multilingual arc (v1.0.73–v1.0.75 + EA r18):** `_detect_booking_language()` Malay-vocab boost; `classify_booking_intent` treats bare EN/MS/ZH dates as `"book"`; `booking/_i18n.py` static 3-lang table; `BookingPickerState.language` detected once on entry + persisted; Malay+CJK date regex. EA r18: reschedule uses `_resolve_provider_service`; cancel/reschedule/confirm chat messages TZ-localized; asset auto cache-bust via `EA_ASSET_TOKEN`. ⚠️ NOT-YET: cancel/reschedule command-reply localization (still EN).  

---

## Quick Start

Read in order:
1. **[architecture.md](architecture.md)** — Dual-pool RLS, tenant lifecycle, E2E infrastructure
2. **[backend.md](backend.md)** — All routers, services, scheduled jobs (Phase 15 product catalog through the 2026-07-02 audit-remediation batches)
3. **[data.md](data.md)** — 120 migrations, schema invariants, FK relationships
4. **[integrations.md](integrations.md)** — Chatwoot/EA/LLM/Meta/Telegram API contracts
5. **[frontend.md](frontend.md)** — Portal + Admin app-router structure
6. **[dependencies.md](dependencies.md)** — Pins, major versions, session loops
7. **[admin.md](admin.md)** — Admin app specifics, E2E helpers
8. **[portal.md](portal.md)** — Portal app + NextAuth + magic-link
9. **[billing.md](billing.md)** — Invoices, quotes, MRR rollup, cost tracking

**Total reading time:** ~10 minutes | **Each file:** token-lean (≤1500 ea)

---

## Phase Timeline & Shipped Status

| Phase | Feature | Status | Migration | Ships With |
|-------|---------|--------|-----------|------------|
| **0** | Monorepo scaffold | ✅ GA | — | v1.0 |
| **1** | Dual-pool JWT/secrets/KMS | ✅ GA | 001–032 | v1.0 |
| **2** | Webhooks/dispatcher/CW | ✅ GA | 033–040 | v1.0 |
| **3** | Knowledge ingestion/chunking | ✅ GA | 041–048 | v1.0 |
| **4** | LLM RAG pipeline | ✅ GA | 049–055 | v1.0 |
| **5** | Flow runtime (5 node types) | ✅ GA | 056–063 | v1.0 |
| **6** | Outbound/broadcasts/templates | ✅ GA | 064 | v1.0 |
| **7** | Handoff/Chatwoot bridging | ✅ GA | 065 | v1.0 |
| **8** | Portal/admin UI + routing | ✅ GA | 066–069 | v1.0 |
| **9** | Admin suite + billing + cost + system health | ✅ GA (PR-6: webhook-errors signal unreleased) | 070–088 | v2.0+ |
| **10** | STOP/quota/observability/DSAR | ✅ GA | 088 | v2.0 |
| **12** | Chatbot↔Booking integ | ✅ GA | 077–079 | v2.1 |
| **13** | AI follow-ups worker | ✅ main | 091–092 | v2.1 |
| **14a** | Demo sessions expiry-cap | ✅ main | — | v2.1 |
| **14b** | Subscription-lead flow | ✅ main | — | v2.1 |
| **15** | Product catalog + images | ✅ main | 089–090 | v2.1 |
| **20** | Meta mock + broadcast E2E | ✅ main | — | v2.1 |
| **Off-Meta 1** | Telegram channel (M1/M2) | ✅ main | 094 | v2.1 |
| **Off-Meta 2** | Website widget (M3/M4) | ✅ main | 095–096 | v2.1 |
| **v1.0.18** | Channel recovery (IG/Messenger agent-forward, multi-channel mirror) | ✅ SHIPPED | — | v1.0.18 |
| **v1.0.19** | Channel auto-provision (token capture from CW fork) | ✅ SHIPPED | — | v1.0.19 |
| **v1.0.20** | Chatwoot mirror double-send fix (#90) + admin token preview + labeled secrets (#88) | ✅ SHIPPED | — | v1.0.20 |
| **Voice M1** | Groq Whisper transcription | ✅ main (batch-merged 2026-06-06) | — | v1.0.x |
| **KGD M1** | Knowledge gap capture + dashboard | ✅ main (batch-merged 2026-06-06) | 098 | v1.0.x |
| **Email Ch.** | Chatwoot-native email assist | ✅ main (batch-merged 2026-06-06) | 097 | v1.0.x |
| **Cost Metrics** | OTel cost counters + admin dashboard | ✅ main (PR #98, d883fb2) | — | v1.0.28 |
| **HUMAN_AGENT tag** | Meta tag on human-agent outbound (PR #100) — ⚠️ REMOVED-from-Polaris by phase-10 cutover (lived in deleted `_dispatch_agent_forward`; tagging+delivery now Chatwoot-native) | ⚠️ superseded (PR #100, bf07fa6) | — | v1.0.28 |
| **Agent Tool Surface** | Tool-calling agent (ADR-063) | ✅ main (PR #99, b5bbec7) | 105 | v1.0.28 |
| **Handoff/Dispatch** | Agent attachment fwd + dispatch hardening | ✅ main (6ebeb7f, 9e41bf1) | 106 | v1.0.28 |
| **RLS service tables** | Deny-all RLS on service-only tables (defense-in-depth) | ✅ main (5b12463) | 107 | — |
| **Chatwoot-native rehaul** | Inbound flip Meta→Chatwoot→agent-bot; meta_router deleted; legacy IG dropped | ✅ main (PR #102, d500177) | 108–109 | gated |
| **/agent portal screen** | AI Agent split onto its own screen (nav 16) | ✅ main (7d1b531) | — | gated |
| **KGD interactivity** | window param (7/30/90) + /summary/examples drill-down + KnowledgeGapsList island | ✅ main (f81e3d1) | — | v1.0.39 |
| **9 portal QA fixes** | dashboard Link rows, template validation, billing/promo guards, bookings step, instagram filter, checkbox→Switch, EA admin link, help link | ✅ main (f81e3d1) | — | v1.0.39 |
| **EA r5** | /admin alias in routes.php + PHP 8.4/libicu Dockerfile fix | ✅ ea-fork (57048360) | — | v1.6.0-r5 |
| **System Health PR-6** | Webhook & Event Errors signal: 24h breakdown by status/error_class, top-N by category + recent 5 rows, benign statuses excluded server-side | ✅ branch v2/phase-9/system-health-webhook-errors (unreleased) | — | gated |
| **EA provisioning settings-seed** | `provision_admin` seeds `ea_settings` at create; deprovision clears; fixes blank calendar/booking crash on fresh EA tenant | ✅ DEPLOYED v1.0.62 | — | v1.0.62 |
| **EA-sourced booking mirror** | `settings_sync.py` mirrors EA→`tenants.settings.ea_calendar`+`booking_config`; read-only portal panel; "Update bot knowledge" sync button; hourly scheduler; lead-time from EA `book_advance_timeout`; `false` billing@ copy removed | ✅ DEPLOYED v1.0.62 | — | v1.0.62 |
| **Inline new-booking intent router** | `_try_booking_command` gains "book" command (EN/BM/ZH noun-anchored phrases + bare tokens); `_do_new_booking` + `_mint_booking_link` + `_resolve_provider_service` (branch_config→EA list fallback); Phase 2 in-chat slot picker (`BookingPickerState`, Redis `book:` prefix 30-min TTL, `_load_booking_picker_state`/`_save_booking_picker_state` in pipeline); `llm.resolve_booking_slot` (Haiku structured-pick, membership-checked); offer→NL-pick→confirm→`create_appointment`; `_resolve_service_duration_minutes`; migration 116 widens `bookings.channel` CHECK to add `telegram`+`email`; `_BOOKABLE_CHANNELS` widened (legacy `instagram` removed) | ✅ DEPLOYED v1.0.63 | 116 | v1.0.63 |
| **Booking-link-fixes** | `ea_booking_public_url` config (env `EA_BOOKING_PUBLIC_URL`, default `https://calendar.getpolaris.my`); ALL customer-facing booking/reschedule links use it instead of `ea_api_base_url` (internal). Short-link redirect router `GET /b/{code}` (`booking_redirect.py`) re-mints signed token with remaining TTL → 307→EA wizard URL; 410 on expired/unknown. `_short_booking_link` helper; `build_ea_wizard_url` shared helper in `booking/client.py`. Migration 117 adds nullable `ea_service_id`/`ea_provider_id` to `bookings`; `create_pending_booking` stores them. Reply labels `*Book here:*`/`*Reschedule here:*` + `_This link is valid for 1 hour._`. `BookingConfig.inchat_picker` field added + Switch in portal Bookings page. EA r14. | ✅ DEPLOYED v1.0.64 | 117 | v1.0.64 |
| **EA→Polaris booking-confirmation loop** | EA emitter built (polaris-ea-fork r16→r18): `Booking::index()` captures `?polaris_ref=` into script_var; `booking_http_client.js` attaches `polaris_ref` to `/booking/register` POST; `Booking::register` calls `notify_polaris_appointment_confirmed()` — HMAC-signed POST `{polaris_ref,appointment_id,start_datetime,end_datetime,service_name}` (provider-tz ISO offset) to `{POLARIS_API_BASE_URL}/v1/webhooks/ea`. Polaris receiver (`webhooks/ea_router.py`) was already present. New EA config key `polaris_api_base_url` (`application/config/polaris.php`, env `POLARIS_API_BASE_URL`). `infra/docker-compose.prod.yml` ea-php-fpm gets `POLARIS_API_BASE_URL`. Asset auto cache-bust: `infra/docker-bake.hcl` ea target passes `args = { EA_ASSET_TOKEN = "${EA_TAG}" }`; EA `Dockerfile` `ARG EA_ASSET_TOKEN` + `sed` stamps `app.php` `cache_busting_token` on every image build — eliminates stale-JS risk across releases. No new migration. | ✅ DEPLOYED v1.0.67 + EA r18 | — | v1.0.67 |
| **Picker localization v1.0.73–v1.0.75** | **v1.0.73 multilingual fixes:** `_do_inchat_book` `last_name or "Unknown"` (single-token/CJK names no longer rejected by EA); Malay months `mac\|mei\|ogos\|okt\|dis` + Malay weekdays + relative words added to `_DATE_ISH_RE`; separate `_DATE_ISH_CJK_RE` (no `\b` — CJK boundary gotcha); Malay+ZH phrases added to `_PICKER_BAIL_PHRASES`/`_ANOTHER_DATE_PHRASES`/`_EARLIEST_PHRASES`. **v1.0.74 full reply localization:** NEW `booking/_i18n.py` — static 3-lang (en/ms/zh-cn) table with WEEKDAYS/MONTHS/AMPM/AND tuples (no glibc strftime), ~38-key message registry `T`, `t(key, lang, **kw)`, CJK-aware `join_and` (顿号); `BookingPickerState.language` field (detected once on entry, persisted in to_dict/from_dict, legacy blobs default "en"); formatters `_fmt_12h`/`_pretty_time`/`_format_iso_slot`/`_format_date_label`/`_render_range_offer` gained `lang` param; ~25 picker reply sites route through `t()`; EN output byte-identical. Removed dead `_join_and`/`_DETAIL_LABELS`. Scope = picker NEW-BOOKING flow only. **v1.0.75 Malay routing fixes:** `_detect_booking_language()` + `_MALAY_LANG_HINT_RE` boost "en" guess to "ms" for terse Malay openers; `classify_booking_intent` prompt extended to treat a bare date in EN/MS/ZH as `"book"`. No DB migration. ⚠️ NOT-YET: cancel/reschedule command-reply localization (still EN). | ✅ DEPLOYED v1.0.75 | — | v1.0.75 |
| **TZ-localized booking chat messages** | `conversational.py` `_format_slot` delegates to `_notify.format_slot(start_at, tenant_timezone)` via new `_resolve_tenant_timezone`; confirm/cancel/reschedule chat messages now show slot in tenant's local timezone instead of raw UTC. | ✅ DEPLOYED v1.0.67 | — | v1.0.67 |
| **Reschedule uses _resolve_provider_service** | `_do_reschedule` in `conversational.py` now calls `_resolve_provider_service` (branch config → EA-API fallback) to build the signed `/b/{jti}` short link; `except` widened to include `asyncpg.PostgresError`. | ✅ DEPLOYED v1.0.67 | — | v1.0.67 |
| **EA "Go to booking page" → /book/{slug}** | `backend_header.php`, `booking_confirmation.php`, `booking_cancellation.php` + views now resolve the tenant slug via `resolve_tenant_slug` → `/book/{slug}`; previously fell back to `/appointments`. | ✅ DEPLOYED EA r18 | — | v1.0.67 |

---

## Services Overview

### Core Pipeline
- **`services/pipeline.py`** — Inbound dispatcher (Chatwoot-native): `run` thin wrapper + deferred CW flush; `_link_cw_conversation` (link existing CW conv, never create); rejects `provider='chatwoot'`; branches: media-only, voice, booking-command (cancel/reschedule/confirm/NEW book + Phase 2 in-chat picker), handoff. **Audit Batch E split (2026-07-02):** 2965→2437 lines; voice-note handling, quota-exhausted escalation, and Chatwoot reply-mirroring/product-image logic extracted to `services/pipeline_voice.py` / `pipeline_quota_escalation.py` / `pipeline_cw_mirror.py`. **Audit Batch B (2026-07-02):** tenant snapshot fetched once per turn and threaded through (was re-fetched); merged `classify_and_condense` single Haiku call.
- **`services/llm.py`** — Intent classify, RAG response, follow-up generation; Anthropic/OpenRouter/Cohere; stub-mode (E2E)
- **`services/rag.py`** — Hybrid search + rerank (Cohere or disabled); conversation history; vector embeddings

### Outbound & Dispatch
- **`services/reply_dispatch.py`** — Multi-channel send: WA/IG/Messenger/Telegram/text
- **`services/meta_whatsapp.py`** — Meta Graph API client (send text/image/template)
- **`services/meta_messaging.py`** — IG/Messenger conversational send (Page token)
- **`services/telegram_client.py`** — Telegram Bot API (send, webhook config)

### Flows & Templates
- `services/flow_runtime/` — RETIRED 2026-07-02 (audit Batch D); deleted, along with
  routers `tenant_flows.py`/`tenant_flow_templates.py` and 22 test files (net −11.6k
  LOC). Survivors (relocated, not deleted): `services/agent/ports.py`
  (Ports/adapters/build_ports), `services/tenant_snapshot.py` (load_tenant_snapshot),
  `services/chatwoot/business_hours.py`, `services/chatwoot/templates.py` (`{{var}}`
  substitute helper). Flow DB tables (`flow_definitions`/`flow_versions`/`flow_runs`/
  `flow_templates`) left in place — a DROP-table migration is a separate follow-up.
- **`services/templates/`** — WhatsApp broadcast message templates (unrelated package,
  migration 041); backs `tenant_templates.py` + `webhooks/meta_template_status_router.py`
- **`routers/tenant_templates.py`** — CRUD + approval status

### Channels & Integrations
- **`services/channels/`** — Provision (WhatsApp, IG, Messenger, Telegram, Widget); v1.0.19 auto-capture tokens
- **`services/telegram_client.py`** — NEW (v2.1) — Telegram Bot API + webhook setup
- **`services/widget.py`** — NEW (v2.1) — Website widget config (read-only allowlist)
- **`services/chatwoot/`** — `client` (mirror via linked conv), `handoff`, `agent_bot` (NEW rehaul: auto-attach platform bot), `label_sync` (two-label bot/human-handling), `recovery`, `reverse_handoff`
- **`services/webhook_resolver.py`** — v1.0.18: AgentForwardContext extended for IG/Messenger/Telegram page/bot tokens

### Bookings & Reminders
- **`services/booking/`** — Config, link-mode HMAC tokens, EA binding, cancel/reschedule/confirm, **new-booking intent** (link mode Phase 1 + in-chat slot picker Phase 2 dark), conversational handlers; `slot_picker_state.py` (BookingPickerState, Redis `book:` prefix 30-min TTL); `client.py::build_ea_wizard_url` shared helper (v1.0.64); `_notify.format_slot` + `_resolve_tenant_timezone` for TZ-localized chat messages (v1.0.67); `_do_reschedule` now uses `_resolve_provider_service` (v1.0.67). **Audit Batch E (2026-07-02) structural split:** `conversational.py` (2564→367 lines) is now a thin façade over new `intent_detect.py` / `formatting.py` / `cancel_reschedule_flow.py` / `picker_statemachine.py`; NLU helpers moved out of `llm.py` into `booking/nlu.py`; `provisioning.py` renamed `branch_bindings.py` (`sync_tenant_branch_ea_bindings`). Behavior-preserving pure moves — no functional change.
- **`webhooks/ea_router.py`** — `POST /v1/webhooks/ea` — EA appointment-confirm callback; HMAC `X-Polaris-Signature` over raw body via `ea_webhook_hmac_secret`; verifies `polaris_ref` binding token → `link_flow.confirm_booking` (idempotent, flips `bookings` row to `confirmed`)
- **`services/booking_reminders.py`** — Scheduled send (text or template); worker loop

### Follow-ups & Engagement
- **`services/followups/`** — timing, eligibility, scheduler, worker (at-least-once, cross-tenant tick)
- **`services/email_assist*.py`** — Email draft + send (Chatwoot-native; ✅ main)

### Knowledge & RAG
- **`services/knowledge_ingestion.py`** — File upload + chunking + embedding → Supabase vector
- **`services/knowledge_search.py`** — Hybrid search + rank
- **`services/knowledge_gaps/`** — Capture signals (no_retrieval, handoff) + aggregate → dashboard (✅ main)

### Products & Media
- **`services/products/`** — NEW (Phase 15) — storage (Supabase bucket), search (embedding + rank)
- **`services/media_download.py`** — Per-channel audio fetch (WA/TG/IG/Messenger) (✅ main)
- **`services/transcription.py`** — Groq Whisper API; auto-detect EN/MS/ZH (✅ main)
- **`services/agent/`** — Tool-calling agent: config, loop, pipeline_integration, tools/ (✅ main, PR #99, ADR-063)
- **`metrics.py`** — OTel cost counters for LLM/Cohere/embed calls (✅ main, PR #98)

### Admin & Audit
- **`services/audit.py`** — Append-only ledger; verification chain (hash chaining)
- **`services/secrets.py`** — KMS encrypt/decrypt (AES-256-GCM); rotation via admin

### Billing & Cost
- **`services/billing/`** — Invoice sequencer, proration SSoT, MRR calculation
- **`services/cost_ceiling.py`** — Soft-cap (80%, email alert) + hard-cap (Sentry page) enforcement
- **`services/mrr_rollup.py`** — Compute MRR; persist to `platform_mrr_snapshots` (migration 093)

### Observability
- **`services/meta_usage.py`** — Record Meta rate-limit headers → `meta_rate_limit_snapshots`
- **`observability.py`** — OTel + Sentry config; FastAPI/asyncpg/httpx instrumentation

### Utilities
- **`services/stop_keyword.py`** — EN/BM/ZH exact match → opt-out
- **`services/quota.py`** — Redis counter: Starter 5k / Pro 25k / Ultra 60k per month
- **`services/webhook_resolver.py`** — Resolve tenant from webhook; agent forward context (AC-12-6)
- **`services/demo_sessions.py`** — Demo mode session tracking (Phase 14a expiry-cap)
- **`services/meta_mock_store.py`** — NEW (Phase 20 E2E) — In-memory capture store for mock Meta API

---

## Routers (40 total)

### Public / Health
- `health.py` — `GET /v1/health`
- `meta_mock.py` — `POST /meta-mock/{full_path}` (E2E only, Phase 20)
- `widget_public.py` — `GET /api/v1/widget/{key}/config` (public, read-only)
- `public_audience_optout.py` — `GET /api/v1/audience/opted-out` (portal JWT)
- `booking_redirect.py` — `GET /b/{code}` (public, no auth) — short-link redirect: service-pool lookup by `jti` (binding_token), re-mints signed token with REMAINING TTL, 307→EA wizard URL; 410 on expired/unknown/null-ids (v1.0.64)

### Webhooks
- `webhooks/chatwoot_router.py` — **PRIMARY inbound** (rehaul): agent-bot `message_created` → human-handling gate → resolve real channel → pipeline; + reverse-handoff resume. Human-agent replies are NOT forwarded by Polaris (phase-10: `_dispatch_agent_forward` deleted — CW delivers natively); only `_record_email_agent_reply` tracks email-draft acceptance on email inboxes
- ~~`webhooks/telegram_router.py`~~ — **DELETED** (uncommitted 2026-06-12): Telegram is a native Chatwoot `Channel::Telegram` now, so inbound rides `chatwoot_router` (channel_kind 'telegram'); the own-webhook router + `resolve_telegram` + `_telegram_extract.py` are gone
- `webhooks/ea_router.py` — `POST /v1/webhooks/ea` — EA appointment-confirm callback (booking-confirmation loop, v1.0.67): HMAC `X-Polaris-Signature` (raw body, `ea_webhook_hmac_secret`); verifies `polaris_ref` binding token → `link_flow.confirm_booking`; idempotent on re-delivery; 401 on bad HMAC, 400 on unknown ref
- `webhooks/meta_template_status_router.py` — Template status (via fork-relay)
- `webhooks/wa_status_router.py` — Message delivery/read status (via fork-relay)
- `webhooks/wa_account_router.py` — Account warnings (via fork-relay)
- ~~`webhooks/meta_router.py`~~ — **DELETED** (rehaul); no direct Meta→Polaris customer-message inbound

### Tenant APIs (17 routes)
| Route | Purpose |
|-------|---------|
| `tenant_knowledge` | Ingestion request CRUD + capacity |
| `tenant_templates` | Template CRUD + approval |
| `tenant_channels` | Channel list (read-only) |
| `tenant_conversations` | Conversation detail + message history |
| `tenant_products` | NEW (Phase 15) — Product CRUD + image upload + search |
| `tenant_broadcasts` | Schedule/pause/resume/cancel |
| `tenant_audience` | Import + opted-out list |
| `tenant_bookings` | Config + conversational handlers |
| `tenant_followups` | Config (enable/timing/quiet hours) |
| `tenant_branches` | Branch CRUD (v2.1 EA routing) |
| `tenant_promotions` | Promotion CRUD |
| `tenant_plan_change_requests` | PCR workflow |
| `tenant_privacy` | DSAR export/delete |
| `tenant_notifications` | Notification list + mark-read |
| `tenant_dashboard` | Summary KPIs |
| `tenant_settings` | Settings upsert |
| `tenant_knowledge_gaps` | Summary dashboard (Ultra-only); GET /summary?window_days={7\|30\|90}; GET /summary/examples?q=&window_days= (NEW drill-down) |
| `tenant_widget` | Widget config (lazy key, allowlist) |
| `tenant_messenger_profile` | Messenger profile setup |

### Admin APIs (17 routes)
| Route | Purpose |
|-------|---------|
| `admin_tenants` | CRUD + suspend/unsuspend/delete + channel override (2010→1093 lines, audit Batch E split) |
| `admin_tenant_channels` | NEW (audit Batch E split from admin_tenants) — channel add/remove/inbox-info/email-config |
| `admin_tenant_email` | NEW (audit Batch E split from admin_tenants) — email credential rotation |
| `admin_quotas` | Usage + override |
| `admin_secrets` | Rotation trigger |
| `admin_audit` | Audit log + verification |
| `admin_impersonation` | JWT mint + clear |
| `admin_billing` | Invoice CRUD + mark-paid + void + PDF (audit Batch C: keyset pagination + explicit columns) |
| `admin_quotes` | Quote CRUD + PDF + convert |
| `admin_plan_changes` | PCR approve/reject |
| `admin_cost` | Per-tenant cost dashboard (Mimir proxy) |
| `admin_financials` | MRR + trend (read-only) |
| `admin_health` | System health snapshot (cached 60s) |
| `admin_ea` | Create/delete tenant EA account (sync endpoint REMOVED 2026-07-02 — dead code) |
| `admin_ingestion` | Chunking config + force-run |
| `admin_demos` | Demo config CRUD + session management |
| `admin_leads` (`website_leads.py`) | Website Channel lead-capture admin surface |

### Internal E2E (20+ endpoints)
**Router relocated (audit Batch E, 2026-07-02):** `routers/internal_e2e.py` → `polaris_api/e2e/router.py`. Still mounted at `/internal/e2e`, still gated on `POLARIS_E2E_INTERNAL_SECRET` — path and behavior unchanged, only the module's location moved.

**Gated by `POLARIS_E2E_INTERNAL_SECRET`:**
- `mint-tenant-jwt` — Tenant JWT for RLS tests
- `mint-admin-jwt` — Platform admin JWT
- `quota-set` — Force Redis counter
- `broadcast-e2e-seed` — Upsert template + broadcast + recipients
- `broadcast-e2e-tick` — Drain due broadcasts (DB-driven, not Redis)
- `broadcast-recipients-query` — Fetch recipient rows
- `branches-query` — EA bindings
- `demo-*` — Session management
- `audience-query` — Cross-tenant member lookup
- `bookings*` — Booking CRUD + reminder control
- `audit-*` — Audit log queries
- `meta-outbound-*` — Mock API store (clear/query)
- `meta-mock` — Catch-all POST router (Phase 20)
- Plus 7 more from Phase 14a/14b/20 E2E

---

## Scheduled Jobs (9 total in `scheduled/`; 7 ADR-062-locked jobs below + 2 additional — see note)

| Job | Time (MYT) | Purpose | Lock Key |
|-----|------------|---------|----------|
| `quota_flush` | 00:05 | Redis → DB bot_replies count (Batch B: UNNEST batch write) | `quota_flush` |
| `retention_sweep` | 02:00 | Hard-delete expired messages + audit (Batch B: chunked deletes) | `retention_sweep` |
| `mrr_rollup` | 02:00 | Compute + persist MRR snapshot (migration 093) | `mrr_rollup` |
| `cost_ceiling_check` | 03:00 | Cost alerting (75% email, hard-cap Sentry) (Batch B: conn scoping tightened) | `cost_ceiling_check` |
| `audit_chain_verify` | 04:00 | Audit hash chain integrity check | `audit_chain_verify` |
| `invoice_overdue` | 09:00 | Mark overdue invoices (Batch B: bulk-fetch + per-row audit tx) | `invoice_overdue` |
| `weekly_digest` | Mon 09:00 | Operator email digest (Batch A: SMTP moved off event loop via `asyncio.to_thread`) | `weekly_digest` |

**All 7 above wrapped in** `scheduled/_locking.run_with_advisory_lock(lock_key)` — session-level Postgres advisory lock per job.

**2 additional jobs in `scheduled/` (pre-existing/new, locking not re-verified this pass):**
- `ea_settings_sync.py` — hourly mirror of EA-side booking settings → tenant `settings.ea_calendar`/`booking_config` (see project_ea_provisioning notes; predates this session, previously undocumented here).
- `reconcile_channels.py` — **NEW (Batch A, 2026-07-02)** — hourly; self-heals unattached agent-bots + logs missing-secret channel gaps. See `backend.md` §Nightly Jobs for detail.

---

## Key Recent Changes

### Phase 10: Supabase Ban Resilience (✅ main, v1.0.53, commit 8d14839 — no migrations)
- **Portal auth DB:** Circuit breaker on transient connection failures (ECONNREFUSED / ETIMEDOUT / socket errors). 5 consecutive failures → trip for 20s, fail-fast without opening new sockets. Prevents amplification of Supabase IP bans during role-password drift.
- **New:** `lib/auth/db.ts` — `portalQuery()` wrapper, `DbUnavailableError`, `isTransientConnectionError()` classifier, per-process breaker state
- **New:** `components/shell/ServiceUnavailable.tsx` — graceful "try again" page on circuit open (avoids /login redirect loop that repeats the failure)
- **New:** `infra/deploy/role-sync.sh` — idempotent re-sync of Supabase `polaris_anon`/`polaris_service` passwords from `.env` (root cause: Supabase resets custom-role creds server-side; between deploys nothing realigns them)
- **New:** `infra/deploy/systemd/polaris-role-sync.{service,timer}` — hourly systemd timer (recovery-latency knob, not ban-prevention). Install once on VPS: `sudo cp systemd/* /etc/systemd/system && systemctl enable --now polaris-role-sync.timer`.
- **Updated:** `infra/deploy/README.md` documents the timer + recovery procedure + test commands

### Phase 15: Product Catalog (✅ main, migrations 089–090)
- **Services:** `services/products/{storage,search}.py` — Supabase bucket upload, embedding search
- **Router:** `tenant_products.py` — CRUD + multipart image upload + signed URLs + blob reap
- **Pipeline:** `llm_reply.py` attaches product images (gated on `send_product_images`)
- **Outbound:** `meta_messaging.py` — IG/Messenger image send via Page token

### Phase 13: AI Follow-ups (✅ main, migrations 091–092)
- **Services:** `services/followups/{timing,eligibility,scheduler,worker}.py`
- **Worker:** At-least-once in-process loop; cross-tenant tick; quiet hours respect
- **Config:** Tenant-gated (default off); 12–23h window, respect tenant quiet hours

### MRR Persistence + Scheduler Locks (✅ main, migration 093, PR #80)
- **ADR-062:** All 7 jobs locked via Postgres session-level advisory lock
- **Migration 093:** `platform_mrr_snapshots` (append-only); replaces in-memory deque
- **Router:** `GET /api/admin/financials/mrr` — read-only live + trend

### Off-Meta Channels (✅ main, migrations 094–096)
- **Telegram M1/M2:** First-class channel; webhook→pipeline; Pro+ gated
  - Migration 094: `telegram` in CHECKs; `telegram_bot_id`, `username`, `webhook_secret` cols
  - Service: `services/telegram_client.py` — Bot API + webhook setup
  - Router: `admin_tenants.py::connect_telegram`
- **Website Widget M3/M4:** Read-only allowlist; no redirect
  - Migration 095–096: `tenant_widget_config` (read-only); `website_widget_enabled` CHECK
  - Services: `services/widget.py` — Config + allowlist validation
  - Router: `tenant_widget.py` (lazy key), `widget_public.py` (public GET)

### Voice Notes M1 (✅ main, batch-merged 2026-06-06)
- **Services:** `services/media_download.py` + `services/transcription.py`
- **Pipeline:** Voice branch (before media-only branch)
- **Tier-gate:** Pro+ only (`voice_note_tiers`)

### Knowledge Gap Detection M1 (✅ main, batch-merged 2026-06-06, migration 098)
- **Signals:** `no_retrieval` (zero RAG) + `handoff` (real Q → human); `low_retrieval` lever (default off)
- **Services:** `services/knowledge_gaps/{capture,aggregate}.py`
- **Router:** `tenant_knowledge_gaps.py` — `GET /summary` (Ultra-only, 403 below)
- **Capture:** SAVEPOINT fail-soft; threads to handoff/Phase-4/flow sites

### Email Channel (✅ main, batch-merged 2026-06-06, migration 097)
- **Chatwoot-native:** No new IMAP/SMTP stack; CW owns email
- **Services:** `services/email_assist*.py` — Draft + send; SAVEPOINT-recover on failures (6ebeb7f)
- **Router:** `tenant_email.py` — Config (per-tenant prompt)
- **Worker:** Email-assist consumer (dedicated); BYO mailbox (app-password / OAuth)

### Agent Tool Surface (✅ main, PR #99, ADR-063, b5bbec7, migration 105)
- **Services:** `services/agent/` — config, loop, pipeline_integration, result, tools/ (4 tools)
- **Prompt rewrite:** `_llm_prompts.py` — layered assembly; `[HANDOFF_TRIGGER]` deleted; `_SAFETY_GUARDS`
- **Portal:** AgentSection in SettingsForm; flow-builder UI removed (sidebar → 15 entries)

### Cost Metrics Dashboard (✅ main, PR #98, d883fb2)
- **New:** `metrics.py` (OTel counters) + updated `grafana_cost.py` (USD prices, `USD_TO_MYR_RATE=4.15`)
- **Call sites:** `llm.generate_response`, classify, rerank, embed

### HUMAN_AGENT Meta Tag (PR #100, bf07fa6 — ⚠️ REMOVED-from-Polaris by phase-10 cutover)
- **What (historical):** Tagged Messenger + IG-Login human-agent outbound; required for Meta App Review. The tag lived INSIDE `_dispatch_agent_forward`'s send branches.
- **Now:** `_dispatch_agent_forward` is DELETED (phase-10 Chatwoot-native cutover) — Polaris no longer forwards human-agent replies or applies the tag; delivery+tagging are Chatwoot-native. The `human_agent` kwarg still exists in `meta_messaging.send_text/send_image` but is no longer called.

### Handoff & Dispatch Hardening (✅ main, 6ebeb7f + 9e41bf1)
- **6ebeb7f:** Agent attachments forwarded; reverse-handoff resume guard; email-assist SAVEPOINT-recover
- **9e41bf1:** Silent send failures surfaced; WABA token pinned to conversation's channel_id (migration 106)

### Chatwoot-Native Rehaul (✅ main, PR #102 d500177 + fixups + /agent screen, migrations 108–109)
- **Inbound flip:** Meta→Chatwoot→agent-bot webhook→`chatwoot_router`→pipeline→reply direct to Meta + mirror w/ `source_id`. `meta_router.py` / `_meta_extract.py` / `_demo_branch.py` DELETED.
- **Pipeline:** `run` thin wrapper over `_run_inner` + deferred CW flush (labels/note out of txn); `_link_cw_conversation` LINKS existing CW conv (never create — fixed dup-conversation bug).
- **New:** `chatwoot/agent_bot.py` (auto-attach), two-label sync, human-handling gate (`status=='pending'`), echo-drop invariant, fork-relay for non-message WA events.
- **Channels:** legacy FB-page `instagram` dropped (migration 109); only `instagram_login` remains.
- **/agent portal screen** (7d1b531): AI Agent split onto its own screen; nav 15→16.
- ⚠️ **VPS deploy GATED** on the Meta→Chatwoot cutover (`../docs/internal-ops/runbooks/chatwoot-native-cutover.md`).

---

## DB State

Prior: 118 (`knowledge_sources` — declarative desired-state ledger for the ingestion reconciler redesign: `knowledge_sources` table + `knowledge_chunks.source_id` FK + relaxes `knowledge_chunks.request_id` to nullable) + 119 (`knowledge_sources_grants` — RLS/GRANT companion migration for 118). See Knowledge Sources Reconciler.
**Applied migrations (on main):** 001–120 (through `120_broadcast_and_reminder_indexes.sql`, verified by listing `packages/db-migrations/`); **next = 121**. Confirmed Cloud-applied through 119; 120 is main-only pending manual apply.
**Schema:** v1.0.55 adds no migration — `persona_blocks` lives in existing `tenants.settings` JSONB. v1.0.63 adds migration 116. v1.0.64 adds migration 117. v1.0.67 adds no new migration. v1.0.73–v1.0.75 add no migration (app-only: `_i18n.py`, `BookingPickerState.language`, Malay detection, classifier prompt). v1.0.78 adds migrations 118+119 (knowledge_sources reconciler). 2026-07-02 audit-remediation batch adds migration 120 (indexes only, no column/table changes) — no other schema changes in this batch (Batches A–G are all code-only).

---

## When to Regenerate

Regenerate the codemaps after:
- Phase closes
- Migration count changes by >2
- Major service added/removed
- Scheduled job added/removed
- Router count changes by >3

**Last regenerated:** 2026-07-02
