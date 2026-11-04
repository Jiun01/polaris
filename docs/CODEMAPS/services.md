
# Services Codemap — `apps/api/polaris_api/services/`

**Last Updated:** 2026-07-02  
**Module Count:** 55+ services across 10 domains (booking/ subpackage grew from 12 to 19 files this pass; pipeline split into 4 files)  
**Latest (2026-07-02 audit-remediation, Batches A–G):** Reliability fixes (new `reconcile_channels.py` hourly self-heal job; `llm.generate_with_tools` exception taxonomy + OpenRouter fallback; `rag.py` Cohere network-degrade distinct from the existing disabled-key degrade); `flow_runtime/` backend fully deleted (Batch D, was already portal-UI-removed); pipeline hot-path work (tenant snapshot fetched once, merged Haiku call, quota Redis-cache, new `http_client.py` singleton, service pool 4→6); structural splits preserving behavior (`pipeline.py`, `booking/conversational.py`, `admin_tenants.py`); `admin_ea.py` sync endpoint deleted (dead code).
**Prior (PR #115, v1.0.39):** RAG hybrid recall fixed (migration 113 OR-semantics); `vector_distance` confidence column; Cohere no-crash degrade; products + active promotions injected in the default reply path; opt-in human escalation (`escalation.py` NEW); confidence-based knowledge-gap capture; STOP opt-out hardened; em-dash sanitizer. `infra/ship.sh` is now the canonical release pipeline (replaces manual `infra/build.sh --push`). `config.py` blank-bool coercion prevents LLM_STUB_ENABLED/COST_STUB_ENABLED/TRANSCRIPTION_STUB_ENABLED crashes on blank `.env` values.

## Core Pipeline

### `pipeline.py` — Inbound request dispatcher (Chatwoot-native, rehaul PR #102)

**Audit Batch E structural split (2026-07-02):** 2965→2437 lines. Three modules carved out
as pure extractions (no behavior change): `services/pipeline_voice.py` (voice-note
download→transcribe→substitute branch), `services/pipeline_quota_escalation.py`
(quota-exhausted CW flip/label escalation), `services/pipeline_cw_mirror.py` (Chatwoot
reply-mirroring + product-image agent-note posting). **Audit Batch B (2026-07-02):** the
tenant snapshot (timezone/persona/agent_config/persona_blocks) is now read ONCE per turn
via `tenant_snapshot_svc.load_tenant_snapshot` and threaded through the call chain —
previously re-fetched at multiple points in the same turn. `classify_and_condense` merges
the intent-classify Haiku call and the follow-up-query-condensation Haiku call into one
round-trip (previously two separate calls per turn).

**Entry:** `async def run(*, incoming: IncomingMessage, deps: PipelineDeps) → PipelineDecision`
— a **thin wrapper** over `_run_inner`. Collects conn-independent best-effort Chatwoot
side-effects into `deferred_cw` during the transaction and flushes them in a `finally`
(`_flush_deferred_cw`) AFTER `_run_inner`'s txn commits + its connection is released — so slow
CW HTTP no longer pins a pool connection across the txn. Flush runs on every exit path
(booking/agent/flow/quota short-circuits included).

**`_run_inner` steps (one DB transaction):**
```
reject provider=='chatwoot' (ValueError — a Chatwoot inbound is a human-agent reply, Phase 7)
0.5: _link_cw_conversation — LINK the existing CW conversation via incoming.chatwoot_*
     (idempotent UPDATE onto conversations; NEVER create — fixed the dup-conversation bug)
1.0: Extract metadata + resolve channel/tenant
1.4: Upsert audience member (first inbound → opt_in_source='inbound')
1.5: STOP keyword check → opt-out
1.6: Hard quota enforcement (5k/25k/60k cap) — graceful escalation to CW human on exhaustion
1.7: Booking command check (CANCEL/RESCHEDULE/CONFIRM; conversational)
2.0: try_run_agent (if agent_config.enabled) OR classify→rag→generate
6.0: Dispatch reply DIRECT to channel (WA/MSG/TG/IG-Login) — per-tenant token
7.0: Save messages + update last_message_at
8.0–11.0: send / record outbound / conversation metadata / audit (hash chain)
12a: mirror bot reply to the LINKED CW conversation (source_id; dedup)  [deferred: bot-handling label]
13.0: ACK
```

**Quota increment (commit 28a553f, 2026-06-20; flow call site retired 2026-07-02 — audit Batch D):** `_increment_bot_reply_quota` helper called at both reply sites (agent/classic) to count bot replies uniformly. Previously flow + agent paths returned early before the quota increment, causing a billing undercount (unlimited free replies for flow/agent-mode tenants). Shared helper preserves ADR-066 brand-demo exemption and uses best-effort Redis semantics.

**Deferred CW (post-txn `finally`):** bot-handling / human-handling ownership labels +
voice-note transcript note. **Still inline (conn-bound, follow-up):** handoff recovery hook +
rare quota-escalation CW flip/label.

**Branches:**
- **Media-only** → handoff ("can't read files, please rephrase")
- **Voice note** (M1) → download → transcribe → substitute user_text → continue (transcript note deferred)
- **Flow runtime** → dedicated flow node executor
- **Booking command** → `_try_booking_command` (v2.2): cancel / reschedule / confirm / **new-booking** (Phase 1 link + Phase 2 in-chat picker); loads/saves `BookingPickerState` from Redis (`book:` prefix, 30-min TTL) unconditionally before/after the command
- **Handoff** → route to Chatwoot team + `human-handling` label
- **Bot-sent product image** → agent note posted to CW (so the human sees what the bot sent)

**Config keys:**
- `STOP_ENABLED`, `STOP_KEYWORD_VARIANTS` (EN/BM/ZH)
- `TRANSCRIPTION_ENABLED`, `TRANSCRIPTION_STUB_ENABLED` (voice M1)
- `KNOWLEDGE_GAP_CAPTURE_ENABLED` (KGD M1)

---

## Intent & LLM

### `llm.py` — Intent classify + response generate

**Audit Batch A (2026-07-02):** `generate_with_tools` gained a proper exception taxonomy
(distinct handling for rate-limit / auth / timeout / malformed-response Anthropic failure
classes, was a bare catch-all) + falls back to OpenRouter on Anthropic failure (mirrors the
existing `generate_response` provider fallback chain, previously the tool-calling path had
none). **Audit Batch E (2026-07-02):** the booking-specific NLU helpers previously living in
this file were moved OUT to `booking/nlu.py` (structural split, no behavior change) —
`extract_booking_datetime`, `extract_customer_name`, `classify_booking_intent` below now
live there, not in `llm.py`; `llm.py` retains the general-purpose `classify_intent` /
`generate_response` / `generate_followup` / `generate_with_tools`.

**Functions:**
- `classify_intent(text, *, tenant_id) → IntentTier` — Haiku (`llm.py:373`). Returns one of the `IntentTier` Literal values `simple | medium | hard | handoff` (frozenset-validated via `get_args(IntentTier)`, `llm.py:88`; unparseable response defaults to `"medium"`, `llm.py:420`). Earlier codemap revisions described a `question|general_support|booking_action|handoff_trigger` enum — that enum does not exist in the current code.
- `generate_response(question, rag_context, ...) → LLMGenerateResult` — Sonnet; structured output with `product_skus` field (Phase 15)
- `generate_followup(question, context) → str` — Haiku; follow-up message (Phase 13)

**Moved to `booking/nlu.py` (audit Batch E, 2026-07-02 — same signatures, new home):**
- `extract_booking_datetime(message, *, today_iso, timezone) → tuple[str|None, str|None]` — v1.0.70 — Haiku; extracts `(date, HH:MM)` from a booking message; each field validated independently; replaced the old `extract_booking_date`
- `extract_customer_name(message) → str | None` — v1.0.70 — Haiku; pulls a name for the in-chat details capture (ignores email/phone)
- `classify_booking_intent(message) → str | None` — Haiku booking-intent fallback; v1.0.72 extended to map availability questions / a bare date → `"book"`; v1.0.75 prompt explicitly names Malay and Chinese bare-date examples ("5 Ogos", "esok", "isnin depan", "7月1日", "下星期一", "明天有空吗") to ensure terse Malay/ZH openers return `"book"` not `None`. Null guard for mentioned-but-not-booking dates unchanged.
  - (`resolve_booking_slot` REMOVED v1.0.71 — superseded by deterministic `_match_time` + `extract_booking_datetime`)

**2026-07-02/03 (simplify pass):** `_model_for_intent` (private helper backing `generate_response`'s hard-tier Sonnet call, `llm.py:973`) now returns a 3-tuple `(model, model_used, thinking_param)` — folds the `thinking={"type":"disabled"}` selection that `_generate_sonnet_streaming` used to compute inline via a duplicated `intent=="hard"` check at 2 call sites; `thinking_param` is `_THINKING_DISABLED` for hard tier, `None` otherwise. `booking/nlu.py`'s 3 call sites were updated for the new arity (mypy-caught after the signature change); 2 test mocks in `test_conversational.py` updated to match.

**Providers:** Anthropic (primary), OpenRouter (fallback), Cohere (fallback)  
**Stub mode:** `LLM_STUB_ENABLED` → canned responses (E2E testing)

**Key params:**
- `llm_model_intent`, `llm_model_generate`, `llm_model_followup` (configurable)
- `llm_response_max_tokens` (512 default)
- `llm_stub_enabled` (E2E only)

---

## Knowledge & RAG

### `rag.py` — Hybrid search + rerank (updated PR #115 + commit 28a553f)

**Functions:**
- `search_hybrid(...) → list[KnowledgeChunk]` — `rag.py:156`. Embedding search via `match_knowledge_chunks_hybrid` (migration 113: OR-semantics BM25 + `vector_distance` confidence column) + Cohere rerank (Ultra+) or skip (Starter/Pro), with the rerank-empty fallback below. A second `search_hybrid` lives in `knowledge_search.py:84` (thinner query+rank wrapper used by other call sites). Earlier codemap revisions named `search_knowledge()` / `rerank_chunks()` — **neither function exists** in the current code; both were renamed/consolidated into `search_hybrid`.

**PR #115 change — Cohere no-crash degrade:** `CohereDisabledError` (raised when `COHERE_API_KEY` is blank — the prod default) was previously uncaught and would crash the RAG reply for Ultra/Enterprise tenants. Now caught and degrades to vector-only result (logged as `rag.cohere_disabled_degrade`; no reply failure).

**Audit Batch A change (2026-07-02) — Cohere network degrade:** a SEPARATE catch for `CohereApiError` / `httpx.TransportError` (real network failure — timeout, 5xx, connection reset — as opposed to the disabled-key config state above) now degrades identically to vector-only (logged as `rag.rerank_network_degrade`; `metrics.record_cohere_rerank_degrade` counter). Previously an Ultra/Enterprise tenant with `COHERE_API_KEY` set but Cohere transiently unreachable would crash the reply — this closes that gap alongside the pre-existing disabled-key degrade.

**Commit 28a553f change — Rerank-empty fallback:** when Cohere rerank drops every candidate below the relevance threshold, `search_hybrid` now returns the un-reranked hybrid top_k instead of an empty list. Previously an Ultra/Enterprise tenant would get a context-free reply on any query whose candidates all ranked low (e.g. cross-language questions). Mirrors the CohereDisabledError degrade behavior above.

**Config:**
- `COHERE_API_KEY` (placeholder-fail-closed; blank → graceful degrade to vector-only, PR #115)
- `knowledge_rerank_enabled` (Ultra+; default True per ADR-026 Am 5th)
- `knowledge_rerank_threshold` (0.0–1.0, default 0.3)

---

### `rag_eval.py` — NEW (2026-07-02/03) — offline RAG scoring (DB-free, pure functions)

**Functions:** `hit_rank`, `recall_at_k`, `mrr`, `summarize` — pure scoring math consumed by
`apps/api/scripts/rag_eval.py` (offline CLI, always exits 0 — a report, not a CI gate).
Scores the real `services.rag.search_hybrid` retrieval path against a hand-curated golden
Q&A set (`apps/api/tests/eval/golden.jsonl`, 36 rows: 26 en/5 ms/5 zh). No baseline has been
run against real/prod data yet — recall@k/MRR numbers for the actual KB are unknown.

**2026-07-02 (simplify pass):** dropped an unused `_ChunkLike` Protocol type (structural
duck-typing shape, no import-time dependency was ever needed once `getattr`-based reads
were in place).

---

### `knowledge_ingestion.py` — File upload + chunking

**Functions:**
- `create_ingestion_request(file_path) → request_id` — Queue for chunking
- `chunk_document(doc_id, tenant_id) → chunk_count` — Semantic chunking + embedding
- `get_capacity(tenant_id) → (used_mb, cap_mb)` — Quota check

**Storage:** Supabase (`polaris-knowledge` bucket)

---

### `knowledge_search.py` — Query + rank

**Functions:**
- `embed_query(text) → embedding` — Text → 384-dim vector
- `search_db(embedding, tenant_id) → chunks` — pgvector search + RLS
- `rank_results(question, chunks) → ranked` — Cohere RRF

---

### `knowledge_gaps/capture.py` — (✅ main, migration 098; updated PR #115)

**Signals captured:**
- `no_retrieval` — zero RAG chunks returned
- `handoff` — real question → human escalation
- `low_retrieval` — `vector_distance` above threshold (default 0.0 = off until tuned)

**Functions:**
- `classify_score_miss(top_score, threshold) → (is_miss, signal_type, top_score)` — Core miss decision. Returns `(True, "no_retrieval", None)` when top_score is None; `(True, "low_retrieval", score)` when score strictly below threshold (threshold > 0); else `(False, None, score)`. **PR #115:** now consumes `vector_distance` from migration 113's hybrid RPC output as the confidence signal.
- `parse_stashed_score(raw) → float | None` — Fail-soft float parse of the flow-stashed `last_rag_top_score` variable (returns None on non-finite/non-numeric; guards the reply critical path).
- `record_miss(conn, tenant_id, question, signals) → None` — SAVEPOINT fail-soft; never poisons outer tx
- `tenant_in_capture_tier(plan_tier) → bool` — Ultra+ only

**Thread sites:** Handoff branch, Phase-4 post-generate, flow runtime (mutually exclusive)
**Feeds:** `escalation.register_reply(is_unhelpful=...)` in pipeline (PR #115)

---

### `knowledge_gaps/aggregate.py` — (✅ main)

**Functions:**
- `summarize_gaps(conn, tenant_id, days=30) → KnowledgeGapsSummary` — M2-lite SQL grouping (lowercase/strip-punct/collapse-ws; no embeddings); top-50 themes

---

### `escalation.py` — NEW (PR #115, v1.0.39) — opt-in human escalation

Pure state machine (no DB/LLM imports — fully unit-testable without infrastructure).

**`EscalationState`** — dataclass persisted as JSON in Redis (keyed by conversation):
- `unhelpful_streak: int` — consecutive turns where the bot had low/no retrieval confidence
- `awaiting_consent: bool` — True after the offer has been made, waiting for customer reply

**Functions:**
- `register_reply(state, *, is_unhelpful) → (new_state, should_offer)` — Folds one turn's outcome into the state. `should_offer=True` exactly when the streak reaches `_OFFER_AFTER_TRIES` (2) and we aren't already awaiting consent — caller appends offer text. A helpful reply resets the streak entirely.
- `interpret_consent(message) → "yes" | "no" | "unclear"` — Multilingual heuristic read of a yes/no reply to the connect offer. Exact-match set + whole-word scan across EN/BM/ZH (`boleh`, `好`, `tak`, `不用`, etc.). Returns `"unclear"` for anything ambiguous so the pipeline can fall back to an LLM read — never escalates without a clear yes.
- `offer_text(language) / accept_text(language)` — Canned EN/MS/ZH reply strings (no em-dashes per F9).

**Config constant:** `_OFFER_AFTER_TRIES = 2` (operator-decided 2026-06-14).

**Pipeline wiring:** `pipeline.py` reads `EscalationState` from Redis, calls `register_reply` after each LLM turn, persists state back. On `awaiting_consent=True` inbound: `interpret_consent` → yes → handoff (Chatwoot) + `accept_text`; no → reset; unclear → LLM consent read.

---

### `promotions/lookup.py` — NEW (PR #115, v1.0.39) — active promotions in default reply path

**Functions:**
- `fetch_active_promotions(conn, tenant_id, *, tenant_branch_id=None) → list[ActivePromotion]` — Queries `tenant_promotions` (migration 053) for `is_active AND now() BETWEEN valid_from AND valid_until`. Branch-scoped: `NULL` tenant_branch_id on the row = shared across branches. Capped at 5 most-recently-updated promos. Caller is the pipeline (anon pool, RLS, `app.current_tenant_id` GUC set).

**`ActivePromotion`** dataclass: `id, title, description, discount_text, terms`.

**Before PR #115:** promotions were accessible only if an operator manually copied them into a knowledge chunk. Now fetched automatically on every default LLM reply and injected into the prompt context.

---

## Reply & Dispatch

### `reply_dispatch.py` — Multi-channel send

**Functions:**
- `send_reply(channel, phone_id, body, ...) → None` — Route to WA/IG/MSG/TG/text

**Channel-specific** (resolved from `_BOT_PROVIDERS`):
- **WhatsApp:** `meta_whatsapp.send_text|image|template` (per-tenant WABA token)
- **Messenger:** `meta_messaging` (Page token, `/me/messages`)
- **instagram_login:** `InstagramLoginClient` (IG-Login user token, `graph.instagram.com`) — legacy FB-page `instagram` dropped
- **Telegram:** `telegram_client.send_message|send_photo`
- **Mirror:** bot reply posted to the linked Chatwoot conversation with `source_id` (not a customer send)

---

### `meta_whatsapp.py` — Meta Graph API client

**Audit Batch B (2026-07-02):** send calls now prefer `http_client.get_http_client_or_none()`
(the lifespan-shared singleton) over opening a fresh `httpx.AsyncClient` per call, falling
back to the old per-call client when the singleton isn't initialised (unit tests/scripts).

**Functions:**
- `send_text(token, phone_id, channel_user_id, body) → message_id`
- `send_image(token, phone_id, channel_user_id, image_url) → message_id`
- `send_template(token, phone_id, channel_user_id, template_name, lang, body_params) → message_id`
- `submit_template(waba_id, name, lang, category, components, token) → meta_template_id` — custom authoring → PENDING → review webhook
- `create_from_library(waba_id, name, lang, category, library_template_name, token) → (id, status)` — Meta Template Library (instant-approved UTILITY/AUTH; `library_template_name`, no components; no status webhook). Backend primitive; portal adoption deferred.
- `delete_template(waba_id, name, token)` — idempotent on 404

**Config:**
- `META_API_BASE_URL` (default https://graph.facebook.com; E2E mock override via config)
- `META_MOCK_ENABLED` (Phase 20 E2E only)

**Rate-limit header tracking:** `meta_usage.py` records X-Business-Use-Case-Usage per send

---

### `meta_messaging.py` — NEW (Phase 15) — IG/Messenger API

**Functions:**
- `send_text_ig(page_token, psid, text) → message_id` — Page token via `/me/messages`
- `send_image_ig(page_token, psid, image_url) → message_id` — Image + text

---

### `telegram_client.py` — NEW (v2.1, off-Meta M1/M2)

**Functions:**
- `send_message(token, chat_id, text) → message_id` — Telegram Bot API
- `send_photo(token, chat_id, image_url, caption) → message_id`
- `set_webhook(token, url, secret) → None` — Webhook registration
- `get_me(token) → BotInfo` — Verify bot token

**Config:**
- `TELEGRAM_API_BASE_URL` (default https://api.telegram.org)
- Per-bot webhook secret stored in `tenant_channels.telegram_webhook_secret`

---

## Media & Attachments

### `media_download.py` — (✅ main, voice-notes M1)

**Audit Batch B (2026-07-02):** fetches now prefer the shared `http_client` singleton
(same adoption pattern as `meta_whatsapp.py`; see backend.md §Key Services for the
singleton's shape).

**Pure HTTP (no DB):**
- `fetch_audio_bytes(provider: str, attachment, access_token) → (bytes, mime_type)`

**Per-channel auth:**
- **WhatsApp:** Graph 2-step (GET media_id → signed URL; both with WABA Bearer)
- **Telegram:** `getFile` → `GET /file/bot{token}/{path}`
- **IG/Messenger:** Direct GET with access_token
- **Chatwoot:** Direct GET with auth header

**Error handling:** `MediaDownloadError`; tokens never logged

**Config:**
- `transcription_max_download_bytes` (50 MB default)

---

### `transcription.py` — (✅ main, voice-notes M1)

**Pure HTTP (no Groq SDK):**
- `transcribe(audio_bytes: bytes, mime_type: str) → TranscriptionResult(text, language)`

**Provider:** Groq Whisper (`whisper-large-v3`)  
**Auto-detect:** Language omitted in request; response carries detected language (EN/MS/ZH)  
**Stub mode:** `TRANSCRIPTION_STUB_ENABLED` → "hello from stub"

**Config:**
- `GROQ_API_KEY` (placeholder-fail-closed)
- `transcription_model`, `transcription_timeout_s` (30 default)
- `transcription_stub_enabled` (E2E only)

---

## Flows & Templates — RETIRED (audit Batch D, 2026-07-02)

`flow_runtime/` (the Phase 5 5-node flow executor, its two routers
`tenant_flows.py` / `tenant_flow_templates.py`, the operator scripts
`seed_demo_flow.py` / `build_template_seed.py`, and their `_template_defs/`
JSON bundle) has been **deleted outright**. Prod had zero published flows and
zero flow_runs ever recorded at retirement time; the portal flow-builder UI
was already removed under ADR-063 well before this. `pipeline.py`'s branch
order is now `try_run_agent OR classify→rag→generate` — no flow branch.

The handful of pieces flow_runtime hosted that were actually load-bearing
for the surviving agent / classic / email / follow-up / handoff paths were
relocated, not deleted, ahead of the engine removal:

- `Ports` container + `LLMPort`/`RAGPort`/`ProductsPort`/`ChatwootPort`/
  `ConversationPort` + their adapters + `build_ports()` → now live in
  `services/agent/ports.py` (the agent tool loop is their sole consumer).
  The flow-only `MetaPort`/`DBPort`/`AuditPort` ports and their adapters
  (`MetaAdapter`, `NullMetaAdapter`, `DBAdapter`, `AuditAdapter`) were
  deleted, not moved — zero agent-tool consumers of `ports.meta`/`.db`/
  `.audit`.
- `load_tenant_snapshot` (tenant timezone/persona/agent_config/persona_blocks
  reader) → now `services/tenant_snapshot.py`. Same 4 non-flow callers as
  before: `pipeline.py`, `agent/pipeline_integration.py`, `email_assist.py`,
  `followups/worker.py`. **2026-07-03 (simplify pass):** the "no such tenant" and
  populated-row return branches' default-value construction was deduped into one
  module-level `_EMPTY_SNAPSHOT` dict, spread (`{**_EMPTY_SNAPSHOT, ...}`) at each
  return site instead of hand-repeating all 10 keys twice.
- `business_hours.is_within_schedule` (ADR-019 helper) → now
  `services/chatwoot/business_hours.py`.
- The `{{varname}}` `substitute()` helper → now
  `services/chatwoot/templates.py`. (The starter-template JSON loader that
  used to share this module — `get_flow_definition`/`list_bundled_templates`
  — was flow-only and deleted with the engine.)

DB tables (`flow_definitions`, `flow_versions`, `flow_runs`,
`flow_templates`, migration 036 grants/RLS) were left untouched — no
migration in this batch; a follow-up DROP-table migration is a separate,
manually-applied unit once the code change has burned in.

---

## Prompt Assembly (`_llm_prompts.py` — PR #99 + v1.0.55)

Layered prompt assembly used by all reply paths:
- `build_response_system_prompt` (classic chat path)
- `build_email_draft_prompt` (email-assist path)
- `build_agent_system_prompt` (agent tool-loop path)
- Non-overridable `_SAFETY_GUARDS` block; `[HANDOFF_TRIGGER]` deleted (PR #99)

**v1.0.55 additions — persona blocks:**
- `DEFAULT_PERSONA_BLOCKS` — structured form of `_V1_TEMPLATE`; byte-identical output, guarded by regression test in `test_persona_blocks.py`
- `default_persona_blocks()` — returns DEFAULT_PERSONA_BLOCKS with tenant name/company substituted (used by GET `/api/tenant/settings` for prefill)
- `render_persona_blocks(blocks) → str` — serializes a list of `PersonaBlock` to the XML persona body; neutralizes `</` in block bodies as defense-in-depth (mirrors `format_rag_context`)
- `coerce_persona_blocks(raw) → list[PersonaBlock]` — fail-soft parse of a runtime snapshot blob (returns `DEFAULT_PERSONA_BLOCKS` on any parse error)

**`_base_persona` precedence (v1.0.55):** `persona_blocks` > legacy `system_prompt` JSONB override > `_V1_TEMPLATE` default

**`TenantPersona`** gained a `persona_blocks` field. All 4 construction sites now pass it: `pipeline.py` (classic), `agent/pipeline_integration.py` (agent), `email_assist.py`, `followups/worker.py`. `services/tenant_snapshot.py::load_tenant_snapshot` surfaces `persona_blocks` from `settings` JSONB.

---

## Agent Tool Surface (`agent/` — PR #99, ADR-063, ✅ MERGED main)

**2026-07-03 (simplify pass, `services/agent/ports.py`):** the embed-memoization block
duplicated identically in `RAGAdapter.search` and `ProductsAdapter.search_products` was
extracted to a private `_embed_cached(query, tenant_id, cache)` helper (`ports.py:223`);
the `Ports` class's 5 wired-or-raise properties (`llm`/`rag`/`products`/`chatwoot`/`conversation`)
were deduped via a private static `_require(value, name)` helper (`ports.py:189`, identical
exception type/message per port, was hand-repeated 5×); dead `IncomingMessage` re-export
dropped (zero external importers).

### `agent/pipeline_integration.py` — `try_run_agent`

Called by `pipeline.run` after STOP/quota/booking, **before** classic (precedence). Returns `None` when `tenants.settings.agent_config.enabled` is false → pipeline unchanged. Reads `agent_config` + `persona_blocks` from `load_tenant_snapshot` (`services/tenant_snapshot.py`, one hot-path read), builds the system prompt via `build_agent_system_prompt` (passing `persona_blocks`), reuses `agent/ports.py`'s `build_ports`, runs the loop, returns an `AgentReplyResult` (envelope + tools_invoked). **v1.0.55:** `TenantPersona` carries `persona_blocks`; all 4 construction sites (pipeline.py classic, agent/pipeline_integration.py, email_assist.py, followups/worker.py) pass `persona_blocks`. **Audit Batch D (2026-07-02):** also computes an `IntentTier` via `llm.classify_intent` (same Haiku classifier the classic path calls) and threads it through `run_agent_reply(tier=...)` → `generate_with_tools(model_tier=...)` — ADR-031 model-tier routing (Haiku vs Sonnet), previously hardcoded to Haiku for every agent-enabled tenant regardless of message complexity.

### `agent/loop.py` — `run_agent_reply`

Anthropic tool-use loop: model emits `tool_use` → execute registered tools → feed `tool_result` → repeat until text reply or `max_tool_iterations` (hard cap; final no-tools call on cap, both calls now honor `model_tier`). Disabled tools are absent from the API call (hard guardrail). `envelope.model_used` is still hardcoded `"haiku"` (telemetry field, out of scope for the D4 tier-routing fix — only the actual Anthropic `model=` kwarg changed).

### `agent/tools/` — `@register_tool` registry

- `search_knowledge` → `RAGAdapter.search`
- `lookup_products` → `ProductsAdapter`
- `handoff_to_human` → `ChatwootAdapter.handoff` (sentinel-free; replaces `[HANDOFF_TRIGGER]`)
- `capture_lead` → `audience.upsert_audience_member`
- `tools_for(enabled, tier)` = registry ∩ enabled ∩ tier; `config.parse_agent_config` (fail-soft)

### `llm.generate_with_tools` (in `llm.py`)

First `messages.create(tools=…, tool_choice=…)` call in the codebase. Haiku tier by default (`model_tier="simple"`); `max_tokens=2048`; honors `llm_stub_enabled`. Returns a normalized `ToolTurn`.

**Deferred tools:** booking, send_template, buttons/carousel, call_api/MCP (registry makes each a one-file add).

**2026-07-03 (currently UNCOMMITTED in the working tree — verify commit status before treating as shipped fact):** now unpacks `_model_for_intent`'s 3rd tuple element (`thinking_param`) and passes it through as `kwargs["thinking"]` when non-`None` (hard tier only) — previously discarded via `model, _, _ = _model_for_intent(...)`, so Sonnet 5's adaptive-thinking-ON default was NOT pinned off on the agent tool-loop path (only the classic `generate_response` hard-tier path had it pinned). Same `_THINKING_DISABLED` pin as `_generate_sonnet_streaming` below.

---

## Bookings & Reminders

### `booking/binding_token.py` — HMAC mint/verify (v2.1)

**Functions:**
- `mint_token(customer_handle, secret, ttl_seconds) → polaris_ref_token`
- `verify_token(token, secret, expected_handle) → verified_handle | None`

**Secret:** Placeholder-prefixed default; fail-closed if missing

---

### `booking/link_flow.py` — Link-mode booking lifecycle

**Functions:**
- `create_pending_booking(tenant_id, conversation_id, customer_handle, ea_service_id=None, ea_provider_id=None) → booking` — v1.0.64: now stores `ea_service_id`/`ea_provider_id` (nullable, migration 117) so the short-link redirect can re-mint the EA wizard URL without a DB round-trip to EA
- `confirm_booking(...) → confirmed_booking` — on confirm (first delivery only) ALSO **schedules the reminder** (`_schedule_reminder_for_confirmed`) + **sends the immediate confirmation** (`_send_confirmation_for_confirmed`); both best-effort (never fail the EA webhook). Single choke point: covers initial bookings AND conversational reschedules (re-book → confirm).
- `sweep_expired_pending(pool) → count`

**Note:** pre-feature, `schedule_reminder` was only called from the internal-E2E endpoint, so reminders never fired in prod; confirmation_message was orphaned (read by nothing). Both now fire here.

---

### `booking/config.py` — NEW — booking_config reader

- `read_booking_config(conn, tenant_id) → dict` — soft-fail read of `tenants.settings.booking_config` JSONB (`{}` on missing/malformed). Shared by confirm path + internal-E2E seeder.

---

### `booking/_notify.py` — NEW — shared notification send + tz formatting

- `format_slot(start_at, tz) → str` / `slot_date_time(start_at, tz) → (date, time)` — tenant-timezone formatting (UTC fallback; cross-platform, no `%-d`)
- `render_named(text, vars) → str` — `{{name}}` substitution (unknown → empty). **2026-07-03 (simplify pass):** now delegates to the shared `chatwoot/templates.py::substitute()` helper (the one Batch D relocated from `flow_runtime`) instead of a second hand-rolled `{{var}}` implementation — same substitution semantics, one fewer duplicate implementation.
- `send_appointment_message(meta_client, *, template_name|None, ...) → wamid` — template if name set (delivers anytime) else free-form text (24h window). Used by confirm + reminder.

---

### `booking/client.py` — EA API client helpers (v1.0.64)

**Audit Batch B (2026-07-02):** EA API calls now prefer the shared `http_client` singleton
(same adoption pattern as `meta_whatsapp.py`).

**Functions:**
- `build_ea_wizard_url(public_url: str, provider_id: int, service_id: int, token: str) → str` — NEW (v1.0.64) — shared helper that constructs the full EA wizard URL from a public base URL + ids + signed token. Used by both `conversational.py` and `booking_redirect.py` to ensure a single canonical URL shape.

---

### `booking/branch_config.py` — Branch EA provider/service resolution

**Functions:**
- `get_branch_ea_config(conn, tenant_id, conversation_id) → (provider_id, service_id, binding_secret)`

---

### `booking/cancel_reschedule.py` — Customer booking actions (v2.1)

**Functions:**
- `find_customer_bookings(conn, tenant_id, channel_user_id) → [Booking]`
- `cancel_booking(conn, ...) → None` — Identity-isolated (BookingNotOwned if no match)
- `reschedule_booking(conn, ..., link_mode=True) → booking_url`

---

### `booking/branch_bindings.py` — NEW name (audit Batch E, 2026-07-02) — was `booking/provisioning.py`

**Functions:**
- `sync_tenant_branch_ea_bindings(...)` — reconciles branch↔EA-provider bindings at
  provisioning time. Renamed (not just moved) to clarify the distinction from the
  pre-existing `booking/branch_config.py` (`get_branch_ea_config` — reads a single
  branch's resolved provider/service for booking flows). `admin_ea.py`'s manual
  `POST .../ea-providers/sync` trigger was removed 2026-07-02 (dead code) because this
  reconciliation already runs automatically — see backend.md §Admin Routes.

---

### `booking/conversational.py` — Conversational booking handler (v2.2, v1.0.64–v1.0.75)

**Audit Batch E structural split (2026-07-02):** 2564→367 lines. This module is now a thin
FAÇADE — `handle_booking_command` is the only function still defined here; everything else
below is re-exported (`from polaris_api.services.booking.X import ...`) from four new
sibling modules, so existing `from booking.conversational import <name>` call sites are
unaffected:
- `booking/intent_detect.py` — `detect_booking_command`, `_looks_date_ish`, `_DATE_ISH_RE`/`_DATE_ISH_CJK_RE`, the bail/nav phrase frozensets, `_detect_booking_language`
- `booking/formatting.py` — reply-string renderers (`_render_range_offer`, `_format_date_label`, etc.)
- `booking/cancel_reschedule_flow.py` — `_do_reschedule`, cancel/confirm reply builders
- `booking/picker_statemachine.py` — the full picker state machine (`_handle_picker_turn`, `_picker_awaiting_*`, `_do_inchat_book`, `_do_new_booking`, `_do_inchat_offer`, `_mint_booking_link`, `_resolve_provider_service`)

Pure extraction — no behavior change; function names/signatures below are unchanged, only
their home file moved.

**2026-07-03 (simplify pass):** dropped a dead `llm_service` re-export (`from polaris_api.services import llm as llm_service`, zero importers) from the module and its `__all__`.

**Functions:**
- `detect_booking_command(text: str) → "cancel" | "reschedule" | "confirm" | "book" | None` — pure string match (no DB/LLM); order: reschedule phrases → cancel phrases → NEW booking phrases → bare tokens
- `handle_booking_command(conn, tenant_id, ..., picker_state) → BookingReply | None` — routes: Phase 2 picker intercept first (if `picker_state` non-None) → detect command → `"book"` NEW branch BEFORE existing-booking gate → find_customer_bookings gate for cancel/reschedule/confirm
- `_resolve_provider_service(tenant_id, conversation_id) → (provider_id, service_id)` — branch_config first, then EA list_services/list_providers fallback; fail-soft `(None, None)`
- `_mint_booking_link(conn, ...) → BookingReply` — shared by `_do_new_booking` + inchat-no-availability fallback; emits short link `{public_api_base_url}/b/{jti}` via `_short_booking_link` helper; returns `kind="book_link"` with label `*Book here:*` + `_This link is valid for 1 hour._` (v1.0.64)
- `_short_booking_link(jti) → str` — NEW (v1.0.64) — builds `{PUBLIC_API_BASE_URL}/b/{jti}` for use as the customer-facing link
- `_do_new_booking(conn, ..., prefetch_date_text, inchat_picker) → BookingReply` — new-booking entry: resolves provider/service + `inchat_picker`; Phase 2 → `_do_inchat_offer` (or, when `prefetch_date_text` is set, jumps straight to that date's ranges via `_picker_awaiting_date`); else link mode
- `_do_inchat_offer(conn, ...) → BookingReply` — v1.0.70 date-FIRST offer: asks "what date and time works?" (+ short link), state `awaiting_date` (no slots listed up front); reply text localized via `t("offer_date_prompt", lang)` + `t("offer_link_tail", lang)` (v1.0.74)
- **Conversational picker state machine** (v1.0.70–75) — `_handle_picker_turn` routes by step (centralized bail check first); all reply strings route through `t(key, state.language)` (v1.0.74):
  - `_picker_awaiting_date` → parse date(+time) via `extract_booking_datetime`; show open RANGES (`_compress_ranges`/`_render_range_offer`) or, with a time, `_match_time` (exact/nearest) → details; empty/closed date → nearest open day → details (`_empty_date_reply`)
  - `_picker_awaiting_pick` → match a stated time against the FULL day's slots; "earliest"/"another date" handled
  - `_picker_awaiting_details` → collect Name+Email(req)+Phone (regex email/phone + Haiku `extract_customer_name`; Chatwoot prefill via `get_contact_details`); "another date"/bare date re-routes; attempt-cap → link
  - `_picker_awaiting_confirm` → ONE final summary; free-flow yes (`interpret_consent`); an edit (`_apply_edit`) re-resolves + re-summarizes
  - `_do_inchat_book` → EA `create_appointment` with REAL `customer_name/email/phone`; v1.0.73: `last_name` defaults to `"Unknown"` so single-token and all-Chinese names are no longer rejected by EA; inserts confirmed `bookings` row via RLS conn (`_resolve_service_duration_minutes` sizes it)
- **Pre-agent availability routing** (v1.0.72) — `_looks_date_ish` + availability keywords gate `classify_booking_intent`; a scheduling message enters the picker instead of the agent (the agent fabricated availability; fix is structural, not the prompt guardrail). (Removed v1.0.72: `_render_slot_offer`, `_SLOTS_PER_TURN`, `_format_local_slot`; removed v1.0.74: `_join_and`, `_DETAIL_LABELS` — superseded by `_i18n.join_and` + `t("label_*", lang)`.)
  - `_DATE_ISH_RE` — v1.0.73: Malay months `mac|mei|ogos|okt|dis` + Malay weekdays + relative words (`esok|lusa|hari\s+ini|minggu\s+depan|minggu\s+hadapan`) added. `\b`-anchored — safe for Latin script.
  - `_DATE_ISH_CJK_RE` — v1.0.73: separate regex with **no `\b`** (Python `\b` is not a word boundary between two CJK chars; would silently miss "我要7月1日"). Consumed by `_looks_date_ish` alongside `_DATE_ISH_RE`.
- **Multilingual routing fixes** (v1.0.73) — nav frozensets (`_PICKER_BAIL_PHRASES`, `_ANOTHER_DATE_PHRASES`, `_EARLIEST_PHRASES`) widened to include Malay and Chinese phrases so mid-flow navigation works in all three languages.
- **Full picker reply localization** (v1.0.74) — all deterministic picker replies now rendered via `booking/_i18n.py` (`t(key, lang)` helper) in **en / ms / zh-cn**. Language detected once on entry via `llm._detect_booking_language` and stored in `BookingPickerState.language`; never re-detected mid-flow (bare email/phone turns would misdetect).
- **Malay booking-intent routing** (v1.0.75) — `_detect_booking_language` adds a Malay-vocabulary boost so a Malay first message is not misclassified as English. `classify_booking_intent` prompt (in `llm.py`) now explicitly instructs the model to recognize bare Malay and 中文 dates as `"book"` intent.
- `_resolve_service_duration_minutes(client, tenant_id, service_id) → int` — reads EA service duration to size the appointment; falls back to `_DEFAULT_SLOT_MINUTES=30`
- `_detect_booking_language(text) → "en" | "ms" | "zh-cn"` — NEW (v1.0.74) — lightweight CJK-script / Malay-vocab / ASCII heuristic; v1.0.75 adds Malay-vocabulary boost so Malay first messages don't misclassify as `"en"`
- `_do_reschedule(conn, ...) → BookingReply` — reschedule reply uses `_resolve_provider_service` (branch config → EA-API fallback) to build the signed `/b/{jti}` short link + `*Reschedule here:*` label; `except` widened to catch `asyncpg.PostgresError` (v1.0.64+v1.0.67). ⚠️ Reply text still English — cancel/reschedule/confirm command-reply localization is NOT YET done.
- `_resolve_tenant_timezone(conn, tenant_id) → str | None` — NEW (v1.0.67) — reads `tenants.settings.booking_config.tenant_timezone`; fail-soft to `None` (renders as UTC)
- `_format_slot(booking, tenant_timezone) → str` — NEW (v1.0.67) — delegates to `_notify.format_slot(start_at, tenant_timezone)` so confirm/cancel/reschedule chat messages show the slot in the tenant's configured timezone instead of raw UTC

**`BookingReply` dataclass:** `content: str`, `kind: BookingReplyKind`, `new_picker_state: BookingPickerState | None`  
**`_BOOKABLE_CHANNELS`** (migration 116): `whatsapp | messenger | instagram_login | telegram | email` — legacy `instagram` removed  
**Gated on:** cancel/reschedule/confirm require an existing booking; `"book"` command is ungated (only requires bookable channel)

---

### `booking/_i18n.py` — NEW (v1.0.74) — picker reply localization

Static 3-language (en / ms / zh-cn) string table for the in-chat booking picker.

**Key exports:**
- `t(key, lang, **kw) → str` — render a message key for `lang` (fallback `en`), then `.format(**kw)`
- `join_and(items, lang) → str` — Oxford-free list join with localized conjunction; CJK uses `、` separator
- `coerce_lang(value) → Lang` — narrow arbitrary value to `"en" | "ms" | "zh-cn"` (default `"en"`)
- `WEEKDAYS`, `MONTHS`, `AMPM` — per-language tuples for locale-safe date formatting (no `strftime` locale dependency)
- `T` — full message registry (40+ keys covering availability, date prompts, details capture, summary/confirm, bail, errors)

**Design:** EN values are byte-for-byte copies of the original `conversational.py` strings — EN reply regression tests keep passing unchanged. Gaps fall back to `"en"` at runtime rather than raising.

---

### `booking/slot_picker_state.py` — NEW (v1.0.63) — In-chat picker state

**`BookingPickerState`** (`@dataclass(slots=True)`):
- `step: str` — `"awaiting_date"` | `"awaiting_pick"` | `"awaiting_details"` | `"awaiting_confirm"`
- `service_id: int | None`, `provider_id: int | None`
- `offered_slots: list[str]` — full ISO8601 slot set for current window
- `resolved_slot: str | None` — set once a time is matched
- `offset_days: int` — pagination offset (0 = today)
- `customer_name: str | None`, `customer_email: str | None`, `customer_phone: str | None` — collected in `awaiting_details`
- `detail_attempts: int` — re-ask counter; caps the details loop → link fallback
- `language: str` — `"en"` | `"ms"` | `"zh-cn"` — detected once on flow entry, carried across every turn (v1.0.74); picker replies rendered via `booking/_i18n.py`
- `to_dict() / from_dict(data)` — fail-soft JSON codec (mirrors `EscalationState`; legacy blobs without `language` coerce to `"en"`)

**Redis:** persisted at `book:{conversation_id}` (TTL 30 min) by `pipeline._load_booking_picker_state` / `_save_booking_picker_state`

---

### `booking_reminders.py` — Scheduled send (text or template)

**Functions:**
- `tick_due(pool) → count_sent` — Nightly worker; cross-tenant
- `_process_one(reminder_row) → None` — Sends via shared `_notify.send_appointment_message`; void on cancel/reschedule (SAVEPOINT)

**Template mode:** Tenant `settings.booking_config.reminder_template_name` + vars `{1}=service`, `{2}=datetime` (slot now formatted in `booking_config.tenant_timezone`, UTC fallback — fixes prior hardcoded-UTC)
**BookingConfig knobs (tenants.settings.booking_config):** `tenant_timezone`, `{confirmation,reminder,cancellation,reschedule}_template_name`+`_language`, `confirmation_message` (now sent on confirm), `inchat_picker` BOOL (default `false`; `true` → Phase 2 NL slot picker; `false` → link-mode only; v1.0.64: surfaced as a field on `BookingConfig` model + Switch in portal Bookings page)
**Config:**
- `booking_reminder_worker_enabled` (default True)
- `booking_reminder_send_offset_minutes` (30 default)

---

## Follow-ups & Engagement

### `followups/timing.py` — Window calculation

**Functions:**
- `compute_followup_send_time(anchor, tenant_tz) → datetime | None` — Latest in [+12h, +23h]; skip if whole window is quiet

---

### `followups/eligibility.py` — Gate checks

**Functions:**
- `should_schedule_followup(inbound) → bool` — Inbound gate (not a handoff, media, or flow)
- `should_send_followup(reminder_row, tenant_data) → bool` — Worker gate (window, opt-out, attempts)
- `is_followup_enabled(tenant_data) → bool`

---

### `followups/scheduler.py` — Upsert + cancel

**Functions:**
- `maybe_schedule_followup(conn, tenant_id, conversation_id, ...) → None` — Idempotent upsert; called after Phase-4 reply
- `cancel_pending_followup(conn, tenant_id, conversation_id) → None` — Called on every inbound

---

### `followups/worker.py` — Nightly tick worker (at-least-once)

**Functions:**
- `tick_due(pool) → count_sent` — Cross-tenant loop
- `run_forever() → None` — In-process lifespan task + `scripts/run_followup_worker.py`

**Behavior:** Single worker / at-least-once (mirrors `booking_reminders`)  
**Config:** Quiet hours via `settings.followup_config.quiet_hours_start/end` (tenant-local zoneinfo)

---

## Email Assist (Email Channel)

### `email_assist.py` (✅ main)

**Functions:**
- `generate_draft(email, customer_message) → draft_reply` — Haiku; bot-generated

---

### `email_assist_queue.py` / `email_assist_worker.py` (✅ main)

**Worker:** Dedicated consumer for email channel (separate from inbound pipeline)  
**Behavior:** Chatwoot natively handles IMAP/SMTP; Polaris adds draft-assist only  
**Hardening (6ebeb7f):** SAVEPOINT-recover on assist failure — drops instead of poisoning the outer transaction

---

## Channels & Provisioning

### `channels/provision.py` — Channel add/remove

**Functions:**
- `add_channel(tenant_id, channel_type, credentials) → channel`
- `remove_channel(tenant_id, channel_id) → None`

**v2.1 enhancement:** Accepts optional `override_tier_cap` + `override_reason` kwargs (admin privilege)

**Supported channels** (`_BOT_PROVIDERS` = whatsapp/messenger/telegram/instagram_login + email):
- WhatsApp (WABA)
- Messenger (Page)
- **instagram_login** (graph.instagram.com / IG-Login user token) — legacy FB-page `instagram` DROPPED (migration 109)
- **Telegram** (v2.1, Pro+)
- **Website Widget** (v2.1, read-only)
- **Email** (email-channel branch; Chatwoot-native)

---

### `channels/lazy_provision.py` (✅ LIVE on main)

First-inbound auto-provision so a connected CW inbox needs no manual `POST .../channels`. Wired into
`webhooks/chatwoot_router.py` — import at line 73, call at line 590.

**Function:** `lazy_provision_channel(*, account_id, inbox_id) → LazyProvisionResult`
(outcome ∈ provisioned | disallowed | no_tenant | not_applicable).

**Flow:** `resolve_by_chatwoot_account_id(account_id)` → `list_inboxes` (CW service token) → map
`channel_type` → kind → read `tenant_quotas.allowed_channel_kinds` → if entitled,
`provision_channel(bypass_tier_cap=False)` + `audit(channel.auto_provisioned)` + best-effort
`get_channel_token`→`secrets.put` + `attach_inbox_agent_bot` → re-resolve.

**Scope:** {whatsapp, messenger, instagram_login} only (token-capturable). telegram/email OUT
(own connect flows) → `not_applicable`. Disallowed kind → `disallowed` (router skips + neg-caches,
NEVER bypasses tier cap) + records `webhook_events(status='tenant_not_found',
error_class='ChannelNotEntitled:<kind>')` + sets Redis negative-cache
`lazy_skip:{account_id}:{inbox_id}` (TTL 600s) so repeat messages skip re-checking; widening
`allowed_channel_kinds` self-heals after the TTL expires. Idempotent: `UniqueViolationError` race →
treated provisioned. Uses the BYPASSRLS service pool (added to `scripts/lint_service_pool.py`
allowlist; same posture as `webhook_resolver.py`).
**Called by:** `chatwoot_router.py` at the `tenant_not_found` dead-end (lines 73, 590).
**Tests:** `apps/api/tests/test_lazy_provision.py` (8 cases).

---

### `telegram_provision.py` (🔄 v2.1 → Chatwoot-native cutover, uncommitted 2026-06-12)

**Functions:**
- `validate_telegram_bot(client, *, bot_token) → TelegramValidation(bot_id, bot_username)` — Network-phase, **getMe ONLY** (was `register_telegram_webhook` — the `setWebhook`-to-Polaris call was dropped; Chatwoot owns the bot webhook now via a native `Channel::Telegram` inbox)
- `persist_telegram_channel(conn, *, tenant_id, bot_token, validation, chatwoot_inbox_id) → TelegramProvisionResult` — DB-phase; `telegram_webhook_secret` left **NULL** (no Polaris webhook); still stores the encrypted bot token (`secrets.put 'telegram_bot_token'`) for DIRECT replies

---

### `widget.py` — NEW (v2.1, off-Meta M3/M4)

**Functions:**
- `is_allowed_target(host: str, allowlist: list[str]) → bool`
- `get_public_config(key: str) → WidgetConfig` — Tenant + target (cached 60s)

**Read-only:** No writes allowed; no redirect/attribution

---

## Chatwoot Integration

### `chatwoot/client.py` — Dual-token ChatwootClient + internal API (Phase 12, v1.0.18/19/20)

**Audit Batch B (2026-07-02):** internal calls now prefer the shared `http_client` singleton
(same adoption pattern as `meta_whatsapp.py`).

**Constructor:** `ChatwootClient(api_key, agent_token, ...)`

**Bot token (built-in messages):**
- `flip_conversation_status`, `assign_team`, `post_private_note`, `post_outbound_message`, `mark_bot_as_active`
- **v1.0.20:** `post_outbound_message(account_id, conversation_id, content, source_id=None)` — NEW `source_id` param (optional, str). When set, Chatwoot's `send_on_channel_service` skips re-delivery on the channel. Use case: pass `provider_message_id` from Meta/Telegram send to prevent double-send (customer sees reply once, not twice). None for backward compatibility.

**Agent token (contact manipulation):**
- `ensure_contact` (NEW v1.0.18: multi-channel identifier support)
- `create_conversation`, `ensure_label`, `apply_conversation_label`, `apply_contact_label`

**Internal API (v1.0.19, docker-internal Chatwoot fork endpoint):**
- `get_channel_token(account_id, inbox_id) → str | None` — Best-effort fetch of channel token via `/internal/inboxes/:id/token` (auth `X-Polaris-Internal-Token`); used by `add_channel` for auto-provision; never logs token value

**v1.0.20 change (PR #90):** `post_outbound_message` now includes `source_id` in request body (when provided). Chatwoot's `send_on_channel_service` recognizes the field and skips channel re-send. The reply-mirror call sites (main pipeline, agent path, booking_command) pass `source_id=provider_message_id` from their send step. (The flow_runtime mirror call site was retired with the engine — audit Batch D, 2026-07-02.)

**v1.0.18 change:** `ensure_contact` now branched by channel:
- **WhatsApp:** E.164 phone number search/create via `/contacts/search`
- **Instagram/Messenger/Telegram:** identifier path (`{channel}:{channel_user_id}`) via `/contacts/filter` (attribute_key=identifier)
- Empty channel_user_id guard prevents junk identifier rows

**Config:** 
- `CHATWOOT_AGENT_API_TOKEN` (separate from bot token)
- `CHATWOOT_INTERNAL_TOKEN` (NEW v1.0.19, for `/internal/*` calls)
- `CHATWOOT_INTERNAL_BASE_URL` (NEW v1.0.19, e.g. http://chatwoot-rails:3000; empty disables auto-provision)

---

### `chatwoot/handoff.py` — Conversation handoff + labeling

**Functions:**
- `_chatwoot_path(tenant_id, conversation_id, team_id) → None` — Assign team (skip if team_id="0") on the LINKED CW conversation; apply `human-handling` label

**v2.1 enhancement:** Audit-log `handoff.initiated` (non-fatal)

---

### `chatwoot/agent_bot.py` — NEW (rehaul Stage 3) — auto-attach platform bot

**Functions:**
- `attach_platform_bot(inbox_id, ...) → None` — Resolve the shared platform "Polaris Bot" by name (`account_id IS NULL`, one bot for all accounts) and link it to a newly-provisioned inbox so new conversations start `pending` (bot-owned) — which is what makes the human-handling gate + native escalation work.

**Called by:** admin channel-add, AFTER the DB txn commits (best-effort — a Chatwoot outage must never fail channel provisioning). Failure → an unattached inbox; fix via re-add or `scripts/backfill_agent_bot_attach.py`.

---

### `chatwoot/agent_reply.py` — NEW (PR #108, v1.0.31) — mirror human-agent replies → `messages`

Post-cutover, Chatwoot sends human-agent replies to the customer NATIVELY (Polaris only acks the webhook), so the portal Conversations transcript (Polaris-`messages`-backed) was missing all human-agent turns on handed-off conversations. This closes that for bot-provider channels.

**Functions:**
- `build_reply_row(payload) → HumanAgentReplyRow | None` — pure; derives the persisted row from a `message_created` payload (text or attachment-summary; `None` if nothing to show).
- `persist_human_agent_reply(*, tenant_id, chatwoot_conversation_id, payload, redis) → bool` — writes via the **anon pool** under RLS; idempotent (`provider_message_id='chatwoot.<id>'` + `ON CONFLICT (tenant_id, provider_message_id) DO NOTHING`); `role='assistant'` + `metadata.author_type='human_agent'` (+ agent name/id) so the portal renders it distinctly (no `messages.role` change / no migration); bumps `conversations.last_message_at`; best-effort Redis cache invalidation.

**Called by:** `chatwoot_router._handle_human_agent_reply` (bot-provider → persist [propagates PostgresError→503, idempotent retry]; email → `email_assist.record_agent_reply` draft-acceptance). Reader: `tenant_conversations.py` exposes `author_type`/`author_name` (shared-types `MessageDetail`); portal `ConversationTranscript.tsx` renders the distinction.

---

### `chatwoot/label_sync.py` — Two-label state sync (rehaul + Phase 7)

**Functions:**
- `sync_opt_out_label` — apply `pol_opted_out` to the CW contact after a STOP opt-out (PDPA; ≤5s SLA)
- `sync_suspension_label` — apply `pol_bot_suspended` to every open conversation under the tenant's `chatwoot_account_id` (Phase 9 one-shot)
- **bot-handling / human-handling** conversation labels track ownership (applied via the deferred CW flush in `pipeline.run`); their removal (`human-handling` off) resumes the bot (reverse-handoff via `conversation_updated`).

**Behavior:** SELECT-driven, non-blocking; silently skip when the CW link is missing; CW failures logged, never re-raised (the Polaris-side write is source of truth).

---

### `webhook_resolver.py` — Webhook metadata resolution (v1.0.18 extended)

**Functions:**
- `resolve_meta(...) → (tenant_id, channel, user_id, ...)`
- `record_event(event_type, ...) → None` — Append-only event table

**AgentForwardContext (v1.0.18):** Extended to support all channels
- **WhatsApp:** `phone_number_id`, `waba_token` (via `meta_waba_access_token`)
- **Instagram/Messenger:** `page_token` (NEW v1.0.18, via `meta_fb_page_access_token`)
- **Telegram:** `telegram_token` (via `telegram_bot_token`)
- `resolve_agent_forward_context(tenant_id, chatwoot_conv_id) → context | None` — Multi-step lookup: conversations → channel_user_id; tenant_channels → {phone_number_id|ig_business_account_id|fb_page_id|telegram_bot_id}; tenant_secrets → appropriate token kind

> **phase-10 note:** `AgentForwardContext` + `resolve_agent_forward_context()` still EXIST and are now consumed by `services/pipeline.py` — NOT by `chatwoot_router`. `chatwoot_router`'s forward path (`_dispatch_agent_forward`) is DELETED; human-agent replies are forwarded by Chatwoot natively (Polaris re-sending double-sent on Messenger/IG).

---

## Observability & Health

### `admin_health.py` (apps/api/polaris_api/routers/) — System health snapshot endpoint

**Entry:** `GET /api/admin/health → HealthSnapshot` (platform_admin auth required)

**Aggregation:** Single endpoint, 15+ independent probes (DB sequential, Redis/HTTP concurrent). Each probe silently degrades on failure; one failing signal NEVER fails the whole response. Cache layer (60s TTL) throttles DB/Redis calls on rapid manual refreshes (5-min admin tab).

**Signals (17 total, async/parallel where possible):**

```
Infrastructure (2):
  - supabase: SELECT 1 latency probe
  - redis: PING + INFO memory

Service & Channel Connectivity (5):
  - chatwoot: HTTP GET (any response = reachable)
  - ea: HTTP GET + status check (< 500 = healthy)
  - telegram_webhooks: webhook_events (source='telegram', 1 h window, HMAC/tenant-not-found)
  - meta_webhooks: webhook_events (sources='meta'|'wa-status'|'wa-account'|'meta-template-status', 1 h)
  - meta_rate_limits: X-Business-Use-Case-Usage avg over 24 h per surface

Traffic & Engagement (4):
  - active_tenants: distinct tenants in messages (24 h); total_provisioned (is_demo=false)
  - messaging: inbound/outbound (24 h); bot_replies_mtd
  - email_drafts: queue depth + resolved outcomes (24 h); latest_draft_at all-time
  - knowledge_gaps: miss captures by signal_type (24 h); latest_at all-time

Spend & Quota (3):
  - llm: token burn (24 h + MTD); est. MYR cost
  - quota: saturation vs cap (all tenants; top-5 highest)
  - broadcasts: queue depth (pending/in_flight/failed_last_hour)

Webhook & Errors (1):
  - webhook_event_errors: ingest error breakdown (24 h)
    Groups by status + error_class from webhook_events. Excludes benign statuses:
    (received, processed, replay_ignored, human_handling, demo_routed,
     non_message_event, chatwoot_deferred). Shows top-8 by status + by error_class;
    top-5 most recent rows. NULL error_class mapped to '(none)'.
    Decorated with grafana_url link when configured.

Observability (2):
  - failures: broadcast errors + deep-links (Grafana, Sentry)
  - apm: Sentry error count (24 h); requires sentry_auth_token to be set
```

**Implementation:** All probes are `async def _probe_*(...) → Model`, with a `_safe_*` wrapper that catches exceptions and returns a degraded version (never re-raises). DB probes run sequentially (asyncpg single-connection limitation); Redis and HTTP probes run concurrently with `asyncio.gather`.

**Webhook Event Errors Detail:**

```python
_probe_webhook_event_errors(conn, settings) → WebhookEventBreakdown:
  # Two queries (both 24-hour window):
  # 1. Grouped: SELECT status, error_class, COUNT(*) ... GROUP BY status, error_class
  #    (excludes benign statuses server-side via NOT IN).
  # 2. Recent:  SELECT received_at, source, status, error_class
  #    ... ORDER BY received_at DESC LIMIT 5
  #
  # Returns:
  #   total_errors_24h: sum of all non-benign events
  #   by_status: top-8 statuses (hmac_fail, tenant_not_found, suspended_*, etc.)
  #   by_error_class: top-8 error_class values (ChannelNotEntitled:instagram, etc.)
  #   recent: 5 most recent non-success rows with timestamps
  #   grafana_url: Grafana logs/explore link (when configured)
  #   error: NULL on success, str on probe failure (DB outage, timeout, etc.)
```

---

## Admin & Audit

### `db/pools.py` — dual-pool singletons

**Audit Batch B change (2026-07-02):** `_SERVICE_MAX_SIZE` raised **4→6** (service/BYPASSRLS
pool). `_ANON_MAX_SIZE` unchanged at 10. Rationale: scheduled-job connection-scoping work in
the same batch (tighter per-tenant connection lifetimes in `ea_settings_sync`,
`retention_sweep`, `cost_ceiling_check`, `quota_flush`, `invoice_overdue`) increased
short-lived concurrent service-pool demand from background jobs; 4 was becoming a
contention point against admin-route traffic sharing the same pool.

### `utils/errors.py` — NEW (audit Batch E, 2026-07-02) — standard error envelope

**Functions:**
- `api_error(code: str, message: str, *, status_code: int) → HTTPException` — builds the
  `{"detail": {"code": ..., "message": ...}}` envelope shape. Applied at 5 call sites in
  `admin_billing.py` (previously ad-hoc `HTTPException(detail="...")` strings at those
  sites); not yet swept across every router — a purely additive helper, not a breaking
  contract change.

### `scripts/lint_rls_grants.py` — NEW (audit Batch C, 2026-07-02) — CI lint

Runtime DB check, not a static file scan: queries `pg_policies` + `information_schema.
role_table_grants` against whatever Postgres it's pointed at (CI's `postgres:17` service
post-`apply.py`, or a local dev DB). For every table with at least one active RLS policy,
asserts a non-empty GRANT exists for `polaris_anon` and/or `polaris_service` — an
RLS-policy-without-GRANT is an orphaned policy that silently 403s/500s the first route that
queries it. Exit 0 = clean, exit 1 = orphaned-policy table names printed to stderr. Explicitly
modeled on the migration 118→119 bug class (118 shipped RLS+policies with zero GRANTs; see
the durable gotcha "a new tenant-facing table needs a `_grants` migration"). Companion to
the pre-existing `scripts/lint_service_pool.py`. Has its own test file.

### `audit.py` — Immutable append-only ledger

**Functions:**
- `record(actor, action, before, after) → None` — INSERT to audit_logs; hash-chain per entity
- `verify_chain(entity_id, entity_type) → verified | None` — Check hash continuity

**Chain:** Each row's `before` hash matches previous row's `after` hash

---

### `audit_chain_verifier.py` — Nightly verification job

**Functions:**
- `verify_all_chains(pool) → (verified, failed_chains)` — Scan all entities; flag breaks

---

### `secrets.py` — KMS encrypt/decrypt

**Functions:**
- `encrypt(plaintext, tenant_id) → ciphertext` — AES-256-GCM
- `decrypt(ciphertext, tenant_id) → plaintext` — ANON pool (RLS enforces per-tenant read)
- `rotate_secret(tenant_id) → new_ciphertext`

**Key management:** `POLARIS_KMS_KEY` env var (Phase 1 one-time setup; post-Phase-1 regeneration = catastrophic)

---

## Billing & Cost

### `billing/invoice.py` — Invoice sequencer

**Functions:**
- `create_invoice(tenant_id, period, ...) → invoice_id`
- `mark_paid(invoice_id) → None`
- `void_invoice(invoice_id, reason) → None` — v2.1 new; 409 if already paid; audit `invoice.voided`

---

### `billing/proration.py` — Plan change math

**Functions:**
- `calculate_proration(old_plan, new_plan, cycle_day) → proration_amount_myr`

---

### `mrr_rollup.py` — MRR calculation (PR #80)

**Functions:**
- `compute_mrr(conn) → float` — Calculate monthly recurring revenue (all active tenants)
- `compute_and_persist(conn) → snapshot_id` — INSERT to `platform_mrr_snapshots` (migration 093)

**Read-only endpoint:** `GET /api/admin/financials/mrr` (no nightly computation in response; uses persisted snapshots + live delta)

---

### `cost_ceiling.py` — Soft + hard cap enforcement

**Functions:**
- `check_cost_ceiling(conn, tenant_id, llm_cost_usd) → CostCeilingExceeded | None`
- On 75%: email alert
- On hard-cap: Sentry page + auto-disable Cohere rerank

---

### `grafana_cost.py` — Mimir proxy + cost stub (updated PR #98)

**Functions:**
- `_fetch_cost(tenant_id, month) → float` — Mimir API or stub; returns USD converted to MYR via `USD_TO_MYR_RATE` (default 4.15)
- `_stub_cost(tenant_id, month) → float` — Deterministic fixture (E2E testing)

**Pricing:** All LLM costs denominated in USD internally; one FX knob (`USD_TO_MYR_RATE=4.15`). Qwen embedding = `$0.01/M tokens`. Prompt-cache priced at reduced rate. ⚠️ NOTE (verified vs code @7d1b531): PR #98 did NOT change the per-tier aggregation shape — `grafana_cost.py` still returns `avg_input_tokens`/`avg_output_tokens`/`avg_cache_read_tokens` (30-day sums, "avg"-misnamed) + `sample_count` (hardcoded 0), and Supabase per-tenant cost is STILL present (`supabase_storage_cost_myr` / `polaris_supabase_cost_myr_total` query). PR #98's actual changes were the `metrics.py` OTel emission + USD pricing + FX knob + embedding-priced + cache-read populated.

---

### `metrics.py` — OTel cost counters (NEW PR #98, `apps/api/polaris_api/metrics.py`)

**NOT in `services/`** — lives at `polaris_api/metrics.py` (top-level, alongside `observability.py`).

**Counters:**
- `llm_tokens_total` — by `provider` + `model` + `tenant_id` label (prompt/completion/cache)
- `cohere_rerank_calls_total` — by `tenant_id`
- `embedding_calls_total` — by `provider` + `model`
- `classify_calls_total` — by model

**Call sites:** `llm.generate_response`, `llm.classify_intent`, `rag.rerank_chunks`, `embed.embed_query`

**Backend:** OTel `MeterProvider` (configured in `observability.py`); pushes to Grafana Cloud Mimir on VPS.

---

## Observability

### `meta_usage.py` — Rate-limit tracking (migration 085)

**Functions:**
- `record(phone_number_id, headers) → None` — Parse X-Business-Use-Case-Usage header; INSERT to meta_rate_limit_snapshots

**Wiring:** Registered as sink in lifespan; called after every Meta send (best-effort, swallows errors)

---

## Utilities

### `stop_keyword.py` — Exact-match STOP

**Functions:**
- `check_stop_keyword(text) → bool` — Fullmatch after strip + lowercase
- Variants: EN=`STOP BOT REPLY`, BM=`BERHENTI`, ZH-S=`退订`

**Side-effect:** Calls `mark_opted_out(tenant_id, channel_user_id)`

---

### `quota.py` — Redis counter enforcement

**Functions:**
- `check_quota(redis, tenant_id, plan_tier) → OverQuota | None` — Hard-cap check; soft-cap warning log
- `flush_to_db(conn) → None` — Nightly job; scan Redis keys → UPSERT tenant_usage

**Caps:** Starter 5k / Pro 25k / Ultra 60k per month; counter key = `bot_replies:{tenant}:{YYYYMM}`

---

### `demo_sessions.py` — Demo session primitives (v1.0.45, ADR-066 — full rework of Phase 14a stub)

**Functions (service pool, exact-token lookups):**
- `find_demo_tenant_by_code(conn, code) → tenant | None` — exact `lower(demo_code)` match
- `has_ever_used_demo(conn, contact_id, demo_tenant_id) → bool` — one-demo-ever gate (90-day reset via `reset_visitor_demo`)
- `get_active_demo_session(conn, contact_id, demo_tenant_id) → session | None`
- `activate_demo_session(conn, contact_id, demo_tenant_id, gateway_tenant_id) → session`
- `increment_demo_message(conn, session_id) → new_count`
- `expire_demo_session(conn, session_id) → None`
- `mark_lead_flagged(conn, session_id) → None` — stamps `lead_flagged_at`
- `reset_visitor_demo(conn, contact_id, demo_tenant_id) → None` — admin-only 90-day reset

---

### `demo_runtime.py` — Demo state machine (v1.0.45, ADR-066)

Read-only resolver; no side-effects. `gateway_tenant_id` is REQUIRED (resolved from `incoming.tenant_id`).

**`resolve(incoming, conn) → DemoState`** — returns one of:
- `default` — no demo code in message, no active session → use gateway tenant as-is
- `activate` — valid unrecognised code typed, has not used demo → start session
- `continue_demo` — active session within cap/time → continue as brand tenant
- `expire` — session hit cap or time limit → revert + trigger lead flag
- `refuse` — has already used this demo (one-demo-ever) → polite refusal

---

### `demo_lead.py` — Lead flagging (v1.0.45, ADR-066)

**`flag(conn, cw_client, session, gateway_tenant_id) → None`**
— stamps `demo_sessions.lead_flagged_at` + posts a Chatwoot private note on the gateway conversation to surface the lead to the operator.

---

### `idempotency.py` — Outbound idempotency key tracking

**Functions:**
- `ensure_idempotent_send(tenant_id, outbound_id) → already_sent | None`

---

### `impersonation.py` — Admin JWT mint for impersonation

**Functions:**
- `mint_impersonation_jwt(admin_id, tenant_id) → jwt_token` — HS256; 1h TTL
- `clear_impersonation() → None`

---

### `meta_mock_store.py` — NEW (Phase 20 E2E)

**Thread-safe in-memory capture store:**
- `record(path: str, body: dict) → None` — Capture POST to mock
- `get_all() → list[dict]` — Retrieve all captures
- `clear() → int` — Clear + return count

**Shared with:** `/meta-mock` router + `GET /internal/e2e/meta-outbound-query` + `DELETE /internal/e2e/meta-outbound-clear`

---

### `conversation.py` — Conversation detail view

**Functions:**
- `get_conversation(conn, tenant_id, conversation_id) → Conversation`
- `get_messages(conn, conversation_id, limit=20) → [Message]` — ORDER BY created_at DESC; role tiebreak (user < assistant) for same-transaction ties

---

### `portal_invite.py` — Portal user invitation

**Functions:**
- `send_invite(tenant_id, email) → invite_token` — Magic-link email

---

## Summary Table

| Service | Phase | Purpose | Status |
|---------|-------|---------|--------|
| `pipeline.py` | P2–P20 | Inbound dispatcher | ✅ main |
| `llm.py` | P4 | Intent + generate | ✅ main |
| `rag.py` | P4 + PR #115 | Search + rerank; Cohere no-crash degrade | ✅ main |
| `knowledge_ingestion.py` | P3 | Chunking + embed | ✅ main |
| `knowledge_gaps/*` | KGD M1 + PR #115 | Capture (confidence-based) + dashboard | ✅ main |
| `escalation.py` | PR #115 | Opt-in human escalation state machine | ✅ main |
| `promotions/lookup.py` | PR #115 | Active promotions in default reply path | ✅ main |
| `reply_dispatch.py` | P6 | Multi-channel send | ✅ main |
| `meta_whatsapp.py` | P2 | Meta Graph API | ✅ main |
| `meta_messaging.py` | P15 | IG/Messenger + IG-Login send | ✅ main |
| `telegram_client.py` | v2.1 | Telegram Bot API | ✅ main |
| `media_download.py` | voice M1 | Audio fetch | ✅ main |
| `transcription.py` | voice M1 | Groq Whisper | ✅ main |
| `flow_runtime/*` | P5 | Node executor | 🗑️ RETIRED 2026-07-02 (audit Batch D) — deleted; survivors moved to `agent/ports.py` / `tenant_snapshot.py` / `chatwoot/business_hours.py` / `chatwoot/templates.py` |
| `agent/*` | PR #99, ADR-063; tier routing D4 (2026-07-02) | Tool-calling agent (4 tools, loop, pipeline integration, ADR-031 Haiku/Sonnet tier routing) | ✅ main |
| `booking/*` | P7, v2.1, v1.0.64–v1.0.75 | Booking lifecycle + new-booking intent (link+picker) + short-link redirect + build_ea_wizard_url + TZ-localized messages + reschedule _resolve_provider_service + multilingual picker (en/ms/zh-cn) via `_i18n.py` | ✅ main |
| `booking_reminders.py` | P13 | Reminder worker | ✅ main |
| `followups/*` | P13 | Follow-up worker | ✅ main |
| `email_assist*.py` | email channel | Email draft assist | ✅ main |
| `chatwoot/*` | P7, P12 | Chatwoot bridging | ✅ main |
| `webhook_resolver.py` | P2, P12 | Webhook metadata | ✅ main |
| `channels/provision.py` | P6, v2.1 | Channel add/remove | ✅ main |
| `channels/lazy_provision.py` | P10 | First-inbound auto-provision (entitlement-gated) | 🌿 branch |
| `widget.py` | v2.1 | Widget config | ✅ main |
| `grafana_cost.py` | P10, PR #98 | Mimir cost proxy + FX conversion | ✅ main |
| `metrics.py` (top-level) | PR #98 | OTel cost counters | ✅ main |
| `audit.py` | P1 | Append-only ledger | ✅ main |
| `secrets.py` | P1 | KMS encrypt/decrypt | ✅ main |
| `billing/*` | P9 | Invoice + proration | ✅ main |
| `mrr_rollup.py` | P9, PR#80 | MRR calculation | ✅ main |
| `cost_ceiling.py` | P10 | Cost cap enforcement | ✅ main |
| `meta_usage.py` | P10 | Rate-limit tracking | ✅ main |
| `quota.py` | P10 | Bot reply count | ✅ main |
| `stop_keyword.py` | P10 | STOP matcher | ✅ main |
| `demo_sessions.py` | v1.0.45, ADR-066 | Demo session primitives (full rework) | ✅ main |
| `demo_runtime.py` | v1.0.45, ADR-066 | Demo state machine (default/activate/continue/expire/refuse) | ✅ main |
| `demo_lead.py` | v1.0.45, ADR-066 | Lead flag + CW private note on demo expiry | ✅ main |
| Plus 10 more | P0+ | (see above) | ✅ main |

