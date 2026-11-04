
# Shipped Features Reference

A running reference of major features and the releases they shipped in.

## Post-Remediation Chips (2026-07-03)

**What:** Four independent follow-ups landed the same/next day after the 6-batch audit-remediation

**RAG eval harness (merged, never run):** NEW `apps/api/polaris_api/services/rag_eval.py`
(pure `hit_rank`/`recall_at_k`/`mrr`/`summarize` scoring, DB-free) + `apps/api/scripts/rag_eval.py`
(offline CLI against the REAL `services.rag.search_hybrid` retrieval path — always exits 0, a
report not a gate) + `apps/api/tests/eval/golden.jsonl` (36 hand-curated rows: 26 en/5 ms/5 zh)
+ 19 unit tests on the scorer. **No baseline has been run against real/prod data** — recall@k/MRR
numbers for the actual KB are unknown. See `docs/CODEMAPS/services.md` §`rag_eval.py`.

**Sonnet bumped `claude-sonnet-4-6` → `claude-sonnet-5`; thinking pinned OFF for hard tier:**
`config.py`'s `llm_sonnet_model` default changed; `_model_for_intent` (`services/llm.py`) now
returns a `(model, model_used, thinking_param)` 3-tuple — hard tier gets
`thinking={"type":"disabled"}` (typed via `ThinkingConfigParam`), everything else gets no
`thinking` kwarg. Threaded into both the streaming call and its non-stream retry in
`_generate_sonnet_streaming`. **Known gap (now fixed in the working tree, uncommitted as of
this doc pass):** `generate_with_tools` (agent tool-use path) originally discarded the
thinking element entirely at merge time — the pin-off did not reach agent-loop Sonnet calls,
only the classic `generate_response` hard-tier path. Verify commit status before treating this
as fully closed.

**TenantRoutes completion sweep + admin billing load-more:** 11 new portal `TenantRoutes`
builders across 27 call sites in 19 files (closes the last hand-rolled `/api/tenant/*` literals
Batch F's sweep missed); admin invoice PDF link fixed (was 404ing a raw FastAPI-only path); NEW
`apps/admin/components/billing/InvoiceTableWithLoadMore.tsx` + NEW shared
`apps/admin/lib/hooks/useCursorLoadMore.ts` cursor-pagination hook (also adopted by the
pre-existing `AuditTable.tsx`); `InvoiceTable.tsx` client-side sort wrapped in `useMemo`.

**Simplify pass (13 zero-behavior-change quality fixes):** NEW
`services/channels/secret_kinds.py` (3-entry whatsapp/messenger/instagram_login → secret-kind
map, replaces 4 hand-copied dicts across `lazy_provision.py`/`reconcile_channels.py`/
`provision.py`/`admin_tenants.py`; `telegram` deliberately excluded); `services/agent/ports.py`
gained a private `_embed_cached()` helper (dedupes identical embed-memoization logic in
`RAGAdapter.search`/`ProductsAdapter.search_products`) and a private `_require(value, name)`
helper backing `Ports`' 5 wired-or-raise properties; `booking/_notify.py::render_named` now
delegates to the shared `chatwoot/templates.py::substitute()` helper (was a second hand-rolled
`{{var}}` implementation); `scheduled/reconcile_channels.py` drops dead `_ChannelRow` + acquires
ONE connection for its whole loop (was per-iteration); assorted dead-code removal (unused
`get_http_client` hard-fail variant, `IncomingMessage` re-export, `llm_service` re-export,
`_ChunkLike` type).

**SMTP MailHog tolerance fix:** `weekly_digest.py` + `cost_ceiling.py` SMTP senders now tolerate
a dev relay (MailHog) that doesn't advertise STARTTLS, falling through to cleartext ONLY when no
credentials would be sent (mirrors `portal_invite.py`'s existing pattern); a real STARTTLS
failure with prod credentials set still hard-fails. Repo-root `.env` routes all 3 SMTP credential
groups to local MailHog.

**Note:** the RAG eval baseline has not been run against production data, and
`api_error()` adoption remains partial elsewhere in the API.

---

## Audit-Remediation Batch (2026-07-02)

**What:** A cross-cutting reliability + cleanup + performance pass across 7 batches (A–G).
Code-only except migration 120 (index-only, no column/table changes).

**Batch A — Reliability:**
- NEW `scheduled/reconcile_channels.py` — hourly self-heal for (1) unattached agent-bots after a lazy-provision blip, (2) missing-secret channel gaps (logged via `metrics.record_channel_reconcile_fix`, not auto-fixed — needs the original inbox context lazy_provision had).
- `llm.py::generate_with_tools` — proper exception taxonomy + OpenRouter fallback (previously a bare catch-all with no fallback, unlike the classic `generate_response` path).
- `rag.py` — Cohere NETWORK-failure degrade (`CohereApiError`/`httpx.TransportError`) as a separate catch alongside the pre-existing disabled-key degrade; previously a transient Cohere outage (as opposed to a blank API key) would crash an Ultra/Enterprise reply.
- `weekly_digest.py` — SMTP send moved off the event loop via `asyncio.to_thread`.
- Recovery-note HTTP call deferred out of its DB transaction (was holding a connection across a Chatwoot HTTP round-trip).
- Inbound queue — `put_nowait` + 503 backpressure (was unbounded blocking put).
- `grafana_cost.py` — parse WARNING-level logging added for malformed Mimir responses.

**Batch B — Pipeline hot-path:**
- Tenant snapshot fetched ONCE per turn and threaded through (was re-fetched at multiple points).
- Products/promos RAG fetch gated behind NEW `config.py::simple_tier_skip_rag` flag (**default OFF**) — when enabled, skips the RAG round-trip for `simple`-tier turns.
- Agent tool loop — per-turn embed/search memoization.
- `classify_and_condense` — merges the classify-intent Haiku call and the follow-up-condensation Haiku call into one round-trip (was two).
- Quota-cap now Redis-cached with a 300s tier TTL (was re-read every turn).
- NEW `apps/api/polaris_api/http_client.py` — lifespan-shared `httpx.AsyncClient` singleton, adopted by `meta_whatsapp.py`/`chatwoot/client.py`/`booking/client.py`/`media_download.py` (previously a fresh client — fresh TLS handshake — per outbound call).
- Scheduled-job connection scoping tightened: `ea_settings_sync` (per-tenant fresh connection), `retention_sweep` (chunked deletes), `cost_ceiling_check`, `quota_flush` (UNNEST batch write), `invoice_overdue` (bulk-fetch + per-row audit tx).
- `db/pools.py::_SERVICE_MAX_SIZE` raised 4→6 (anon pool unchanged at 10).

**Batch C + G — Cleanup:**
- `admin_ea.py`'s `POST /{tenant_id}/ea-providers/sync` endpoint DELETED (dead code, zero UI callers) — create/delete EA account endpoints remain; branch↔EA binding now reconciles automatically at provisioning time via `services/booking/branch_bindings.py::sync_tenant_branch_ea_bindings`.
- Dead UI files removed: admin `components/ui/dropdown-menu.tsx`, portal `components/ui/{tooltip,skeleton}.tsx`.
- Admin `package.json` — `react-hook-form` + `@hookform/zod` pruned (unused).
- `admin_billing.py` — keyset pagination + explicit column lists (was offset-based / `SELECT *`).
- NEW `apps/api/scripts/lint_rls_grants.py` + test — runtime CI check pairing every RLS-policy'd table with a live GRANT, modeled on the migration 118→119 bug class.

**Batch D — Flow runtime full retirement:**
- `services/flow_runtime/` deleted outright (0 prod usage confirmed at retirement — the portal flow-builder UI was already removed under ADR-063 well before this) along with routers `tenant_flows.py`/`tenant_flow_templates.py`, seed scripts, and 22 test files (net −11.6k LOC).
- Relocated (not deleted) load-bearing pieces: `load_tenant_snapshot` → `services/tenant_snapshot.py`; `Ports`/adapters/`build_ports` → `services/agent/ports.py`; `business_hours.is_within_schedule` → `services/chatwoot/business_hours.py`; `{{var}}` `substitute()` → `services/chatwoot/templates.py`.
- ADR-031 model-tier routing (Haiku vs Sonnet) wired into the agent tool loop (`agent/loop.py` + `agent/pipeline_integration.py`) — previously hardcoded to Haiku for every agent-enabled tenant.
- Flow DB tables (`flow_definitions`/`flow_versions`/`flow_runs`/`flow_templates`) intentionally LEFT IN PLACE — a DROP-table migration is a deliberate separate follow-up.

**Batch E — Structural splits (pure extraction, no behavior change):**
- `services/booking/conversational.py` (2564→367 lines, now a thin façade) → `intent_detect.py` / `formatting.py` / `cancel_reschedule_flow.py` / `picker_statemachine.py`.
- Booking NLU helpers moved out of `llm.py` → `booking/nlu.py`.
- `booking/provisioning.py` renamed `booking/branch_bindings.py`.
- `admin_tenants.py` (2010→1093 lines) → `admin_tenant_channels.py` + `admin_tenant_email.py` (same `/api/admin/tenants` prefix, no path changes).
- `pipeline.py` (2965→2437 lines) → `pipeline_voice.py` + `pipeline_quota_escalation.py` + `pipeline_cw_mirror.py`.
- `routers/internal_e2e.py` → `polaris_api/e2e/router.py` (same `/internal/e2e` mount, same `POLARIS_E2E_INTERNAL_SECRET` gate).
- NEW `utils/errors.py::api_error()` helper — standardizes the `{"detail":{"code":...}}` envelope; applied at 5 `admin_billing.py` call sites (not yet swept elsewhere).

**Batch F — Shared-types + frontend wiring:**
- `packages/shared-types/scripts/export.py` — fixed a `$defs`-hoisting bug (nested `$defs` from sub-models were landing under `$defs.<Model>.$defs.<Nested>` while `$ref` strings pointed at the top level).
- `packages/shared-types/typescript/polaris-shared-types.ts` now correctly generated AND actually imported by both `apps/portal` and `apps/admin` via `@polaris/shared-types`.
- NEW `apps/portal/lib/channel-kinds.ts` — canonical channel-kind vocabulary, consumed by 11 portal call sites + admin's `OverrideForm`.
- Admin `TenantRoutes` / portal `lib/api/routes.ts` — `templates()`/`broadcasts()` trailing-slash fix (zero live callers of the old form); `products()`/`productImages()`/`knowledgeSources()` entries confirmed present.
- `BroadcastWizard.tsx` payload bug fixed (was posting a malformed/incomplete body).
- Admin audit-verify page — hex-suffix field-name display fix.
- NEW `apps/portal/lib/types/conversations.ts`.

---

## Knowledge Sources Reconciler ✅ (SHIPPED v1.0.78 — migrations 118–119)

**What:** Replaces the imperative `ingestion_requests` queue model with a **declarative desired-state ledger**. The portal (or any future ingestion client) writes a `knowledge_sources` row per source (url/file/text) carrying a caller-computed `desired_hash`; an external reconciler worker (per ADR-057, outside this monorepo) diffs `desired_hash` vs `last_synced_hash` and only re-processes sources that actually changed, instead of re-running a queue item every time.

**Migrations:**
- `packages/db-migrations/118_knowledge_sources.sql` — creates `knowledge_sources` (`id, tenant_id, tenant_branch_id, source_type, source_url, storage_path, source_text, source_label, desired_hash, last_synced_hash, status, chunk_count, error, created_at, updated_at, last_synced_at, deleted_at`); adds `knowledge_chunks.source_id UUID NULL REFERENCES knowledge_sources(id) ON DELETE CASCADE`; relaxes `knowledge_chunks.request_id` to nullable. RLS mirrors 018 (self_select/self_insert/self_update; no tenant DELETE — soft-delete only).
- `packages/db-migrations/119_knowledge_sources_grants.sql` — RLS/GRANT companion (pairs with the established `_grants` migration convention).

**Backend:**
- `apps/api/polaris_api/services/knowledge_sources.py` — `insert_source()` (capacity-gated INSERT, `status='pending'`), `list_sources()` (excludes soft-deleted), `soft_delete_source()` (sets `deleted_at`; 404 if already gone).
- `apps/api/polaris_api/routers/tenant_knowledge.py` — new endpoints: `POST /api/tenant/knowledge/sources`, `GET /api/tenant/knowledge/sources`, `DELETE /api/tenant/knowledge/sources/{source_id}`.
- **Legacy imperative endpoints retained but deprecated:** `POST/GET/DELETE /api/tenant/knowledge/requests` and `GET /api/tenant/knowledge/requests/{id}` all carry `.. deprecated::` docstrings pointing callers at `/sources`. Not removed — kept for backward compatibility.
- The legacy `POST /api/tenant/knowledge/uploads/sign` signed-upload endpoint (Supabase Storage, 15-min signed PUT URL) is **unchanged and still used by the file-upload flow** — a file source's `desired_hash` is computed browser-side before the sign step, then the resulting `storage_path` is passed into `POST /sources`. The signed-upload step and the `/sources` create-flow are complementary (sign → PUT to storage → declare the source), not competing paths.

**Capacity gate:** `insert_source` reuses the same `_capacity_exceeded` / `estimate_current_usage_mb` / `get_effective_cap_mb` helpers as the legacy `enqueue_request` path, so both submission surfaces share identical 409 semantics and cap arithmetic.

**Not yet documented pre-audit:** no SSOT feature note, no codemap section, no SHIPPED_FEATURES entry existed for this prior to the 2026-07-02 doc-drift pass. See `docs/SSOT/features/Knowledge Sources Reconciler.md`.
**Portal channel-narrowness sweep (v1.0.24–26):** widening `conversations.channel` for `instagram_login` exposed many hand-rolled narrow channel maps (NOT generated from shared-types, so `next build`/`tsc` passed but they broke at runtime). Fixed: backend `tenant_channels.list_channels` `per_kind` KeyError **500** (seed from `get_args(ChannelKind)` — this 500 cascaded to Channels/Templates/Broadcasts/Audience); portal ConversationsTable crash + dashboard/detail labels; Channels page email tile + IG-Login folded into Instagram tile; conversations filter (`ALLOWED_CHANNELS` was missing telegram+instagram_login); audience label-map + select; admin channel type. Defensive `?? fallback` added on channel→label/icon lookups. **Lesson: a new channel touches ~10+ hand-rolled collections across api+portal+admin; run BOTH portal+admin `pnpm typecheck` + grep all three apps.**
**Auto-capture gotcha:** channel-add token capture needs `CHATWOOT_INTERNAL_TOKEN` (polaris/.env) == `POLARIS_INTERNAL_TOKEN` (CW env) + `CHATWOOT_INTERNAL_BASE_URL=http://chatwoot-rails:3000`, else `get_channel_token … not configured — skipping` and `token_captured=false`.


**What:** The `/agent` portal screen's persona section is rebuilt as a **block editor** instead of a single raw-XML textarea. The persona is now a list of named "persona blocks" (human display_name → auto-slugified snake_case slug → body text), rendered 2-up with a recommended-block palette dropdown. Guardrails are shown as read-only cards (was raw XML). The separate "Instructions" textarea is removed from the UI (backend `agent_config.instructions` retained for back-compat).

**No DB migration.** `persona_blocks` lives in `tenants.settings` JSONB alongside `agent_config` and `booking_config`.

**Key design decisions:**
- Slug = `slugify_block_name(display_name)` (snake_case, max 40 chars). Slugs matching `RESERVED_BLOCK_SLUGS` (platform XML tag names) are rejected server-side.
- Max 10 blocks; duplicate slugs rejected. Shared-types SSOT owns the contract.
- `render_persona_blocks()` neutralizes `</` in block bodies (defense-in-depth, mirrors `format_rag_context`).
- `DEFAULT_PERSONA_BLOCKS` is byte-identical to `_V1_TEMPLATE` — guarded by a regression test so the structured form never silently diverges from the legacy default.
- Agent default-on for **new tenants only**: `admin_tenants.py` tenant-create now seeds `agent_config = {enabled:true, enabled_tools:[search_knowledge, handoff_to_human], …}`. Existing tenants NOT backfilled. Reverses ADR-063 "agent off by default" for new tenants (ADR append pending — no ADR number fabricated).
- PATCH JSONB-merge refactor: all settings-JSONB keys now merged in a **single** `settings = settings || jsonb_build_object(...)` clause (Postgres rejects assigning a column twice in one UPDATE).

**New / changed files:**
- `packages/shared-types/python/polaris_shared_types/agent_config.py` — `PersonaBlock`, `RESERVED_BLOCK_SLUGS`, `slugify_block_name()`, `RECOMMENDED_PERSONA_BLOCKS`, `MAX_PERSONA_BLOCKS=10`
- `apps/api/polaris_api/services/_llm_prompts.py` — `DEFAULT_PERSONA_BLOCKS`, `default_persona_blocks()`, `render_persona_blocks()`, `coerce_persona_blocks()`; `TenantPersona.persona_blocks`; `_base_persona` precedence updated
- `apps/api/polaris_api/routers/tenant_settings.py` — GET returns `persona_blocks` + `default_persona_blocks`; PATCH validates + single-clause JSONB merge
- `apps/api/polaris_api/services/tenant_snapshot.py` — `load_tenant_snapshot` surfaces `persona_blocks` (moved from `flow_runtime/repo.py` — audit Batch D, 2026-07-02)
- 4 reply-path sites (`pipeline.py`, `agent/pipeline_integration.py`, `email_assist.py`, `followups/worker.py`) — pass `persona_blocks` to `TenantPersona`
- `apps/api/polaris_api/routers/admin_tenants.py` — tenant-create seeds `agent_config` enabled=True
- `apps/portal/components/agent/AgentForm.tsx` — rewritten (block grid + palette dropdown + guardrail cards)
- `apps/portal/components/settings/SettingsForm.tsx` — `PersonaBlockValue`, `personaBlocksSchema`, `slugifyBlockName`, `RESERVED_BLOCK_SLUGS`
- `apps/portal/lib/settings/load.ts` — carries `persona_blocks` + `default_persona_blocks`
- `apps/api/tests/test_persona_blocks.py` — new test file
- `apps/api/tests/test_e2e_cross_tenant_isolation.py` — `loop_scope="function"` fix bundled

**Release:** commit `2992dd3` (feature merge `5d31f70`); images `api/portal/admin` = `v1.0.55` on GHCR; pinned in `infra/docker-compose.prod.yml`. EA/CW forks unchanged.

---

## Demo System v3 ✅ (DEPLOYED v1.0.45, 2026-06-16 — ADR-066, migration 114)

**What:** Text-gated single-account roleplay demo. Customers message the operator's **live channels** (the operator's dogfood tenant). Default (no code) = operator's own bot. Typing a configured **demo code** in any message → bot roleplays as a hidden `is_demo=true` brand tenant (own KB/persona/products via RLS) for a capped session. On cap/time expiry → revert to operator bot + post a Chatwoot private-note lead flag. One-demo-ever per contact (90-day reset via admin).

**Key design decisions:**
- Gateway = channel-owning tenant (`incoming.tenant_id`), NOT a hardcoded ID. Dispatch creds resolved via `_resolve_channel_owner()` so brand-scoped turns still send via the gateway channel.
- Demo tenant `is_demo=true` flag causes MRR, admin tenant list, and admin health to exclude them automatically.
- `META_DEMO_*` config, the meta_whatsapp demo-WABA client, and `reply_dispatch.is_demo` branch are all REMOVED.

**New files:**
- `packages/db-migrations/114_demo_tenant_model.sql` — schema changes (see data.md)
- `apps/api/polaris_api/services/demo_sessions.py` — session primitives (full rework)
- `apps/api/polaris_api/services/demo_runtime.py` — read-only state machine
- `apps/api/polaris_api/services/demo_lead.py` — lead flag + CW private note
- `apps/api/polaris_api/routers/admin_demos.py` — `/api/admin/demos` CRUD (registered in main.py)
- `apps/admin/app/(admin)/demo-system/*` + `apps/admin/components/demos/*` — admin UI

**Pipeline integration:** `pipeline.run_inbound()` (new demo-aware entry called by inbound_worker) invokes `demo_runtime.resolve()` → swaps `tenant_id` to the brand tenant when demo is active; skips audience+quota for demo turns; delegates to `_run_inner` as normal.

---

## Chatwoot-Native Inbound Rehaul ✅ (DEPLOYED v1.0.31, 2026-06-13 — PR #102 `d500177` + fixups `dc8cd2c`→`73e7f78`)

**What:** Inbound is re-architected from `Meta → meta_router (Polaris)` to **`Meta → Chatwoot → agent-bot webhook → Polaris`** — **now LIVE on prod v1.0.31 (2026-06-13)**. Chatwoot owns the channel inboxes + conversations; its agent-bot fires `message_created` on every inbound → `POST /v1/webhooks/chatwoot`. Polaris gates on `conversation.status=='pending'` (bot-owned), resolves the real channel + recipient (`conversation.contact_inbox.source_id`), runs the pipeline, replies **direct to Meta/Telegram** (per-tenant token), then mirror-posts to the **linked** CW conversation with `source_id`.

**Deleted:** `webhooks/meta_router.py` (534 lines), `webhooks/_meta_extract.py` (349), `webhooks/_demo_branch.py`. There is no longer a direct Meta→Polaris customer-message path.

**New behaviours:**
- **Human-handling gate** — bot runs only when `status=='pending'`; open/resolved/snoozed = a human owns it → record `human_handling`, stay silent (`is_bot_owned`).
- **Echo-drop invariant** — the bot's own mirror-posts return as `outgoing` + `sender.type=='agent_bot'` and are dropped; a human agent's reply (`sender.type=='agent'`) is NO LONGER forwarded by Polaris — Chatwoot delivers it to the customer NATIVELY. On Chatwoot-native EMAIL inboxes only, the agent reply triggers email-draft acceptance tracking (`_record_email_agent_reply` → `email_assist.record_agent_reply`); non-email inboxes are a no-op. **NEW (v1.0.31, PR #108):** agent replies are ALSO persisted to `messages` table for portal transcript.
- **Mirror = LINK, not create** — `pipeline._link_cw_conversation` reuses the existing CW conversation from the agent-bot payload ids (idempotent UPDATE), fixing a duplicate-conversation bug.
- **Two-label sync** — bot-handling / human-handling conversation labels (`services/chatwoot/label_sync.py`); removing `human-handling` resumes the bot (reverse-handoff).
- **Agent-bot auto-attach** — `services/chatwoot/agent_bot.py` attaches the shared "Polaris Bot" to a new inbox on provision so conversations start `pending`.
- **Deferred CW side-effects** — `pipeline.run` is a thin wrapper over `_run_inner`; conn-independent CW work (labels, voice-note note) flushed in a `finally` after the txn commits.

**Fork-relay:** non-message WA webhooks (delivery/read receipts, template-status, account/MM-Lite) reach Polaris via a `prepend_mod_with` module on `Webhooks::WhatsappEventsJob` (polaris-cw-fork) → the existing routers (unchanged).

**Migrations 108–112:** 108 widens `webhook_events.status`; 109 drops legacy `instagram`; **110** grants `messages.metadata` UPDATE to anon; **111** repairs `audience_members.metadata` JSONB; **112** repairs `tenants.settings` JSONB. 001–112 applied + LIVE on prod (v1.0.31, 2026-06-13).

**Critical Chatwoot config:** `SAFE_FETCH_ALLOW_PRIVATE_NETWORK=true` required on chatwoot-rails + chatwoot-sidekiq (v4.14.2+) for agent-bot webhook to reach internal polaris-api:8000 (PR #103).

**Key files:** `webhooks/chatwoot_router.py`, `services/pipeline.py`, `services/chatwoot/{agent_bot,label_sync,recovery,agent_reply}.py`, migrations `108_*`–`112_*`.

---

## AI Agent Portal Screen ✅ (DEPLOYED v1.0.31, 2026-06-13)

**What:** A new `/agent` portal screen splits the Bot persona + tool-calling agent (enable / instructions / tool toggles) + read-only guardrails OUT of Settings. Persona box prefilled with the **effective** prompt + a "Using default / Customized" badge + Reset; "beta" wording dropped; the **Authentication** template category removed from the compose form. Nav placed 2nd (after Conversations) → sidebar **16 entries**.
**Backend:** `GET /api/tenant/settings` += read-only `default_system_prompt` + `agent_guardrails`.
**Key files:** `apps/portal/app/(portal)/agent/`, `components/agent/`, `routers/tenant_settings.py`. No migration.

---

## Human-Agent Reply Persistence ✅ (DEPLOYED v1.0.31, PR #108, 2026-06-13)

**What:** Chatwoot human-agent replies are now persisted into the Polaris `messages` table for the portal Conversations transcript (bot-provider channels: whatsapp/messenger/telegram/instagram_login). When Chatwoot webhook fires `message_created` with `sender.type='agent'` (a human agent reply), the `chatwoot_router` calls `_handle_human_agent_reply()`.

**Implementation:** NEW service `apps/api/polaris_api/services/chatwoot/agent_reply.py`
- `build_reply_row(payload)` — pure, constructs the message row from Chatwoot payload (role='assistant', metadata.author_type='human_agent', author name/id from payload)
- `persist_human_agent_reply(...)` — anon pool under RLS; idempotent via `provider_message_id='chatwoot.<id>'` + `ON CONFLICT (tenant_id, provider_message_id) DO NOTHING`
- Bumps `conversations.last_message_at` to reflect new reply in transcript
- Best-effort Redis cache invalidation (`invalidate_cache()` helper in `services/conversation.py`)

**Schema updates:** Shared-types `MessageDetail` (packages/shared-types/python/polaris_shared_types/conversation_view.py + schema.json) += `author_type` / `author_name`; reader endpoint `routers/tenant_conversations.py` exposes them; portal `components/conversations/ConversationTranscript.tsx` renders human-agent replies distinctly.

**Database:** Migration 031 already granted `INSERT` to polaris_anon on `messages`; migration 110 added `UPDATE (metadata)` grant (PR #106, needed for dispatch failure recording).

**Tests:** `tests/chatwoot/test_agent_reply.py` + `tests/test_chatwoot_agent_reply_persist.py`. Idempotent pattern verified (multiple webhook fires of the same reply are safely deduplicated).

**Email inboxes:** The router's `_record_email_agent_reply` now unified under `_handle_human_agent_reply` (bot-provider inboxes → persist, email inbox → draft acceptance scoring, same as v1.0.27).

**Key files:** `services/chatwoot/agent_reply.py`, `webhooks/chatwoot_router.py`, `services/conversation.py`, shared-types `conversation_view.py` + `schema.json`, portal `ConversationTranscript.tsx`, migrations `031`/`110`.

---

## Post-Cutover Stability PRs (#103–#110) ✅ (DEPLOYED v1.0.31, 2026-06-13)

Batch of 8 fixups merged to main (PRs #103–#110) immediately after cutover; DEPLOYED v1.0.31 to prod (2026-06-13).

**PR #103 — SAFE_FETCH_ALLOW_PRIVATE_NETWORK flag**
- **Issue:** Chatwoot v4.14.2's `ssrf_filter` blocks private-IP webhook targets. Agent-bot webhook to internal polaris-api:8000 silently failed → no reply, no label, no portal sync.
- **Fix:** Set `SAFE_FETCH_ALLOW_PRIVATE_NETWORK=true` on chatwoot-rails + chatwoot-sidekiq containers (infra/docker-compose.prod.yml).
- **Key files:** `infra/docker-compose.prod.yml`, `../docs/internal-ops/runbooks/chatwoot-native-cutover.md` (documented).

**PR #104 — Quota flush orphan-skip**
- **Issue:** nightly `quota_flush.py` job failed on deleted-tenant counters (orphaned Redis keys) → aggregated ERROR on every run, FK violations on `tenants` table.
- **Fix:** `services/scheduled/quota_flush.py` extracts module-level `_flush_counters`; pre-filters Redis counter tenant_ids against live `tenants` and skips orphans with aggregated WARNING. New `FlushResult` NamedTuple with `.skipped` field. Tests added (`tests/scheduled/test_quota_flush.py`).
- **Key files:** `services/scheduled/quota_flush.py`, `tests/scheduled/test_quota_flush.py`.

**PR #105 — Mimir cost query 404 fix**
- **Issue:** Cost dashboard 404 on every query; Mimir endpoint was `.../push` (remote-write) instead of `.../api/prom` (query).
- **Fix:** `services/grafana_cost.py` new `_normalize_mimir_base()` strips trailing `/push` so cost query hits `{host}/api/prom/api/v1/query`. Non-2xx now logs self-diagnosing 404-vs-401 hint. **ENV docs updated:** `GRAFANA_CLOUD_MIMIR_URL` must be query base `.../api/prom`; `GRAFANA_CLOUD_MIMIR_TOKEN` must be base64(instanceID:token) with metrics:read scope.
- **Key files:** `services/grafana_cost.py`, `docs/ENV.md`.

**PR #106 — Messages.metadata UPDATE grant**
- **Issue:** Dispatch-failure recorder calling `messages.metadata` UPDATE failed with `InsufficientPrivilegeError` (polaris_anon had no UPDATE grant on the column).
- **Fix:** Migration 110 adds `GRANT UPDATE (metadata) ON messages TO polaris_anon` (column-scoped, mirrors migration 031's provider_message_id grant).
- **Key files:** migration `110_grant_messages_metadata_update.sql`.

**PR #107 — Meta structured error logging**
- **Issue:** Non-2xx Meta send failures logged empty or truncated error body; couldn't diagnose e.g. expired Page token (code=190).
- **Fix:** `services/meta_messaging.py` now logs STRUCTURED error fields (code, error_subcode, type, fbtrace_id, message, all bounded) on non-2xx. Body + error_data NEVER logged (token-fragment safety). Exception message carries code+subcode for quick scanning.
- **Key files:** `services/meta_messaging.py`.

---

## Systemic JSONB Double-Encode Fixes (#109–#110) ✅ (DEPLOYED v1.0.31, PRs #109–#110, migrations 111–112, 2026-06-13)

**Root cause:** The anon AND service asyncpg pools both register a JSONB codec (encoder=`json.dumps`) in `db/pools.py::_init_conn`. Calling `json.dumps(x)` before passing to a `$N::jsonb` param double-encodes — the codec applies another `json.dumps`, wrapping the string in a JSONB string literal. This breaks `col->>'key'` filters and corrupts stored data.

**Canonical pattern (correct):** Pass Python dict/list directly to `::jsonb` param. Example: `services/audit.py:151-153` (audit row INSERT with metadata JSONB).

**PR #109 — Audience members fix**
- **Issue:** `services/audience/importer.py` called `json.dumps(metadata)` then passed to `::jsonb` param → double-encoded rows.
- **Fix:** Pass metadata dict DIRECTLY to param (no json.dumps). Migration 111 repairs corrupted rows using idempotent `jsonb_encode_repair` helper.
- **Key files:** `services/audience/importer.py`, migration `111_repair_audience_members_metadata_jsonb_double_encode.sql`.

**PR #110 — Systemic fix across 14 writer files**
- **Issue:** Audit sweep found 14 writer sites passing double-encoded JSONB across dispatch, audience, tenant settings, etc.
- **Fix:** Audit sweep corrects all 14 call sites in: `services/{audit, audience/members, audience/importer, conversation, dispatch, pipeline, chatoot/agent_reply, billing/invoice, scheduling, ...}.py`. Migration 112 repairs the one live `tenants.settings` corrupted row (same idempotent pattern).
- **Readers defensively guarded:** Historic code has `if isinstance(raw, str): json.loads(...)` fallback — intentionally left in place to tolerate older data, now redundant post-fix.
- **Key files:** 14 service modules (see PR #110 full list), migration `112_repair_tenants_settings_jsonb_double_encode.sql`.

**Gotcha for new code:** ALWAYS pass dict/list directly to `::jsonb` params. The codec is transparent; `json.dumps` + codec = double-encode (the string "{\\"key\\": ...}").

---

## Handoff & Outbound Dispatch Hardening ✅ (SHIPPED, v1.0.28, HEAD=9e41bf1, 2026-06-11)

**What:** Two commits merged after v1.0.27 close the last silent-failure gaps in the dispatch surface.

**6ebeb7f — handoff fixes:**
- Agent attachments (images/files) forwarded from Chatwoot agent replies (previously dropped)
- Guard on reverse-handoff resume: `pipeline.run` now explicitly rejects `provider='chatwoot'` to prevent bot re-entering a handoff conversation
- `email_assist_worker` SAVEPOINT-recover: drops instead of poisoning the outer transaction on assist failures

**9e41bf1 — outbound dispatch hardening:**
- Silent send failures now surface as logged errors (swallowed exceptions in `reply_dispatch` replaced with explicit raises + error recording)
- WABA token pinned to conversation's `channel_id` (prevents wrong-WABA multi-channel misroute)

**Key files:** `services/reply_dispatch.py`, `services/pipeline.py`, `webhooks/chatwoot_router.py`, `services/email_assist_worker.py`. **No migration.**

---

## Cost Metrics Dashboard ✅ (SHIPPED, v1.0.28, PR #98, d883fb2, 2026-06-11)

**What:** Fixes the empty admin cost dashboard by wiring a full OTel meter pipeline.
**Key pieces:**
- `apps/api/polaris_api/metrics.py` — OTel meter definitions (counters for LLM tokens by provider + model, Cohere rerank calls, embedding calls, classify calls)
- `services/grafana_cost.py` — updated Mimir proxy + cost stub; per-tenant USD prices with one FX knob (`USD_TO_MYR_RATE`, default `4.15`). Qwen embedding priced at `$0.01/M tokens USD`. Prompt-cache priced separately.
- Call sites instrumented: `llm.generate_response`, `llm.classify_intent`, rerank, embed
- Admin cost dashboard now has live data once OTel/Mimir is configured on VPS
- ⚠️ CORRECTION (verified vs merged code @7d1b531): the per-tier aggregation shape was NOT changed — `grafana_cost.py` still returns `avg_*` token fields + `sample_count` (hardcoded 0), and Supabase per-tenant cost (`supabase_storage_cost_myr`) is still included. PR #98's real scope = OTel emission + USD pricing/FX + embedding-priced + cache-read populated (the earlier "avg→total / sample_count removed / Supabase dropped" claim never landed)

**Config:** `USD_TO_MYR_RATE` env var (default `4.15`); existing `GRAFANA_CLOUD_MIMIR_URL` + `GRAFANA_CLOUD_MIMIR_TOKEN`. No new migration.
**Note:** Dashboard shows live data only after VPS OTel/Mimir config applied.

---

## HUMAN_AGENT Meta Tag ⚠️ (PR #100, bf07fa6, 2026-06-10 — REMOVED-from-Polaris by phase-10 cutover)

> ⚠️ **SUPERSEDED by the Chatwoot-native cutover (phase-10):** the HUMAN_AGENT tag was applied INSIDE `_dispatch_agent_forward`, which is now DELETED. **Polaris no longer forwards human-agent replies or applies the tag** — human-agent delivery (and any tagging) is now **Chatwoot-native**. The `human_agent` kwarg still exists in `meta_messaging.send_text/send_image` but is no longer called from `chatwoot_router`. The detail below is historical.

**What (historical):** Tagged human-agent outbound messages on Messenger + Instagram-Login with the `HUMAN_AGENT` Meta message tag (required for App Review 7-day window).
**Details (historical):**
- `_dispatch_agent_forward` (Messenger path): sent `messaging_type: MESSAGE_TAG` + `tag: HUMAN_AGENT`
- `_dispatch_agent_forward` (Instagram-Login path): sent bare `tag: HUMAN_AGENT` (no `messaging_type`)
- Always-tag, unconditional; bot send path (`reply_dispatch`) untouched; WhatsApp out-of-scope (no equivalent tag)
**Key file (now deleted):** `webhooks/chatwoot_router.py::_dispatch_agent_forward`. No migration.

---

## Agent Tool-Calling Surface ✅ (SHIPPED, v1.0.28, PR #99, b5bbec7, ADR-063, 2026-06-11; tier routing D4 + flow_runtime backend fully retired 2026-07-02, audit Batch D)

**What:** Tool-calling agent replaces the flow-builder as the primary intelligence layer. The flow-builder portal UI was removed at ADR-063 ship time; the `flow_runtime/` backend archive was kept "for now" at that point — audit Batch D (2026-07-02) completed the retirement by deleting the backend engine outright. Its `Ports`/adapters (the pieces the agent actually depended on) now live in `services/agent/ports.py`, not `flow_runtime/`.
**Services (`services/agent/`):**
- `config.py` — `parse_agent_config`, `AgentConfig` model, `tools_for(enabled, tier)` registry filter
- `loop.py` — `run_agent_reply`: Anthropic tool-use loop (model emits `tool_use` → execute → feed `tool_result` → repeat until text or `max_tool_iterations` hard cap; final no-tools call on cap). **D4 (2026-07-02):** both calls now pass `model_tier` (ADR-031 Haiku/Sonnet routing) instead of always defaulting to Haiku.
- `pipeline_integration.py` — `try_run_agent`: called after STOP/quota/booking-cmd, before classic; returns `None` when `agent_config.enabled=false`. **D4:** also classifies the inbound message via `llm.classify_intent` and threads the resulting tier through to `run_agent_reply`.
- `result.py` — `AgentReplyResult` envelope
- `tools/` — `@register_tool` registry with 4 tools: `search_knowledge` (RAGAdapter), `lookup_products` (ProductsAdapter), `handoff_to_human` (ChatwootAdapter; replaces `[HANDOFF_TRIGGER]`), `capture_lead` (audience upsert)
- `ports.py` (audit Batch D, 2026-07-02) — `Ports` container + `LLMPort`/`RAGPort`/`ProductsPort`/`ChatwootPort`/`ConversationPort` + adapters + `build_ports()`, relocated verbatim from `flow_runtime/ports.py` + `adapters.py` + `pipeline_integration.py`. Flow-only `MetaPort`/`DBPort`/`AuditPort` were dropped, not moved.

**Prompt rewrite (`_llm_prompts.py`):**
- `build_response_system_prompt` (chat path)
- `build_email_draft_prompt` (email path, separate)
- `build_agent_system_prompt` (agent path)
- Non-overridable `_SAFETY_GUARDS` block; `[HANDOFF_TRIGGER]` deleted

**Portal:** `AgentSection` in `SettingsForm.tsx` (enable toggle + instructions textarea + tool-toggle checkboxes); flow-builder UI removed (flows/ dir, flow-editor/, lib/flow-editor/, 3 flow e2e specs, "Flow Builder" nav entry deleted → sidebar back to **15 entries**).
**Migration 105:** `polaris-internal persona seed` — `UPDATE tenants SET system_prompt` on the demo/marketing tenant (idempotent; applied).
**Key files:** `services/agent/`, `services/_llm_prompts.py`, `services/pipeline.py`, `services/tenant_snapshot.py`, `webhooks/chatwoot_router.py`, `apps/portal/app/(portal)/settings/`.

---

## Booking notifications + appointment templates ✅ (SHIPPED, v1.0.27, PR #97, 2026-06-10)

**What:** The pre-appointment **reminder** ("1 day prior") and the immediate **"you're booked" confirmation** now actually fire in prod. Both send machineries existed but neither was triggered.
**Root cause:** `schedule_reminder` was only ever called from the internal-E2E endpoint → the reminder worker scanned an empty table in prod; `confirmation_message` was collected by the portal but read by no send-path (orphaned).
**Fix:** `link_flow.confirm_booking` (the single choke point — covers initial bookings AND conversational reschedules, which re-book → confirm) now best-effort `_schedule_reminder_for_confirmed` + `_send_confirmation_for_confirmed`. Confirmation prefers `confirmation_template_name` (delivers anytime), else free-form `confirmation_message` (24h window). New shared `services/booking/_notify.py` (template-or-text sender + **tenant-timezone** formatting — fixes prior hardcoded-UTC in the reminder worker) + `services/booking/config.py` (`read_booking_config`). `meta_whatsapp.create_from_library()` for the instant-approved Meta Template Library (backend primitive). `BookingConfig` gains `tenant_timezone` + `{confirmation,reminder,cancellation,reschedule}_template_name`/`_language`.
**Deferred (verified):** cancel/reschedule notices (self-serve already replies inline → would double-message; needs an EA cancel webhook); portal preset-library gallery + canonical `library_template_name` strings (live WhatsApp Manager only).
**Key files:** `services/booking/link_flow.py`, `services/booking/_notify.py`, `services/booking/config.py`, `services/booking_reminders.py`, `services/meta_whatsapp.py`, `routers/tenant_bookings.py`. **No migration** (config is JSONB in `tenants.settings.booking_config`). 600 tests pass; ECC-reviewed.
**Best-effort contract:** a reminder/confirmation failure never fails the EA confirm webhook (else EA retries an already-confirmed booking). Known follow-up: `confirm_booking` holds a DB conn across the Meta send (fine at confirm volume).

## Instagram-Login channel ✅ (SHIPPED + LIVE, v1.0.23, PR #91, migration 104, 2026-06-10)

> ⚠️ **Inbound path SUPERSEDED by the Chatwoot-native rehaul (PR #102):** `meta_router` is deleted; inbound IG-Login now flows Meta → Chatwoot → agent-bot → `chatwoot_router` → pipeline. The legacy FB-page `instagram` channel is fully DROPPED (migration 109) — only `instagram_login` remains. The send-side (`InstagramLoginClient`, graph.instagram.com) and the two-secret HMAC are unchanged. The detail below is historical context for the channel itself.

**What:** First-class `instagram_login` channel for the **Instagram API with Instagram Login** (graph.instagram.com) — Chatwoot's "Continue with Instagram" path — distinct from the legacy FB-page `instagram` (graph.facebook.com), now removed. Bot replies via the IG-Login user token; mirrors to Chatwoot.
**Why it was broken:** meta_router only verified with `META_APP_SECRET` + sent IG via graph.facebook.com (page token). IG-Login signs webhooks with a **separate** Instagram App Secret and sends via graph.instagram.com → DMs never verified / replies hit the wrong endpoint.
**Key pieces:** two-secret HMAC `verify_meta_signature_multi` (`META_APP_SECRET` + `META_INSTAGRAM_APP_SECRET`); `resolve_meta` returns the row's own `channel` so `instagram` vs `instagram_login` (shared `ig_business_account_id`) is distinguishable; `InstagramLoginClient` (graph.instagram.com, `{recipient,message}`, no `messaging_type`, token as `?access_token=`); `reply_dispatch._send_instagram_login` + pipeline `_resolve_ig_login_token` (`meta_ig_login_access_token`); Chatwoot mirror + agent-forward + media + broadcast + bookings parity; admin "Instagram (Login)" option; `Channel::Instagram`→`instagram_login` auto-detect.
**Key files:** `webhooks/meta_router.py`, `webhooks/_hmac.py`, `services/webhook_resolver.py`, `services/meta_messaging.py`, `services/reply_dispatch.py`, `services/pipeline.py`, `services/channels/provision.py`, `routers/admin_tenants.py`, migration `104_instagram_login_channel.sql`.
**Deploy footgun (the live "no messages" bug):** the Meta app had **no `instagram` webhook object subscription** (only WA + page) → zero IG deliveries. Fixed via `POST graph.facebook.com/{META_APP_ID}/subscriptions object=instagram` and now auto-registered by `infra/deploy/register-meta-webhooks.sh` (set `META_APP_ID=1434258047621253` in `.env`). The IG-Login app id `1340991594496089` + `META_INSTAGRAM_APP_SECRET` are NOT valid on graph.facebook.com (code 190).

## WhatsApp meta_router + per-tenant send ✅ (SHIPPED, v1.0.21 + CW fork r4, 2026-06-10)

> ⚠️ **Inbound path SUPERSEDED by the Chatwoot-native rehaul (PR #102):** `meta_router` + the WABA `override_callback_uri` to Polaris are gone. Chatwoot now owns the WABA webhook; inbound WhatsApp flows Meta → Chatwoot → agent-bot → `chatwoot_router` → pipeline, and non-message WA events arrive via the fork-relay. **The per-tenant WABA send fix (v1.0.21) still stands** — replies go from the tenant's own WABA token. The `META_DEMO_*` meta_router footgun no longer applies (no demo branch). The detail below is historical.

**What (historical):** WhatsApp worked end-to-end via the canonical path. Inbound: Meta WABA `override_callback_uri` → Polaris `meta_router` (resolves tenant by `phone_number_id`) → pipeline → mirror to Chatwoot. Reply: sent from the **tenant's own** WABA number/token.
**Why it was broken:** (a) `reply_dispatch._send_whatsapp` was hardcoded to the demo number/token (per-tenant routing was a GA gap); (b) Chatwoot's `Whatsapp::WebhookSetupService` kept reclaiming the WABA webhook for itself, so inbound never reached Polaris.
**Fix:** v1.0.21 — `pipeline._resolve_whatsapp_credentials` (tenant_channels.phone_number_id + tenant_secrets.meta_waba_access_token) threaded to `MetaWhatsAppClient.send_text(access_token=)`, demo fallback. Fork r4 — `WebhookSetupService` registers `POLARIS_WABA_WEBHOOK_OVERRIDE_URL` (meta_router) + `POLARIS_META_WEBHOOK_VERIFY_TOKEN` durably.
**Key files:** `services/reply_dispatch.py`, `services/pipeline.py`, fork `app/services/whatsapp/webhook_setup_service.rb`.
**Footgun:** keep `META_DEMO_*` EMPTY in prod (a real number there → meta_router demo-branch hijack + FK-crash → blackhole).

## Admin Remove-Channel ✅ (SHIPPED, v1.0.22, 2026-06-10)

**What:** `DELETE /api/admin/tenants/{id}/channels/{kind}` + a per-channel "Remove" button in the admin Channels tab — reconfigure one channel without hard-deleting the whole tenant.
**How:** `services/channels/provision.deprovision_channel()` deletes the `tenant_channels` row + its captured token secret (sibling-safe: keeps `meta_fb_page_access_token` if IG/Messenger sibling remains); audit `channel.removed`; impersonation-guarded; does NOT touch the Chatwoot inbox or Meta webhook.
**Key files:** `routers/admin_tenants.py`, `services/channels/provision.py`, `apps/admin/components/tenants/RemoveChannelButton.tsx`.

## Chatwoot Mirror Double-Send Fix ✅ (SHIPPED, v1.0.20, PR #90, 2026-06-09)

**What:** Prevent customers from receiving bot replies twice. When Polaris mirrors a bot reply into Chatwoot (for agent view), Chatwoot now sees the message already came from the channel and skips re-sending it.

**Architecture:**
- **Problem:** Polaris sends bot reply → saves it locally → mirrors to Chatwoot for agent view. Chatwoot's `send_on_channel_service` then re-sent the message on the channel (intended for non-Polaris message sources). Customers saw the reply twice + Meta `message_echoes` flooded the logs.
- **Solution:** `services/chatwoot/client.py::post_outbound_message()` now accepts optional `source_id` param (the provider message-id). When set in the request body, Chatwoot's service recognizes the message originated from the channel and skips re-delivery. `source_id` = the WAMID (WhatsApp), mid (Messenger), msg_id (Telegram), or similar.
- **Wiring:** All 3 reply-mirror call sites in pipeline pass `source_id=provider_message_id`:
  - Main LLM reply (step 12a)
  - Flow runtime replies (LLMReplyNode, HandoffNode, TextNode)
  - Booking command replies (CONFIRM/CANCEL/RESCHEDULE)

**Config:** No new env vars. Feature auto-enabled if Chatwoot fork v4.14.0-r3+ is deployed.

**Effect:** Customers receive bot replies exactly once; Meta message_echo counts drop (less noise); Chatwoot inbox reflects accurate message history.

**Tests:** Transparent to existing test suite (no new tests; legacy tests verified no regression).

**Status:** ✅ SHIPPED v1.0.20 (2026-06-09); affects all channels (WA/IG/Messenger/Telegram)

---

## Observability: Grafana Cloud + OpenTelemetry ✅ (SHIPPED, v1.0.15, 2026-06-09)

**What:** Full-stack observability wired to Grafana Cloud; distributed tracing (Tempo), metrics (Mimir), error tracking (Sentry).

**Services:**
- **polaris-api:** OTLP exporter → Grafana Cloud Tempo (traces) + Mimir (metrics); Sentry for errors + perf monitoring
- **polaris-portal, polaris-admin:** @sentry/nextjs (frontend error tracking)

**Key Components:**
- `apps/api/polaris_api/observability.py` — OTel SDK init (already built pre-v1.0.15)
- `infra/docker-compose.prod.yml` — ENVIRONMENT, OTEL_*, SENTRY_* env vars; `OTEL_RESOURCE_ATTRIBUTES: service.namespace=polaris`
- `infra/grafana-dashboards/polaris-overview.json` — Dashboard imported to Grafana Cloud
- `apps/api/polaris_api/config.py` (line ~425) — `environment: Literal["local", "staging", "prod", "production"]` allows `ENVIRONMENT=production`

**Production Config (v1.0.15):**
- `OTEL_ENDPOINT` = https://otlp-gateway-prod-<region>.grafana.net/otlp (Grafana Cloud Tempo datasource)
- `OTEL_HEADERS` = Authorization header + Content-Type
- `GRAFANA_CLOUD_MIMIR_URL`, `GRAFANA_CLOUD_MIMIR_TOKEN` = Mimir cost API
- `SENTRY_DSN`, `SENTRY_PROJECT_URL` = error tracking
- `GRAFANA_DASHBOARD_URL` = link in alerts

**Graceful Degradation:**
- Empty DSN → Sentry no-op
- Empty OTEL_ENDPOINT → OTel no-op
- Empty Mimir endpoint → cost_ceiling_check skipped (info logged)

**Tests:**
- Backend: No new tests (instrumentation is transparent; existing tests unaffected by OTel/Sentry initialization)

**Status:** ✅ SHIPPED v1.0.15 (2026-06-09); traces flowing to Grafana Cloud Tempo
**Next steps:** M2 = semantic dashboards, custom alerts (deferred); live E2E tracing validation

**Documentation:**
- See **[dependencies.md](dependencies.md)** (env vars + external services)
- See **[architecture.md](architecture.md)** (observability diagram + prod gotchas)

---

## Admin UX: Token Preview + Labeled Secrets Rotation ✅ (SHIPPED, v1.0.20, PR #88, 2026-06-09)

**What:** Improved admin UI for channel provisioning and secret rotation.

**Changes:**
1. **AddChannelModal (v1.0.20):**
   - New `token_preview` field: Shows masked last-4 chars of existing credential (e.g., "…XyZ1" for WhatsApp WABA token)
   - Read-only display; helps admins verify they're updating the right channel
   - Telegram `chatwoot_inbox_id` field moved to top (prominence + logical flow)
   - Per-channel status note (e.g., "Auto-capture successful" / "Manual entry required")

2. **RotationForm on Secrets page (v1.0.20):**
   - Each rotate option now labeled with the actual credential being rotated:
     - "WhatsApp WABA Access Token"
     - "Facebook Page Access Token (Messenger + Instagram)"
     - "Telegram Bot Token"
     - "Chatwoot Agent API Token"
   - Prevents accidental rotation of wrong credential
   - Inline save feedback (success toast / error message)

**Backend:** `routers/admin_tenants.py::get_inbox_info()` returns `token_preview` field (masked, never full token). Returns None on any error (fail-soft).

**Security:** Tokens never logged; only last-4 displayed. Full token only revealed in the input field during initial setup.

**Status:** ✅ SHIPPED v1.0.20 (2026-06-09); improves UX for multi-channel tenants

---

## Channel Auto-Provision ✅ (SHIPPED, v1.0.19, PR #87, 2026-06-09)

**What:** Automatic token capture from Chatwoot fork during channel provisioning. Polaris can now auto-populate WhatsApp WABA tokens and Instagram/Messenger Page tokens without manual copy-paste.

**Architecture:**
- **New endpoint (Chatwoot fork v4.14.0-r3):** `GET /internal/inboxes/:id/token` — Returns the channel's outbound access token. Only reachable via docker-internal network; auth via `X-Polaris-Internal-Token` header (const-time compare). Returns 422 for unsupported channel types.
- **Polaris integration:** `services/chatwoot/client.py::get_channel_token(account_id, inbox_id) → str | None` — Best-effort method. Calls the fork's internal endpoint over `chatwoot_internal_base_url` (default `http://chatwoot-rails:3000`). Never logs token. Returns None on any failure (4xx, timeout, parse error, missing token).
- **Admin flow:** `routers/admin_tenants.py::add_channel` — After creating tenant_channels row, auto-calls `get_channel_token()` and stores result in `tenant_secrets` (whatsapp→`meta_waba_access_token`, messenger/instagram→`meta_fb_page_access_token`). Response includes `token_captured: bool` field.
- **UI feedback:** `AddChannelModal.tsx` displays per-channel status note + toast ("Token captured successfully" / "Token capture failed, please add manually"). No visual difference for disabled auto-provision (CHATWOOT_INTERNAL_TOKEN not set).

**Config Keys (NEW):**
- `CHATWOOT_INTERNAL_TOKEN` — Polaris-internal auth for fork `/internal/*` endpoints (const-time verification). Empty = feature disabled.
- `CHATWOOT_INTERNAL_BASE_URL` — Docker-internal Chatwoot Rails base URL (e.g. `http://chatwoot-rails:3000`). Empty = feature disabled.

**Security:**
- Internal endpoint blocked by `Caddyfile.prod` — only reachable via docker internal network.
- Token never logged by Polaris (logging only the success/failure and inbox_id).
- Shared secret `CHATWOOT_INTERNAL_TOKEN` (platform-wide env var, not per-tenant KMS).

**Status:** ✅ SHIPPED v1.0.19 (2026-06-09); CW fork v4.14.0-r3 deployed to VPS (2026-06-07)
**Commits:** PR #87 (Polaris-api + admin) + fork PR #2 (endpoint)

**Documentation:**
- See **[integrations.md](integrations.md)** §Chatwoot fork (new internal endpoint)
- See **[backend.md](backend.md)** §Channel auto-provision (diagram)
- See **[dependencies.md](dependencies.md)** §External services (new env vars)

---

## Channel Recovery ✅ (SHIPPED, v1.0.18, PR #86, 2026-06-09)

**What:** Complete multi-channel contact + agent-forward support. IG/Messenger now mirror to Chatwoot and support agent reply forwarding (same as WhatsApp). Telegram contact identifier support added.

**Inbound (v1.0.18):**
- **`ensure_contact()` multi-channel:** WhatsApp continues to use E.164 phone search; Instagram/Messenger/Telegram now use identifier path (`{channel}:{channel_user_id}`) via Chatwoot `POST /contacts/filter`. Prevents 422 errors on non-phone channels. Empty channel_user_id guard added.
- **`_ensure_cw_conversation()`:** Branched per channel (whatsapp→phone, instagram/messenger/telegram→identifier). Logs warning on unexpected channel + raises.
- **Mirror now covers Telegram:** Inbound Telegram messages now mirrored to Chatwoot (same mirror logic as WhatsApp/IG/Messenger).

**Outbound (v1.0.18 — ⚠️ `_dispatch_agent_forward` DELETED by phase-10 Chatwoot-native cutover; Polaris no longer forwards human-agent replies, Chatwoot delivers natively):**
- **`_dispatch_agent_forward()` extended (historical):** Agent replies from Chatwoot were forwarded to Instagram + Messenger (in addition to WhatsApp + Telegram).
  - **Instagram/Messenger:** Used `MetaMessagingClient` (same Page token as bot sends). Routed via `/me/messages`.
  - **Telegram:** Existing logic.
  - **WhatsApp:** Existing logic.
- **`AgentForwardContext` updated:** Added `page_token` field. NOTE: `AgentForwardContext` + `resolve_agent_forward_context()` still EXIST and are now consumed by `services/pipeline.py` (not `chatwoot_router`'s deleted forward path). `resolve_agent_forward_context()` resolves:
  - WhatsApp: `waba_token` (via phone_number_id + `meta_waba_access_token`)
  - IG/Messenger: `page_token` (via fb_page_id + `meta_fb_page_access_token`)
  - Telegram: `telegram_token` (via telegram_bot_id + `telegram_bot_token`)
  - All channels: `channel_user_id` (from conversations table)

**Files Changed:**
- `services/pipeline.py` — `_ensure_cw_conversation()` branched per channel
- `webhooks/chatwoot_router.py` — `_dispatch_agent_forward()` extended; IG/Messenger branch (⚠️ this function was later DELETED by the phase-10 Chatwoot-native cutover — CW now forwards human-agent replies natively)
- `services/webhook_resolver.py` — `AgentForwardContext` + per-channel token resolution
- `webhooks/_hmac.py` — `verify_chatwoot_signature()` now supports fork v4.14.0-r3 timestamp + prefix

**Config Changes (v1.0.18):**
- No new config keys (uses existing service tokens)
- Fork `verify_chatwoot_signature` behavior changed: now accepts `sha256=` prefix + timestamp replay window (±300s)

**Status:** ✅ SHIPPED v1.0.18 (2026-06-09); PR #86 merged
**Commits:** PR #86

**Documentation:**
- See **[architecture.md](architecture.md)** §Channel Architecture (inbound/outbound diagram)
- See **[services.md](services.md)** §Chatwoot Integration + webhook_resolver (multi-channel support)
- See **[integrations.md](integrations.md)** §Webhook signature verification (v1.0.18 changes)

---

## Admin: Channel Auto-fill + Telegram/Email Secrets ✅ (SHIPPED, 2026-06-08)

**What:** Three admin panel enhancements for channel + secret management.

**Features:**
1. **Chatwoot inbox auto-fill** — `AddChannelModal.tsx` (400ms debounced lookup of Chatwoot inboxes) → auto-populates wabaId, phoneNumberId, igBusinessId, fbPageId, telegram bot name. New endpoint `GET /api/admin/tenants/{id}/channels/inbox-info?chatwoot_inbox_id=N` + ChatwootClient.get_inbox().
2. **Telegram secret rotation** — `RotationForm.tsx` now includes `telegram_bot_token` service kind (NEW 2026-06-08). New entry in `admin_secrets.py::_SUPPORTED_SERVICES`.
3. **Email IMAP/SMTP secrets** — New `EmailConfigSection.tsx` component shows IMAP/SMTP config blocks per inbox + inline PasswordUpdatePanel (collapsible form, setTimeout cleanup). Endpoint `GET /api/admin/tenants/{id}/channels/email-config` lists email inboxes, `PATCH /api/admin/tenants/{id}/channels/email-credentials/{inbox_id}` rotates password. Cross-tenant guards on inbox_id. Audit: recent_audit reads `after` field.
4. **Tenant detail secrets link** — "Manage secrets →" button added to tenant detail page linking to `/secrets/{tenant_id}`.

**Files changed:**
- `apps/admin/components/tenants/AddChannelModal.tsx` — debounced Chatwoot lookup + auto-fill
- `apps/admin/components/secrets/RotationForm.tsx` — `telegram_bot_token` kind + UI
- `apps/admin/components/secrets/EmailConfigSection.tsx` — NEW component
- `apps/admin/app/(admin)/secrets/[tenant_id]/page.tsx` — parallel emailConfig fetch
- `apps/api/polaris_api/routers/admin_tenants.py` — 3 new endpoints + mapping dict
- `apps/api/polaris_api/services/chatwoot/client.py` — `get_inbox`, `list_inboxes`, `update_inbox_channel`

**Security/Compliance:**
- Cross-tenant inbox_id guard on email endpoints (preflight + WHERE clause)
- `_safe_port(raw, key)` helper for safe int port parsing
- Audit: payload includes `after` field with service name + password_last_rotated timestamp

**Commits:** 7b67fc8, b202f69, ace690e

---

## Admin: Telegram Channel + Quota Defaults Fix ✅ (SHIPPED, v1.0.3–v1.0.6)

**What:** Three bugs in Telegram channel provisioning via admin panel, all fixed and deployed.

**Fixes shipped:**
1. **v1.0.3** — `chatwoot_inbox_id` not sent in Telegram POST body → 422. Added field to form + payload.
2. **v1.0.4** — Override tier-cap checkbox hidden for Telegram → 403 (no way to bypass tier gate). Shown for all kinds now; override fields included in Telegram payload.
3. **v1.0.5–v1.0.6** — `_TIER_QUOTA_DEFAULTS` had `["whatsapp"]` for all tiers. Pro/Ultra/Enterprise now default to `["whatsapp","instagram","messenger","telegram","email"]`. Migration 099 backfills existing tenants.

**Files changed:**
- `apps/admin/components/tenants/AddChannelModal.tsx` — form fields + payload + canSubmit
- `apps/api/polaris_api/routers/admin_tenants.py` — `_TIER_QUOTA_DEFAULTS` per-tier defaults
- `packages/db-migrations/099_backfill_allowed_channel_kinds.sql` — backfill existing tenants

**Note:** Email in `allowed_channel_kinds` is cosmetic only — email_assist is Chatwoot-native and has no provision endpoint; it works for all tiers regardless of this field.

---

## Phase 15: Product Catalog ✅ (SHIPPED, main)

**Feature:** Tenant product registry with image upload, embedding search, and flow-builder integration.

**Entry Points:**
- `apps/api/polaris_api/routers/tenant_products.py` — CRUD + multipart image upload + signed URLs
- `apps/api/polaris_api/services/products/` — `storage.py` (Supabase bucket), `search.py` (embedding + rank)

**Linked Services:**
- `apps/api/polaris_api/services/meta_messaging.py` — IG/Messenger image send via Page token
- Product image attachment gating (`_attach_product_images`, formerly `flow_runtime/nodes/llm_reply.py`) was retired with the flow engine (audit Batch D, 2026-07-02) — flow-only code, no classic/agent-path equivalent

**Migrations:**
- **089** `tenant_products` — Product registry + blob references (Supabase bucket `polaris-products`)
- **090** `product_search_vectors` — pgvector embeddings; `tenant_quotas.product_images_total_mb_cap`

**Config Keys:**
- `product_image_max_size_mb` (10 default)
- `product_images_storage_enabled` (True default)
- `SUPABASE_STORAGE_URL`

**Tests:**
- `apps/api/tests/test_tenant_products.py` — 15+ tests (CRUD, image upload, search)

**Portal Integration:**
- `apps/portal/app/[tenantSlug]/(app)/products/` — Product management UI

**Documentation:**
- See **[services.md](services.md)** §"Products & Media"
- See **[backend.md](backend.md)** §Phase 15

---

## Phase 13: AI Follow-ups ✅ (SHIPPED, main)

**Feature:** Automated re-engagement follow-up when customer goes quiet (12–23h window, respect quiet hours).

**Entry Points:**
- `apps/api/polaris_api/services/followups/` — timing, eligibility, scheduler, worker
- `apps/api/polaris_api/routers/tenant_followups.py` — Config GET/POST

**Linked Services:**
- `apps/api/polaris_api/services/pipeline.py` — Calls `scheduler.maybe_schedule_followup()` after Phase-4 reply + `cancel_pending_followup()` on every inbound
- `apps/api/polaris_api/services/llm.py` — `generate_followup()` (Haiku)
- `apps/api/polaris_api/services/booking_reminders.py` — Similar pattern (nightly worker)

**Migrations:**
- **091** `scheduled_followups` — Pending follow-up records (singleton per conversation)
- **092** `scheduled_followups` indexes — Cross-tenant tick optimization

**Config Keys:**
- `followup_enabled` (default False, tenant-gated via settings)
- `followup_timing_window_start_hours`, `followup_timing_window_end_hours` (12–23 default)
- `followup_quiet_hours_start`, `followup_quiet_hours_end` (22:00–08:00 default, tenant-local zoneinfo)
- `followup_max_retries` (3 default)

**Scheduled Job:**
- `scheduled/followups/worker.py::tick_due()` — Nightly at-least-once worker (in-process lifespan task)

**Tests:**
- `apps/api/tests/services/test_followups.py` — 20+ tests (timing, eligibility, scheduler, worker)

**Portal Integration:**
- `apps/portal/app/[tenantSlug]/(app)/settings/` — Followup config (enable, quiet hours)

**Documentation:**
- See **[services.md](services.md)** §"Follow-ups & Engagement"
- See **[backend.md](backend.md)** §"AI follow-ups (re-engagement) — Phase 13"

---

## MRR Persistence + Scheduler Locks ✅ (SHIPPED, main, PR #80)

**Feature:** Persist MRR snapshots to DB; all 7 nightly jobs locked via Postgres advisory locks (ADR-062).

**Entry Points:**
- `apps/api/polaris_api/services/mrr_rollup.py` — Compute + persist MRR
- `apps/api/polaris_api/scheduled/_locking.py` — Advisory lock wrapper (NEW)

**Linked Services:**
- All 7 scheduled jobs wrapped in `run_with_advisory_lock(pool, job_name, lock_key, job_func)`
- `apps/api/polaris_api/routers/admin_financials.py` — `GET /api/admin/financials/mrr` (read-only live + persisted trend)

**Migration:**
- **093** `platform_mrr_snapshots` — Append-only MRR history (replaces in-memory deque)

**Scheduled Jobs (All locked):**
| Job | Lock Key | Time |
|-----|----------|------|
| `quota_flush` | `quota_flush` | 00:05 MYT |
| `retention_sweep` | `retention_sweep` | 02:00 MYT |
| `mrr_rollup` | `mrr_rollup` | 02:00 MYT |
| `cost_ceiling_check` | `cost_ceiling_check` | 03:00 MYT |
| `audit_chain_verify` | `audit_chain_verify` | 04:00 MYT |
| `invoice_overdue` | `invoice_overdue` | 09:00 MYT |
| `weekly_digest` | `weekly_digest` | Mon 09:00 MYT |

**Config Keys:**
- No new config; uses existing `mrr_rollup` logic

**ADR:** **ADR-062** (in docs/Polaris V2/09_DECISION_LOG.md) — Postgres advisory locks for multi-process safety

**Documentation:**
- See **[backend.md](backend.md)** §"Scheduled jobs summary (Phase 10)"
- See **[migrations.md](migrations.md)** §Phase 10 & PR #80

---

## Off-Meta Channels: Telegram ✅ (SHIPPED, main) — ⚠️ NATIVE-CHATWOOT cutover (uncommitted 2026-06-12)

**Feature:** First-class Telegram channel; Pro+ tier-gated. **Now a NATIVE Chatwoot `Channel::Telegram`** — inbound rides the agent-bot path (`chatwoot_router`, channel_kind 'telegram'), NOT a Polaris-owned webhook. The legacy own-webhook path was DELETED (no live bots to migrate; VPS teardown).

**Entry Points:**
- `apps/api/polaris_api/services/telegram_client.py` — Bot API client (outbound `send_message`/`send_photo` + `get_me`)
- Inbound: `apps/api/polaris_api/webhooks/chatwoot_router.py` (native) — ⚠️ legacy `webhooks/telegram_router.py` + `_telegram_extract.py` + `services/webhook_resolver.py::resolve_telegram` + `scripts/seed_telegram_channel.py` **DELETED**
- `apps/api/polaris_api/routers/admin_tenants.py::connect_telegram()` — Provision endpoint (now getMe-only via `validate_telegram_bot`; no setWebhook; `telegram_webhook_secret` NULL)

**Linked Services:**
- `apps/api/polaris_api/services/pipeline.py` — Telegram inbound branch (resolve token, dispatch)
- `apps/api/polaris_api/services/reply_dispatch.py` — `_send_telegram()`
- `apps/api/polaris_api/services/channels/telegram_provision.py` — Register webhook + persist channel

**Migration:**
- **094** `telegram_channel` — Bot registry (bot_id, username, webhook_secret, partial-unique idx, charset CHECK via regex)

**Config Keys:**
- `telegram_enabled` (default True)
- `TELEGRAM_API_BASE_URL` (default https://api.telegram.org)

**Admin Provision:**
- `POST /api/admin/tenants/{id}/channels/telegram` — getMe + setWebhook + persist (Pro+ gated, audited)

**Webhook Signature:**
- `X-Telegram-Bot-Api-Secret-Token` header verification (per-bot secret in `tenant_channels.telegram_webhook_secret`)

**Tests:**
- `apps/api/tests/test_telegram.py` — 18+ tests (client, webhook, provision, pipeline)

**Portal Integration:**
- `apps/portal/app/[tenantSlug]/(app)/channels/` — Channel management

**Documentation:**
- See **[services.md](services.md)** §"Channels & Integrations"
- See **[backend.md](backend.md)** §Off-Meta Channels (Telegram M1/M2)
- See **[integrations.md](integrations.md)** §Telegram

---

## Off-Meta Channels: Website Widget ✅ (SHIPPED, main)

**Feature:** Read-only website launcher widget (no redirect, no attribution); direct t.me/wa.me links.

**Entry Points:**
- `apps/api/polaris_api/services/widget.py` — Config + allowlist validation (NEW)
- `apps/api/polaris_api/routers/widget_public.py` — Public GET (NEW)
- `apps/api/polaris_api/routers/tenant_widget.py` — Tenant GET/PUT (NEW)
- `apps/api/polaris_api/main.py::serve_widget_js()` — Static Shadow-DOM bubble

**Linked Services:**
- `apps/api/polaris_api/services/channels/provision.py` — Add widget channel

**Migrations:**
- **095** `tenant_widget_config` — Widget read-only allowlist + `tenant_widget_enabled` CHECK
- **096** `widget_readonly_reconcile` — Constraint audit + 094 secret-CHECK fix

**Config Keys:**
- `widget_enabled` (default True)
- `widget_cache_ttl_seconds` (60 default)

**Public API:**
- `GET /api/v1/widget/{key}/config` — PUBLIC (no auth), returns allowlist targets (cached 60s)
- `GET /widget.js` — Static Shadow-DOM launcher

**Tenant API:**
- `GET /api/tenant/widget` — Fetch config + embed snippet
- `PUT /api/tenant/widget` — Update allowlist-validated targets

**Tests:**
- `apps/api/tests/test_widget.py` — 12+ tests (config, public, allowlist)

**Portal Integration:**
- `apps/portal/app/[tenantSlug]/(app)/channels/` — Widget embed snippet copy

**Documentation:**
- See **[services.md](services.md)** §"Channels & Integrations"
- See **[backend.md](backend.md)** §Off-Meta Channels (Widget M3/M4)

---

## Voice Notes M1 ✅ (SHIPPED, main)

**Feature:** Inbound voice notes → Groq Whisper transcription → text reply (Pro+ only; EN/MS/ZH auto-detect).

**Entry Points:**
- `apps/api/polaris_api/services/media_download.py` — Per-channel audio fetch (NEW)
- `apps/api/polaris_api/services/transcription.py` — Groq Whisper API (NEW)
- `apps/api/polaris_api/services/pipeline.py` — Voice branch (BEFORE media-only branch)

**Config Keys:**
- `GROQ_API_KEY` (placeholder-fail-closed)
- `transcription_model` (whisper-large-v3 default)
- `transcription_enabled`, `transcription_stub_enabled` (E2E testing)
- `transcription_timeout_s` (30 default)
- `transcription_max_download_bytes` (50 MB default)
- `voice_note_tiers` (frozenset {pro, ultra, enterprise})

**Tier Gate:** Pro+ only; Starter → fallback ("please type" + Pro+ feature CW note)

**Chatwoot Integration:** Voice Note: <transcript> private note posted on success

**Tests:**
- `apps/api/tests/test_media_download.py` — 15 tests (per-channel auth, token non-leak, oversize)
- `apps/api/tests/test_voice_note_transcription.py` — 21 tests (transcribe, pipeline, fallback, notes)

**Status:** ✅ SHIPPED on main (batch-merged 2026-06-06 alongside email + KGD M1)
**Next steps:** M2 = flow-builder voice (deferred); live E2E with real GROQ_API_KEY

**Documentation:**
- See **[services.md](services.md)** §"Media & Attachments"
- See **[backend.md](backend.md)** §"Inbound voice notes — M1"

---

## Knowledge Gap Detection M1 ✅ (SHIPPED, main)

**Feature:** Ultra-only capture of knowledge gaps (no_retrieval, handoff, low_retrieval signals) + dashboard.

**Entry Points:**
- `apps/api/polaris_api/services/knowledge_gaps/capture.py` — Signal classification + record (NEW)
- `apps/api/polaris_api/services/knowledge_gaps/aggregate.py` — Dashboard aggregation (NEW)
- `apps/api/polaris_api/routers/tenant_knowledge_gaps.py` — GET /summary (NEW)

**Linked Services:**
- `apps/api/polaris_api/services/pipeline.py` — Threads `capture_ok` to handoff / Phase-4 / flow sites
- `apps/api/polaris_api/services/llm.py` — Stashes `last_rag_used` + `last_rag_top_score` for post-generate parse

**Migration:**
- **098** `knowledge_gap_misses` — Capture table (signals, customer_question, created_at; applied)

**Config Keys:**
- `knowledge_gap_capture_enabled` (default False)
- `knowledge_gap_retrieval_threshold` (0.0=off, default off)
- `knowledge_gap_tiers` (frozenset {ultra, enterprise})

**Tier Gate:** Ultra+ only (both capture AND dashboard); 403 KNOWLEDGE_GAPS_TIER_BLOCKED below Ultra

**Signals (2 real, 1 lever):**
- `no_retrieval` — zero RAG chunks returned
- `handoff` — real question → human escalation
- `low_retrieval` (lever, default off) — top RAG score below threshold

**Dashboard Aggregation:** M2-lite SQL grouping (lowercase, strip punctuation, collapse whitespace); no embeddings; top-50 themes

**Capture Safety:** SAVEPOINT fail-soft; never poisons outer tx (ECC HIGH fix applied)

**Tests:**
- `apps/api/tests/knowledge_gaps/test_capture.py` — Classify/score/parse/truncation/tier
- `apps/api/tests/knowledge_gaps/test_wiring.py` — Pipeline integration
- `apps/api/tests/knowledge_gaps/test_tenant_knowledge_gaps_router.py` — 200/403/empty scenarios
- 306 backend tests green total

**Status:** ✅ SHIPPED on main (merged 2026-06-06; migration 098 applied)
**Next steps:** M2 semantic clustering, notification-on-new-gap, M4 Add-answer→KB

**Documentation:**
- See **[services.md](services.md)** §"Knowledge & RAG"
- See **[backend.md](backend.md)** §"Knowledge Gap Detection — M1 capture + M3-lite Ultra dashboard"
- See **[migrations.md](migrations.md)** §Knowledge Gap Detection

---

## Knowledge Gaps Interactivity ✅ (SHIPPED, v1.0.39, 2026-06-14)

**What:** Extends KGD M1 with a configurable look-back window and a drill-down endpoint exposing raw miss examples per theme.

**Backend (`routers/tenant_knowledge_gaps.py`):**
- `GET /api/tenant/knowledge-gaps/summary?window_days={7|30|90}` — window now a validated query param (was fixed 30-day). 422 `INVALID_WINDOW` if not in allow-set; 403 `KNOWLEDGE_GAPS_TIER_BLOCKED` below Ultra (unchanged).
- `GET /api/tenant/knowledge-gaps/summary/examples?q=<norm_q>&window_days=N` — NEW drill-down; `q` = `norm_q` key from /summary; returns `KnowledgeGapExamplesResponse` (raw miss rows, ≤20).

**Service (`services/knowledge_gaps/aggregate.py`):**
- `gap_examples(tenant_id, pool, norm_q, window_days)` — NEW function + `_EXAMPLES_SQL`.
- `_THEMES_SQL` SELECT now includes `norm_q`; `KnowledgeGapTheme` += `norm_q: str`.

**Shared-types (`packages/shared-types/python/polaris_shared_types/knowledge_gaps.py`):**
- `KnowledgeGapTheme` += `norm_q: str`; NEW `KnowledgeGapExample`, `KnowledgeGapExamplesResponse`; `schema.json` regenerated.

**Portal:**
- `app/(portal)/knowledge-gaps/page.tsx` — 7/30/90-day window selector (URL `?window=`; server component).
- `components/knowledge-gaps/KnowledgeGapsList.tsx` — NEW client island: sort + signal-type filter + lazy per-theme drill-down to `/conversations/{id}`.
- `KnowledgeGapsContent.tsx` — delegates list to `KnowledgeGapsList`; stays server-renderable.
- `lib/api/routes.ts` += `knowledgeGapsExamples(q, windowDays)` + optional `windowDays` on `knowledgeGapsSummary`.

**Data cleanup:** `ops/cleanup/2026-06-knowledge-gap-echo-purge.sql` — one-off echo-purge (NOT a numbered migration; no schema change). Regression tests in `apps/api/tests/knowledge_gaps/test_capture.py`.

**Key files:** `routers/tenant_knowledge_gaps.py`, `services/knowledge_gaps/aggregate.py`, `packages/shared-types/python/polaris_shared_types/knowledge_gaps.py`, portal `components/knowledge-gaps/KnowledgeGapsList.tsx`.

---

## RAG Remediation — Hybrid Recall + Confidence ✅ (DEPLOYED v1.0.39, PR #115 `c964fcc`, 2026-06-15)

**Context:** The bot RAG/chatflow QA test pass (`../docs/internal-ops/PRPs/reports/bot-rag-chatflow-test-report.md`) found two structural defects (F2, F4) and several missing features (F7 promos, F9 em-dash). This section covers the full remediation shipped in PR #115.

### F4 — Hybrid BM25 recall fix (migration 113)

**Root cause:** Both `match_knowledge_chunks_hybrid` and `match_products_hybrid` used `websearch_to_tsquery` for the BM25 leg — AND-semantics that required every word of the query to appear in the chunk. Ordinary questions ("how long does delivery take", "what are the payment options") contain stop/common words that many chunks lacked, so the BM25 leg fired on only ~4 of 16 natural test questions. "Hybrid" search was de-facto vector-only.

**Fix:** OR-semantics: `plainto_tsquery('english', q)` then `replace(…::text, ' & ', ' | ')::tsquery` so a chunk matching **any** query term is a candidate; `ts_rank` still orders hits, RRF still fuses with the vector leg. Quoted-phrase support (websearch-only) is dropped — acceptable for a FAQ/support bot. Vector-proximity tiebreaker (`vector_distance ASC NULLS LAST`) handles the richer RRF ties that OR-semantics introduces.

### F2 — Retrieval confidence signal (migration 113)

**Root cause:** Both hybrid RPCs returned only `rrf_score` (rank-derived: the #1 vector hit is always `1/(k+1)` regardless of true cosine similarity), so nothing downstream could distinguish a strong match from the nearest-of-the-irrelevant.

**Fix:** Both RPCs now return `vector_distance REAL` (cosine distance, lower = closer; `NULL` for BM25-only hits that the vector leg didn't surface). Consumed by:
- `knowledge_gaps/capture.py` — `classify_score_miss()` uses `vector_distance` as the confidence floor (threshold default 0.0 = off until tuned)
- `escalation.py` — `is_unhelpful` signal derived from the miss classification

### Cohere no-crash degrade (rag.py)

`CohereDisabledError` was uncaught when `COHERE_API_KEY` is blank (the prod default). For Ultra/Enterprise tenants this would crash the RAG reply. Now caught and logged as `rag.cohere_disabled_degrade`; falls back to vector-only results.

### Products + active promotions in default reply path

**Before:** Products were only surfaced via the agent tool (`lookup_products`) or flow nodes. Active promotions (`tenant_promotions`) were only visible if an operator manually copied them into a KB chunk.

**After (PR #115):**
- `services/products/search.py` — `match_products_hybrid` called from the default LLM reply path (not just agent/flow tools).
- `services/promotions/lookup.py` (NEW) — `fetch_active_promotions()` queries `tenant_promotions` for `is_active AND now() BETWEEN valid_from AND valid_until`, branch-scoped, capped at 5. Both injected into the LLM prompt context on every default turn.

### Opt-in human escalation (services/escalation.py NEW)

After 2 consecutive unhelpful bot replies (low/no retrieval confidence), the bot offers to connect the customer to the team (`offer_text` EN/MS/ZH, no em-dashes). On the next turn, `interpret_consent()` (multilingual exact-match + whole-word scan) reads the reply: yes → Chatwoot handoff + `accept_text`; no → streak reset; unclear → pipeline consults LLM. Never escalates without a clear yes. `EscalationState` persisted as JSON in Redis per conversation.

### Other pipeline hardening (PR #115)

- **Em-dash sanitizer** (F9): strips em-dashes from bot reply text before sending (em-dashes render as `—` in WhatsApp/Telegram but are absent from natural SMS-style text).
- **STOP/UNSUBSCRIBE opt-out hardened**: additional keyword variants; `stop_keyword.py` expanded.
- **Follow-up query condensation**: multi-turn context collapsed before RAG (improves retrieval on follow-up questions that reference prior context).

**Migration:** 113 (APPLIED + LIVE on prod, operator-confirmed 2026-06-15).

**Key files:** `packages/db-migrations/113_hybrid_search_or_semantics_and_distance.sql`, `services/rag.py`, `services/escalation.py` (NEW), `services/promotions/lookup.py` (NEW), `services/products/search.py`, `services/pipeline.py`, `services/knowledge_gaps/capture.py`, `services/llm.py`, `services/_llm_prompts.py`, `services/stop_keyword.py`.

**Tests:** `tests/test_escalation.py` (NEW), updates to `test_rag_cohere_tier_gate`, `test_stop_keyword`, `test_product_search`, `test_knowledge_search_service`.

---

## infra/ship.sh — Canonical Release Pipeline ✅ (DEPLOYED v1.0.39, commit 48eb307, 2026-06-15)

**What:** `infra/ship.sh` replaces the manual `infra/build.sh --push` flow as the canonical way to cut a Polaris release.

**Subcommands:** `preflight → gitsync → build → release` (or `all` for the full sequence).
- `preflight` — detects branch/dirty state; auto-bumps tags (`v1.0.39 → v1.0.40`, `v1.6.0-r5 → v1.6.0-r6`); checks fork status (dirty/ahead/untagged → build; clean → skip); writes `.ship-state.env`; fails fast on a bad GHCR token.
- `gitsync` — runs verify gate (`make verify` or ruff+prettier+pytest fallback); commits working tree if dirty; merges feature branch → main + prunes; or pushes main directly if already on main.
- `build` — parallel `docker buildx bake` via `infra/docker-bake.hcl` for all needed targets (api/portal/admin + ea/cw forks when changed); pushes to GHCR. `--sequential` flag for OOM situations.
- `release` — bumps image pins in `infra/docker-compose.prod.yml` for built targets; commits + pushes; prints the `ssh <vps> 'bash vps-update.sh'` line. Stops before deploy — operator runs VPS step manually.

**Flags:** `--app-tag`, `--ea-tag`, `--cw-tag` (override auto-bump); `--ea`/`--no-ea`, `--cw`/`--no-cw` (force include/skip forks); `--no-verify`, `--no-latest`, `--sequential`, `--verbose`, `--branch`, `--main`, `-m`.

**Key files:** `infra/ship.sh`, `infra/docker-bake.hcl`.

---

## config.py blank-bool coercion ✅ (DEPLOYED v1.0.39, commit 48eb307, 2026-06-15)

**What:** `config.py` now coerces blank/whitespace `.env` values for bool-typed settings to their declared default instead of crashing with a pydantic validation error.

**Root cause:** `LLM_STUB_ENABLED=`, `COST_STUB_ENABLED=`, `TRANSCRIPTION_STUB_ENABLED=` in `.env` (bare empty string, meaning "use the default") caused `Settings` import to fail. `_scrub_empty_env_vars()` only cleared blank OS env vars; blanks from the `.env` file itself escaped the scrub.

**Fix:** `_blank_bool_uses_default` validator: for any bool-typed field whose incoming value is a blank/whitespace string, coerce to the field's declared default. No more "set stub flags to False explicitly" workaround needed.

**Key file:** `apps/api/polaris_api/config.py` (`_blank_bool_uses_default` model_validator).

---

## Email Channel ✅ (SHIPPED, main)

**Feature:** Chatwoot-native email handling (no new IMAP/SMTP stack) with bot-assist draft generation.

**Entry Points:**
- `apps/api/polaris_api/services/email_assist.py` — Draft generation
- `apps/api/polaris_api/services/email_assist_queue.py` + `email_assist_worker.py` — Dedicated queue + worker
- `apps/api/polaris_api/routers/admin_tenants.py` — IMAP/SMTP config (`/channels/email-config`, `/channels/email-credentials/{inbox_id}`)
- `apps/api/polaris_api/routers/tenant_settings.py` — `email_draft_prompt` (GET/PATCH)
- Provisioning: admin `POST /channels` (`channel_kind=email`) or `scripts/seed_email_channel.py`; both attach the Polaris Bot to the inbox

**Linked Services:**
- Chatwoot natively owns IMAP/SMTP/OAuth → Polaris adds draft-assist only
- `apps/api/polaris_api/services/llm.py` — Haiku draft generation
- `apps/api/polaris_api/services/chatwoot/client.py` — Post draft as private note (never auto-send)

**Migration:**
- **097** `email_channel` — `email` in `tenant_channels` CHECK + `email_drafts` table (applied)

**Config Keys:**
- `email_draft_prompt` (per-tenant, settings JSONB; editable in the portal Settings → "Email draft tone", Pro+)

**Tier Gate:** Pro+ (in `allowed_channel_kinds`). Human-first; bot posts a private-note draft only, never auto-sends.

**BYO Mailbox:** App-password (Gmail) or OAuth (Microsoft; deferred behind CASA)

**Worker Pattern:** Dedicated email-assist consumer (separate from inbound pipeline)

**Status:** ✅ SHIPPED on main (merged 2026-06-06 alongside voice-notes #84 + KGD M1; human-handling tag pushed direct-to-main 2026-06-07)  
**Next steps:** M2 (Google OAuth CASA), M3/M4 deferred

**Documentation:**
- See **[services.md](services.md)** §"Email Assist"

---

## Phase 20: Meta Mock Infrastructure + Broadcast E2E ✅ (SHIPPED, main)

**Feature:** In-memory mock of Meta Graph API for E2E testing; broadcast seeding + draining.

**Entry Points:**
- `apps/api/polaris_api/routers/meta_mock.py` — Catch-all POST router (NEW)
- `apps/api/polaris_api/services/meta_mock_store.py` — In-memory capture store (NEW)
- `apps/api/polaris_api/e2e/router.py` — 3 broadcast E2E endpoints (NEW; relocated from `routers/internal_e2e.py`, audit Batch E 2026-07-02 — same `/internal/e2e` mount)

**Linked Services:**
- `apps/api/polaris_api/services/meta_whatsapp.py` — Uses `settings.meta_api_base_url` (configurable; E2E mock override)
- `apps/api/polaris_api/services/broadcast/worker.py` — `_drain_broadcast()` (called by E2E tick endpoint)

**Config Keys:**
- `meta_api_base_url` (default https://graph.facebook.com; E2E override via config)
- `meta_mock_enabled` (E2E only, default False)
- `POLARIS_META_MOCK_ENABLED` env var

**E2E Endpoints (all gated by `POLARIS_E2E_INTERNAL_SECRET`):**
- `POST /internal/e2e/broadcast-e2e-seed` — Upsert template + broadcast + recipients
- `POST /internal/e2e/broadcast-e2e-tick` — DB-driven drain (not Redis)
- `POST /internal/e2e/broadcast-recipients-query` — Fetch recipient rows

**Critical Fix:** E2E re-seed NULL out `wa_message_id` on prior recipients (prevents unique index collision)

**Tests:**
- `apps/portal/e2e/broadcast.spec.ts` + `apps/admin/e2e/broadcasts.spec.ts` — Full broadcast E2E
- Phase 20 E2E gate: 11 pass, 9 skip, 0 fail

**Documentation:**
- See **[backend.md](backend.md)** §"Phase 20: Meta Mock Infrastructure + Broadcast E2E"

---

## Cross-Feature Index

| Feature | File/Router | Service | Scheduled Job | Config | Migration | Tests |
|---------|------------|---------|----------------|--------|-----------|-------|
| **Product Catalog** | `tenant_products.py` | `products/{storage,search}.py` | — | product_image_max_size_mb | 089–090 | test_tenant_products.py |
| **Follow-ups** | `tenant_followups.py` | `followups/{timing,eligibility,scheduler,worker}.py` | followup_worker.tick_due | followup_* | 091–092 | test_followups.py |
| **MRR Persistence** | `admin_financials.py` | `mrr_rollup.py` | mrr_rollup (locked) | — | 093 | test_mrr.py |
| **Telegram** | `admin_tenants.py::connect_telegram` | `telegram_client.py` | — | telegram_enabled | 094 | test_telegram.py |
| **Widget** | `tenant_widget.py`, `widget_public.py` | `widget.py` | — | widget_* | 095–096 | test_widget.py |
| **Voice M1** | `pipeline.py` (voice branch) | `media_download.py`, `transcription.py` | — | transcription_*, voice_note_tiers | — | test_voice_note_transcription.py |
| **KGD M1** | `tenant_knowledge_gaps.py` | `knowledge_gaps/{capture,aggregate}.py` | — | knowledge_gap_* | 098 | test_knowledge_gaps.py |
| **KGD interactivity** | `tenant_knowledge_gaps.py` (window param + /examples endpoint) | `aggregate.gap_examples` + `_EXAMPLES_SQL` | — | — | — (ops/cleanup only) | test_capture.py (echo-drop regression) |
| **Email** | `chatwoot_router` | `email_assist{,_queue,_worker}.py` | email_assist_worker | `settings.email_draft_prompt` | 097 | test_email_assist*.py |
| **Meta Mock** | `meta_mock.py` | `meta_mock_store.py` | — | meta_api_base_url, meta_mock_enabled | — | broadcast.spec.ts |
| **Cost Metrics** | `admin_cost.py` | `metrics.py`, `grafana_cost.py` | — | USD_TO_MYR_RATE, GRAFANA_CLOUD_MIMIR_* | — | — |
| **Agent Surface** | `tenant_settings.py` (agent_config) | `agent/{config,loop,pipeline_integration,tools/}.py` | — | agent_config.enabled | 105 | test_agent*.py |
| **HUMAN_AGENT tag** | ⚠️ REMOVED-from-Polaris (phase-10): tag lived in deleted `_dispatch_agent_forward`; tagging+delivery now Chatwoot-native | — | — | — | — | — |
| **IG-Login** | `webhooks/chatwoot_router.py` (inbound; meta_router deleted) | `meta_messaging.py` (InstagramLoginClient, send) | — | META_INSTAGRAM_APP_SECRET | 104 | test_instagram_login.py |
| **Booking Notifications** | `routers/tenant_bookings.py` | `booking/{link_flow,_notify,config}.py` | booking_reminders | booking_config JSONB | — | — |
| **Conversations channel_id** | — | `pipeline.py` (WABA pin) | — | — | 106 | — |
| **Chatwoot-native inbound** | `webhooks/chatwoot_router.py`, `_status_gate.py` | `pipeline.py` (link, deferred CW), `chatwoot/{agent_bot,label_sync}.py` | — | use_chatwoot_inbound (vestigial), CHATWOOT_WEBHOOK_HMAC_SECRET | 108–109 | test_chatwoot_*.py, test_pipeline_cw_mirror.py |
| **AI Agent screen** | `app/(portal)/agent/`, `tenant_settings.py` | — | — | agent_config + read-only default_system_prompt/agent_guardrails | — | — |
| **RAG hybrid recall** | — | `rag.py`, `knowledge_search.py`, `knowledge_gaps/capture.py` | — | knowledge_gap_retrieval_threshold | 113 | test_rag_cohere_tier_gate, test_knowledge_search_service |
| **Products in default path** | — | `products/search.py`, `pipeline.py` | — | — | — | test_product_search |
| **Active promos in default path** | — | `promotions/lookup.py` (NEW) | — | — | — | — |
| **Opt-in escalation** | — | `escalation.py` (NEW) | — | — | — | test_escalation.py |
| **infra/ship.sh** | `infra/ship.sh`, `infra/docker-bake.hcl` | — | — | GITHUB_TOKEN | — | — |

---

## How to Find Code

**By feature name:**
- Product Catalog → `docs/CODEMAPS/backend.md` §Phase 15
- Follow-ups → `docs/CODEMAPS/backend.md` §"AI follow-ups"
- Telegram → `docs/CODEMAPS/backend.md` §Off-Meta Channels
- Voice Notes → `docs/CODEMAPS/backend.md` §"Inbound voice notes"
- Knowledge Gaps → `docs/CODEMAPS/backend.md` §"Knowledge Gap Detection"

**By service module:**
- See `docs/CODEMAPS/services.md` (comprehensive 45+ module reference)

**By migration:**
- See `docs/CODEMAPS/migrations.md` (all 120 migrations with purpose + FK relationships + constraints; next = 121)

**By router:**
- See `docs/CODEMAPS/backend.md` §Routes (39 routers, all 200+ endpoints)

