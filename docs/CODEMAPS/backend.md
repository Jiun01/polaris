
# Backend Codemap (apps/api)

## Router Mount Points

```
/v1/webhooks/chatwoot → webhooks/chatwoot_router.py  PRIMARY inbound (agent-bot, HMAC) — rehaul front door
                        (Telegram rides this too now — native Channel::Telegram, channel_kind
                         'telegram'; legacy telegram_router.py + resolve_telegram DELETED, v1.0.31 LIVE)
                        (unknown inbox_id → LAZY auto-provision on first inbound via
                         services/channels/lazy_provision.py — LIVE on main, wired at
                         chatwoot_router.py:73 (import) and :590 (call); see
                         docs/SSOT/features/Channel Provisioning.md)
/v1/webhooks/ea       → webhooks/ea_router.py  EA appointment-confirm callback (v1.0.67 LIVE)
                        POST only; HMAC X-Polaris-Signature (raw body, ea_webhook_hmac_secret);
                        verifies polaris_ref binding token → link_flow.confirm_booking;
                        idempotent on re-delivery; 401 on bad HMAC, 400 on unknown ref.
                        EA emitter: Booking::notify_polaris_appointment_confirmed() in polaris-ea-fork.
/v1/webhooks/meta/*   → wa_status / meta_template_status / wa_account routers
                        non-message WA events ONLY (delivery/read/template/account),
                        delivered via the fork-relay (NOT customer messages)
/api/tenant/*         → routers/tenant_*.py        portal JWT (tenant_user role)
/api/admin/*          → routers/admin_*.py         platform_admin JWT
                        (admin_tenants.py split, audit Batch E 2026-07-02: 2010→1093 lines;
                         admin_tenant_channels.py + admin_tenant_email.py carved out, same
                         /api/admin/tenants prefix, no path changes)
/internal/e2e/*       → polaris_api/e2e/router.py  Bearer POLARIS_E2E_INTERNAL_SECRET (E2E only)
                        (relocated from routers/internal_e2e.py, audit Batch E 2026-07-02;
                         same mount path, same behavior)
/b/{code}             → routers/booking_redirect.py  PUBLIC, no auth — short-link booking redirect
                        (v1.0.64): GET /b/{code} → service-pool lookup by jti (binding_token),
                        re-mints signed token with REMAINING TTL → 307 to EA wizard URL built via
                        build_ea_wizard_url; 410 on expired/unknown code or null ea_service_id/provider_id.
                        Allowlisted in scripts/lint_service_pool.py.
/v1/health            → health check
```

> **meta_router.py DELETED (rehaul):** there is no direct Meta→Polaris customer-message inbound.
> Customer messages enter via Chatwoot's agent-bot → `/v1/webhooks/chatwoot`. `main.py` refuses a
> non-local boot when `CHATWOOT_WEBHOOK_HMAC_SECRET` is unset (the agent-bot front door must be
> verifiable).

## Admin Health Endpoint (Phase 9 PR-6)

```
GET  /api/admin/health                                      → HealthSnapshot (single endpoint)
  Single aggregated snapshot cached 60s; 15+ independent probes
  run in parallel (DB sequential, Redis/HTTP concurrent).
  Each probe silently degrades on failure (NEVER fails whole response).
  One request maps to one HealthSnapshot with 17 signals:

  Infrastructure:
    - supabase: SELECT 1 latency + connectivity
    - redis: PING + INFO memory

  Service & Channel Connectivity:
    - chatwoot: HTTP reachability (any response OK)
    - ea: HTTP reachability probe (status < 500 = healthy)
    - telegram_webhooks: webhook_events surface (1 h window)
    - meta_webhooks: webhook_events surface (1 h window)
    - meta_rate_limits: X-Business-Use-Case-Usage header avg (24 h)

  Traffic & Engagement:
    - active_tenants: message volume (24 h distinct tenants)
    - messaging: inbound/outbound + bot replies MTD
    - email_drafts: queue depth + resolved outcomes (24 h)
    - knowledge_gaps: miss captures by signal type (24 h)

  Spend & Quota:
    - llm: token burn + est. MYR cost (MTD)
    - quota: tenant saturation vs cap (all tenants, top-5)
    - broadcasts: queue depth (pending/in_flight/failed)

  Webhook & Errors:
    - webhook_event_errors: ingest error breakdown (24 h)
      Groups by status/error_class; excludes benign statuses
      (received/processed/replay_ignored/human_handling/demo_routed/
       non_message_event/chatwoot_deferred); shows top-N + recent rows

  Observability:
    - failures: broadcast errors + deep-links (Grafana/Sentry)
    - apm: Sentry error count (24 h, requires token)
```

## Admin Routes

```
GET  /api/admin/quotas/usage                                → all-tenants quota + bot-reply usage
PUT  /api/admin/quotas/{id}/override                        → single-column override (16 allowed, ADR-017;
                                                              value now int|list[str]|None — allowed_channel_kinds
                                                              editable [branch], canonical-set validated)
GET  /api/admin/tenants                                     → tenant list
POST /api/admin/tenants                                     → create + seed quota + magic-link
                                                              (+ optional allowed_channel_kinds override [branch])
PATCH /api/admin/tenants/{id}                               → suspend/unsuspend/update
DELETE /api/admin/tenants/{id}                              → soft-delete (30-day grace)
POST /api/admin/tenants/{id}/hard-delete                    → SECURITY DEFINER cascade
POST /api/admin/tenants/{id}/channels                       → add channel (+ token auto-capture)
DELETE /api/admin/tenants/{id}/channels/{channel_kind}      → remove ONE channel + its secret (v1.0.22)
GET  /api/admin/tenants/{id}/channels/inbox-info            → CW inbox auto-fill
GET  /api/admin/tenants/{id}/channels/email-config          → CW email inboxes (read-only)
PATCH /api/admin/tenants/{id}/channels/email-credentials/{inbox_id} → rotate imap/smtp pwd
PATCH /api/admin/tenants/{id}/chatwoot-account              → set chatwoot_account_id
GET  /api/admin/secrets/{id}                                → secret refs (no plaintext)
POST /api/admin/secrets/{id}/rotate                         → re-encrypt + audit
GET  /api/admin/financials/mrr                              → read-only MRR from snapshots (excludes is_demo tenants)
GET  /api/admin/health                                      → System health snapshot (see Admin Health Endpoint above)

GET  /api/admin/demos                                       → list demo configs (+ optional tenant_id filter)
POST /api/admin/demos                                       → create demo (hidden tenant + demo code + caps)
PATCH /api/admin/demos/{id}                                 → update demo config
DELETE /api/admin/demos/{id}                                → delete demo config
POST /api/admin/demos/{id}/reset-visitor                    → reset one-demo-ever for a contact
GET  /api/admin/demos/{id}/sessions                         → list active/recent sessions
```

## Key Services (polaris_api/services/)

```
quota.py          → check_quota() / increment_counter()
                    Redis key: bot_replies:{tenant_id}:{YYYYMM}
                    Tier defaults: starter=5k, pro=25k, ultra=60k, enterprise=0 (custom)
                    Audit Batch B (2026-07-02): quota-cap now Redis-cached with a 300s tier TTL
                    (was re-read every turn).
audit.py          → record() — append hash-chain row to audit_logs
secrets.py        → PyNaCl encrypt/decrypt via POLARIS_KMS_KEY
llm_reply.py      → structured-LLM trigger + product catalog + KB retrieval
agent/            → tool-calling agent (ADR-063, PR #99). tools/ @register_tool
                    registry + 4 tools (search_knowledge, lookup_products, handoff_to_human,
                    capture_lead) reusing agent/ports.py adapters (inherited from the now-
                    retired flow_runtime — audit Batch D, 2026-07-02); loop.py (Haiku/Sonnet
                    tool-use loop w/ ADR-031 tier routing D4, iteration cap);
                    pipeline_integration.try_run_agent (Batch B: per-turn embed/search
                    memoization added; 2026-07-03: the memoization lookup/populate logic
                    shared by RAGAdapter.search and ProductsAdapter.search_products was
                    extracted into a private ports.py::_embed_cached() helper, was
                    duplicated inline in each adapter); config.parse_agent_config
followups/        → re-engagement scheduler + worker (in-process lifespan)
email_assist/     → email draft queue consumer (posts CW private note, never auto-sends)
knowledge_gaps/   → miss detection + summary (Ultra-only capture)
                    aggregate.py: summarize_gaps(window_days) + gap_examples(norm_q, window_days)
                    _THEMES_SQL now SELECTs norm_q; _EXAMPLES_SQL (new) raw-miss drill-down ≤20 rows
_llm_prompts.py   → layered prompt assembly (PR #99 + v1.0.55): build_response_system_prompt
                    (chat), build_email_draft_prompt (email), build_agent_system_prompt (agent);
                    non-overridable _SAFETY_GUARDS; [HANDOFF_TRIGGER] deleted.
                    v1.0.55: DEFAULT_PERSONA_BLOCKS, default_persona_blocks(),
                    render_persona_blocks(), coerce_persona_blocks(); _base_persona
                    precedence: persona_blocks > system_prompt override > _V1_TEMPLATE
llm.py            → classify_intent / generate_response (channel-aware) / generate_with_tools
                    (Batch A, 2026-07-02: exception taxonomy + OpenRouter fallback on
                    Anthropic failure classes, was a bare catch-all); NLU helpers moved OUT
                    to booking/nlu.py (Batch E structural split)
http_client.py     → NEW (Batch B, 2026-07-02) — lifespan-shared httpx.AsyncClient singleton
                    (init_http_client/close_http_client/get_http_client_or_none, mirrors the
                    redis_pool.py singleton shape). Adopted by meta_whatsapp.py,
                    chatwoot/client.py, booking/client.py, media_download.py — these no
                    longer open a fresh AsyncClient (fresh TLS handshake) per outbound call.
                    get_http_client_or_none() returns None outside the lifespan (unit
                    tests/scripts) so callers fall back to a per-call client unchanged.
```

## Tenant agent config + persona blocks (PR #99, v1.0.55)

```
GET  /api/tenant/settings  → ...+ agent_config (AgentConfig) + agent_tool_catalog (toggle list)
                             + default_system_prompt (read-only) + agent_guardrails (read-only)
                             + persona_blocks (list[PersonaBlock]) [v1.0.55]
                             + default_persona_blocks (list[PersonaBlock], read-only) [v1.0.55]
PATCH /api/tenant/settings → ...+ agent_config (whole-object → tenants.settings JSONB)
                             + persona_blocks (validated list → tenants.settings JSONB) [v1.0.55]
  AgentConfig = {enabled, instructions, enabled_tools[], max_tool_iterations};
    default disabled for existing tenants; NEW tenants seeded with enabled=True + search_knowledge
    + handoff_to_human tools (admin_tenants.py tenant-create, v1.0.55 — reverses ADR-063
    "agent off by default" for new tenants; ADR append pending).
  PersonaBlock = {display_name, slug, body}; stored in tenants.settings.persona_blocks JSONB
    (no new column, no migration). Slug pattern validated; RESERVED_BLOCK_SLUGS enforced;
    max 10 blocks; duplicate-slug rejected. Shared-types SSOT:
    packages/shared-types/python/polaris_shared_types/agent_config.py —
    PersonaBlock, RESERVED_BLOCK_SLUGS, slugify_block_name(),
    RECOMMENDED_PERSONA_BLOCKS, MAX_PERSONA_BLOCKS=10.
  PATCH JSONB-merge refactor [v1.0.55]: all settings-JSONB keys merged in a SINGLE
    settings = settings || jsonb_build_object(...) clause (Postgres rejects assigning a
    column twice in one UPDATE — the old multi-key PATCH would error when patching >1
    settings key simultaneously).
  load_tenant_snapshot now surfaces persona_blocks from settings JSONB (one hot-path read).
  default_system_prompt + agent_guardrails feed the /agent portal screen (persona prefill +
  read-only guardrails); both are read-only on the settings response.
```

## RAG Skip Flag (Batch B, 2026-07-02 audit-remediation)

`config.py::simple_tier_skip_rag` — **default OFF**. When enabled, products/promotions
RAG fetches are skipped for turns classified as the `simple` intent tier (ADR-031), since
a simple turn rarely needs KB context and the RAG round-trip was a fixed per-turn cost
regardless of tier. Operator-flippable; off by default preserves current behavior exactly
until explicitly enabled per tenant/globally.

## RAG Remediation (PR #115, v1.0.39 — commit c964fcc)

```
Migration 113 — both match_knowledge_chunks_hybrid + match_products_hybrid rewritten:
  F4: BM25 leg now OR-semantics (plainto_tsquery + replace ' & '→' | '); any matching
      term qualifies a chunk as a candidate. Previously AND-semantics (websearch_to_tsquery)
      required every query word — the BM25 leg fired on only ~4/16 natural questions,
      making "hybrid" de-facto vector-only. Quoted-phrase support dropped (acceptable for FAQ).
  F2: Both RPCs now return vector_distance REAL (cosine, lower=closer; NULL for BM25-only
      hits). Downstream: knowledge_gaps/capture.py relevance floor + escalation confidence.
  Tiebreaker: ORDER BY rrf_score DESC, vector_distance ASC NULLS LAST (handles the richer
      RRF ties that OR-semantics introduces; BM25-only hits trail vector hits on ties).

rag.py — Cohere no-crash degrade (+23 lines):
  CohereDisabledError was uncaught when COHERE_API_KEY is blank (prod default). Now
  caught and degrades to vector-only (no crash, logged as rag.cohere_disabled_degrade).

knowledge_gaps/capture.py — confidence-based gap logging (+50 lines):
  Consumes vector_distance from the hybrid RPC as the confidence signal. classify_score_miss()
  now operates on vector_distance (lower=closer = higher confidence) with a configurable
  threshold (default 0.0 = off until tuned). classify_retrieval_miss() used for no_retrieval.
  Feeds escalation.register_reply() is_unhelpful signal.

services/escalation.py — NEW opt-in human escalation (+153 lines):
  Pure state machine (no DB/LLM deps — unit-testable). Tracks consecutive unhelpful replies
  (unhelpful_streak) per conversation in Redis. After 2 unhelpful turns: offers to connect
  to the team (offer_text EN/MS/ZH) + sets awaiting_consent=True. Next inbound:
  interpret_consent() (multilingual heuristic: exact-match + whole-word scan across EN/BM/ZH)
  → "yes" → handoff + accept_text; "no" → reset streak; "unclear" → pipeline consults LLM.
  EscalationState.to_dict/from_dict for Redis serialisation. Pipeline owns persistence.

services/promotions/lookup.py — NEW active promotions in default path (+77 lines):
  fetch_active_promotions(conn, tenant_id, tenant_branch_id) queries tenant_promotions
  (migration 053) for is_active AND now() BETWEEN valid_from AND valid_until, branch-scoped
  (NULL row = shared; capped at 5 most-recently-updated). Previously promos were accessible
  only if copied into a knowledge chunk — now surfaced in every default LLM reply.

services/products/search.py — products in default reply path:
  Product hybrid search (match_products_hybrid) now called from the default reply path
  (not just the agent/flow tools). Consumes the updated vector_distance column.

services/pipeline.py (+296 lines):
  - products + promos fetched and injected into the LLM prompt on the default path
  - follow-up query condensation (multi-turn context collapse before RAG)
  - confidence-based gap logging (feeds escalation state machine)
  - opt-in escalation wiring (EscalationState, Redis persistence, consent branch)
  - STOP/UNSUBSCRIBE opt-out path hardened
  - em-dash sanitizer (F9: strips em-dashes from bot replies before send)

services/llm.py + services/_llm_prompts.py (+93 + +54 lines):
  - confidence field threaded through generate_response result
  - em-dash sanitizer applied post-generation
  - STOP opt-out message prompt updated

services/stop_keyword.py:
  STOP/UNSUBSCRIBE opt-out path expanded (additional keyword variants).

New tests: tests/test_escalation.py, updates to test_rag_cohere_tier_gate,
  test_stop_keyword, test_product_search, test_knowledge_search_service.
```

## Knowledge Gaps API (tenant_knowledge_gaps.py — v1.0.39, Ultra-only)

```
GET /api/tenant/knowledge-gaps/summary?window_days={7|30|90}
    → KnowledgeGapsSummaryResponse  (previously fixed 30-day window; now validated param)
    → 403 KNOWLEDGE_GAPS_TIER_BLOCKED (below Ultra)
    → 422 INVALID_WINDOW (window_days not in {7,30,90})
    Each theme row now includes norm_q (stable key for drill-down)

GET /api/tenant/knowledge-gaps/summary/examples?q=<norm_q>&window_days={7|30|90}
    → KnowledgeGapExamplesResponse  (NEW — raw miss rows for one theme, ≤20)
    → q must equal the norm_q value from /summary
    → same 403/422 guards as /summary
```

Shared-types SSOT (`packages/shared-types/python/polaris_shared_types/knowledge_gaps.py`):
- `KnowledgeGapTheme` += `norm_q: str`
- NEW: `KnowledgeGapExample`, `KnowledgeGapExamplesResponse`
- `schema.json` regenerated

Data cleanup: `ops/cleanup/2026-06-knowledge-gap-echo-purge.sql` — one-off echo-purge (NOT a numbered migration; no schema change).

## Nightly Jobs (scheduled/ — all advisory-locked)

| File | Schedule MYT | Purpose |
|---|---|---|
| quota_flush.py | 00:05 | Redis counters → tenant_quotas (Batch B: UNNEST batch write, was per-row) |
| mrr_rollup.py | 02:00 | Append platform_mrr_snapshots |
| audit_verify.py | 01:00 | Hash-chain verify |
| knowledge_gap_flush.py | 03:00 | KGD miss aggregation |
| broadcast_queue.py | continuous | Broadcast dispatch |
| followup_scheduler.py | continuous | Re-engagement timing |
| ingestion_scheduler.py | varies | KB ingestion queue |
| **reconcile_channels.py** | hourly | **NEW (Batch A, 2026-07-02)** — self-heals two silent-failure classes: (1) an inbox left with no platform bot attached after a lazy-provision blip (re-runs the same idempotent `attach_inbox_agent_bot` logic `scripts/backfill_agent_bot_attach.py` uses, on a schedule); (2) a channel provisioned with no usable secret (`lazy_provision`'s best-effort token capture can fail silently) — cannot self-heal (needs the original inbox context), so it logs + counts via `metrics.record_channel_reconcile_fix` for operator visibility. Uses the BYPASSRLS service pool to scan all tenants in one query. **2026-07-03:** dead `_ChannelRow` type removed, loop now acquires ONE connection for its whole run (was per-iteration). |
| ea_settings_sync.py | hourly | Pre-existing (undocumented in this table until now) — mirrors EA-side booking settings → tenant `settings.ea_calendar`/`booking_config`. |

See INDEX.md §Scheduled Jobs for the ADR-062-advisory-lock status of each job.

## Pipeline (polaris_api/services/pipeline.py — Chatwoot-native, rehaul + PR #115 + v1.0.45 demo)

**Audit Batch E structural split (2026-07-02):** `pipeline.py` (2965→2437 lines) is now a
thinner orchestrator; voice-note handling → `services/pipeline_voice.py`; quota-exhausted
escalation (CW flip/label) → `services/pipeline_quota_escalation.py`; Chatwoot reply-mirroring
+ product-image-note logic → `services/pipeline_cw_mirror.py`. Pure extraction — no behavior
change. **Audit Batch B (2026-07-02):** tenant snapshot is now fetched ONCE per turn and
threaded through (previously re-fetched at multiple points); `classify_and_condense` merges
what were two separate Haiku calls (classify intent + condense follow-up query) into one.

```
chatwoot_router enqueues IncomingMessage on the REAL provider (NOT 'chatwoot'), carrying
  chatwoot_conversation_id/_account_id from the agent-bot payload.

pipeline.run_inbound(incoming, deps)  — demo-aware entry (v1.0.45); called by inbound_worker
  → demo_runtime.resolve(incoming) → state machine (default/activate/continue_demo/expire/refuse)
  → if demo active: swap tenant_id to demo brand tenant; skip audience+quota; dispatch via
    _resolve_channel_owner() (gateway creds, not demo-tenant creds)
  → delegates to _run_inner for the actual pipeline execution

pipeline.run(incoming, deps)  — thin wrapper over _run_inner (non-demo path)
  → _run_inner (one DB transaction):
      reject provider=='chatwoot' (ValueError — that's the reverse-handoff path)
      → normalize → _link_cw_conversation (LINK existing CW conv via the payload ids;
         idempotent UPDATE — never create; fixes the duplicate-conversation bug)
      → STOP/UNSUBSCRIBE opt-out gate (expanded keyword set, PR #115)
      → quota / booking-command gate (v2.2: cancel/reschedule/confirm/NEW book; Phase 2
          in-chat slot picker loaded from Redis book:{conv_id} 30-min TTL BEFORE agent)
      → try_run_agent (if agent_config.enabled) OR:
          [PR #115 default path]
          → follow-up query condensation (multi-turn context collapse)
          → RAG (hybrid recall — migration 113 OR-semantics; vector_distance confidence)
          → active promotions fetch (promotions/lookup.fetch_active_promotions)
          → product search (products/search — match_products_hybrid, now in default path)
          → LLM generate (products + promos injected into prompt)
          → em-dash sanitizer on reply text (F9)
          → confidence-based gap logging (vector_distance → escalation.register_reply)
          → opt-in escalation (EscalationState Redis; offer after 2 unhelpful turns;
             consent branch: yes→handoff, no→reset, unclear→LLM)
      → reply DIRECT to Meta/Telegram (per-tenant token, saves provider_message_id)
      → mirror bot reply to the linked CW conversation via
         post_outbound_message(source_id=provider_message_id)  (dedup, no re-send/echo)
  → finally: _flush_deferred_cw  — conn-independent CW side-effects deferred OUT of the txn
      (bot-handling / human-handling labels, voice-note note) so slow CW HTTP no longer
      pins a pool connection across the transaction. Runs on every exit path.

Still inline (conn-bound, follow-up to defer): the handoff recovery hook + the rare
quota-escalation CW flip/label.

email: provider=='chatwoot' + email channel is branched at the router into email_assist_queue
  (the generic inbound queue dead-letters provider='chatwoot').
```

## Channel Auto-Provision (v1.0.19)

**2026-07-03:** the whatsapp/messenger/instagram_login → secret-kind mapping used by
provision/lazy_provision/reconcile/admin_tenants is now centralized in
`services/channels/secret_kinds.py` (was 4 hand-copied dicts); `telegram` deliberately
excluded (own connect flow).

```
admin: POST /api/admin/tenants/{id}/channels/{kind}
  → services/chatwoot/client.py::get_channel_token()
    (internal docker call: GET /internal/inboxes/:id/token)
  → auto-captures token into tenant_secrets
    (whatsapp→meta_waba_access_token,
     messenger→meta_fb_page_access_token, instagram_login→ig-login user token)
  → response.token_captured = true/false (best-effort)
  → then (post-txn) agent_bot.attach_platform_bot(inbox) so new convs start 'pending'
```

## Channel Remove (v1.0.22 — inverse of add)

```
admin: DELETE /api/admin/tenants/{id}/channels/{channel_kind}
  → services/channels/provision.py::deprovision_channel()
    - DELETE tenant_channels row (RETURNING id; 404 if none)
    - DELETE its tenant_secrets token kind, BUT only if no sibling
      channel still uses it (meta_fb_page_access_token shared IG+Messenger)
  → audit action='channel.removed'; impersonation-guarded
  → does NOT touch Chatwoot inbox or Meta/WABA webhook
```

## WhatsApp send (per-tenant, v1.0.21)

```
pipeline._dispatch_reply (channel='whatsapp')
  → _resolve_whatsapp_credentials(tenant): tenant_channels.phone_number_id
    + tenant_secrets.meta_waba_access_token  (returns (None,None) if either absent)
  → reply_dispatch.send_reply(whatsapp_phone_number_id=, whatsapp_access_token=)
  → MetaWhatsAppClient.send_text(phone_number_id=, access_token=)
  Fallback to META_DEMO_* env when per-tenant creds absent (demo-only was the GA gap).
```

## WhatsApp routing (canonical, Chatwoot-native rehaul)

```
INBOUND (customer messages): Meta WABA → Chatwoot inbox → agent-bot message_created
  → POST /v1/webhooks/chatwoot → resolve tenant by inbox_id → human-handling gate
  → pipeline → reply via per-tenant WABA token → mirror to the linked CW conversation.
  Chatwoot owns the WABA webhook subscription now (no override_callback_uri to Polaris).

LAZY auto-provision [LIVE on main — services/channels/lazy_provision.py, wired into
  webhooks/chatwoot_router.py:73 (import) / :590 (call)]: if
  resolve_chatwoot_with_status → None (no tenant_channels row), the router calls
  services/channels/lazy_provision.lazy_provision_channel(account_id, inbox_id) → LazyProvisionResult
  with outcome ∈ {provisioned, disallowed, no_tenant, not_applicable}:
  resolve tenant by payload.account.id → list_inboxes (CW service token) → map channel_type
  → kind → check allowed_channel_kinds → provision_channel(bypass_tier_cap=False) + token
  capture + agent-bot attach → re-resolve + process. Scope = {whatsapp,messenger,instagram_login}
  (telegram/email out — not_applicable). Disallowed kind → skip + record_event(status=tenant_not_found,
  error_class=ChannelNotEntitled:<kind>) + Redis neg-cache lazy_skip:{account_id}:{inbox_id} (TTL 600s)
  so repeat messages don't re-hit list_inboxes/re-alert; widening allowed_channel_kinds self-heals
  after the TTL. No new webhook status (CHECK-constrained) → reuse tenant_not_found + error_class.
  Idempotent: a concurrent first message hitting UniqueViolationError is treated as provisioned.
  Tests: apps/api/tests/test_lazy_provision.py (8 cases). See docs/SSOT/features/Channel Provisioning.md.

NON-MESSAGE WA (statuses / template-status / account / MM-Lite): Meta → Chatwoot
  Webhooks::WhatsappEventsJob → fork prepend module relays to Polaris →
  wa_status / meta_template_status / wa_account routers (logic unchanged).

reverse-handoff: a human agent's Chatwoot reply (outgoing + sender.type=='agent') is
  delivered to the customer by Chatwoot NATIVELY (phase-10: _dispatch_agent_forward DELETED —
  Polaris re-sending double-sent on Messenger/IG). Polaris only calls _record_email_agent_reply
  on EMAIL inboxes (email-draft acceptance tracking; non-email = no-op). The bot's own
  mirror-posts (sender.type=='agent_bot') are echo-dropped. pipeline.run still REJECTS
  provider='chatwoot'.
```

## Booking-Link-Fixes (v1.0.64)

```
Config (apps/api/polaris_api/config.py):
  ea_booking_public_url  env EA_BOOKING_PUBLIC_URL, default https://calendar.getpolaris.my
    — ALL customer-facing booking/reschedule links use this.
    — ea_api_base_url remains the INTERNAL EA API base (http://ea-nginx:80 in prod) for
      API calls ONLY; the two must never be confused.
    — infra/docker-compose.prod.yml sets EA_BOOKING_PUBLIC_URL from ${EA_PUBLIC_URL}.

Short-link redirect (routers/booking_redirect.py):
  GET /b/{code}  (public, no auth; allowlisted in scripts/lint_service_pool.py)
    code = jti = booking binding_token JWT id
    → service-pool SELECT bookings WHERE binding_token LIKE '%.<jti>'
    → 410 if not found / already expired / ea_service_id or ea_provider_id is NULL
    → re-mint signed token with REMAINING TTL (original exp - now)
    → 307 → build_ea_wizard_url(ea_booking_public_url, ea_service_id, ea_provider_id, new_token)
    Friendly 410 HTML on expired/unknown codes.

create_pending_booking (services/booking/link_flow.py):
  → now writes ea_service_id + ea_provider_id (from _resolve_provider_service) into bookings row
  → migration 117 adds these nullable cols to bookings

Reply label changes (services/booking/conversational.py):
  booking link: "*Book here:* {short_url}\n_This link is valid for 1 hour._"
  reschedule link: "*Reschedule here:* {short_url}\n_This link is valid for 1 hour._"
  short_url = {PUBLIC_API_BASE_URL}/b/{jti}  (via _short_booking_link helper)
```

## Booking-Confirmation Loop (v1.0.67)

```
New env:  POLARIS_API_BASE_URL  (Polaris API base; e.g. https://api.getpolaris.my)
          Set on ea-php-fpm container in infra/docker-compose.prod.yml.

Polaris receiver (was already present since PR #68, v2.1):
  POST /v1/webhooks/ea  (webhooks/ea_router.py)
    HMAC-SHA256 X-Polaris-Signature header over raw request body
    Secret: settings.ea_webhook_hmac_secret (env EA_WEBHOOK_HMAC_SECRET)
    Body: { polaris_ref, appointment_id, start_datetime, end_datetime, service_name }
    → verify_binding_token(polaris_ref) → link_flow.confirm_booking(...)
    → flips matching pending bookings row to confirmed; idempotent on re-delivery
    → 401 on HMAC fail; 400 on unknown/expired polaris_ref; 200 on success or already_confirmed

EA emitter (polaris-ea-fork r16→r18):
  Booking::index()  — captures ?polaris_ref= from URL query string, passes it to
                       the booking page as a PHP script_var (sanitized: drops malformed tokens)
  booking_http_client.js  — attaches polaris_ref to the /booking/register POST body
  Booking::register()  — on successful appointment insert, calls
                          notify_polaris_appointment_confirmed($appointment, $service, $provider, $polaris_ref)
                          only when $polaris_ref !== ''
  notify_polaris_appointment_confirmed()  — builds POST body
    {polaris_ref, appointment_id, start_datetime (provider-tz ISO8601 offset), end_datetime, service_name}
    signs with HMAC-SHA256 using polaris_api_base_url config key (env POLARIS_API_BASE_URL)
    fires best-effort (failure logged, never breaks booking completion)

Asset auto cache-bust (r16/r18 gotcha fix):
  infra/docker-bake.hcl  — ea target: args = { EA_ASSET_TOKEN = "${EA_TAG}" }
  EA Dockerfile           — ARG EA_ASSET_TOKEN + sed stamps app.php cache_busting_token
                            on every build; plain `docker build` with no arg = no-op (static fallback)
  Eliminates the stale-minified-JS risk: asset_url() always serves urls with the
  current image tag as the ?token query param.

TZ-localized chat messages (services/booking/conversational.py):
  _resolve_tenant_timezone(conn, tenant_id) → str | None
    reads tenants.settings.booking_config.tenant_timezone; fail-soft to None (UTC fallback)
  _format_slot(booking, tenant_timezone) → str
    delegates to _notify.format_slot(start_at, tenant_timezone)
  confirm/cancel/reschedule chat messages now show slot in tenant's local timezone.

Reschedule with _resolve_provider_service (services/booking/conversational.py):
  _do_reschedule now calls _resolve_provider_service(tenant_id, conversation_id)
    (branch_config first, EA-API fallback) before building the /b/{jti} short link.
  except widened to asyncpg.PostgresError (was narrower).
```
