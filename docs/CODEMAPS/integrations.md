# Integrations Codemap — Polaris v2


**Last Updated:** 2026-06-13 | **Doc-metadata refreshed 2026-07-03** (facts below unchanged except the Anthropic model line; this file was not part of the 2026-07-02 audit-remediation doc-sync pass and had drifted on its own header only) | **DEPLOYED v1.0.31 (2026-06-13)** — Chatwoot-native cutover + post-cutover hardening all LIVE: Chatwoot owns the channel inboxes; its **agent-bot** fires `message_created` on every inbound → `POST /v1/webhooks/chatwoot` (the PRIMARY inbound LIVE). Polaris gates on `conversation.status=='pending'`, resolves the real channel + recipient, runs the pipeline, replies DIRECT to Meta/Telegram, then mirror-posts to the **linked** CW conversation with `source_id`. **`meta_router.py` is DELETED** — no direct Meta→Polaris customer-message path. **Non-message WA webhooks** (delivery/read receipts, template-status, account/MM-Lite) reach Polaris via a **fork-relay**: a `prepend_mod_with` module on `Webhooks::WhatsappEventsJob` (polaris-cw-fork) → the existing `wa_status` / `meta_template_status` / `wa_account` routers (unchanged). Legacy FB-page `instagram` channel DROPPED (migration 109); only `instagram_login` remains. **Telegram is now a NATIVE Chatwoot `Channel::Telegram`** — inbound rides the agent-bot path (`resolve_chatwoot` admits channel_kind 'telegram'); the legacy own-webhook path (`telegram_router.py`, `resolve_telegram`, `_telegram_extract.py`, `seed_telegram_channel.py`) was DELETED. `connect_telegram` no longer self-`setWebhook`s (`register_telegram_webhook`→`validate_telegram_bot`, getMe-only; `telegram_webhook_secret` NULL). **Human-agent replies:** Chatwoot forwards them natively; Polaris now persists them to messages table for portal transcript via `services/chatwoot/agent_reply.py` (PR #108). **Shipped v1.0.31 (2026-06-13):** #103 SAFE_FETCH, #104 quota_flush orphan-skip, #105 Mimir cost 404 fix, #106 messages.metadata grant, #107 Meta structured errors, #108 agent-reply persist, #109–110 JSONB double-encode fixes (migrations 111–112). All LIVE on prod.

Polaris integrates with two external whitelabeled forks (Chatwoot for messaging, Easy!Appointments for bookings) plus LLM/AI providers (Anthropic, OpenRouter, Cohere, Groq Whisper). This codemap documents the contract between `polaris` monorepo and external services.

## Fork Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│  Polaris v2 Monorepo (apps/api + apps/portal + apps/admin)      │
│  Consumes Docker images + REST APIs (never source deps)          │
└────────────────┬──────────────────────────┬─────────────────────┘
                 │                          │
        ┌────────▼─────────┐        ┌──────▼──────────┐
        │ polaris-cw-fork  │        │ polaris-ea-fork │
        │ (Chatwoot v4.14) │        │ (EA v1.6.0)     │
        │ GitHub repo      │        │ GitHub repo     │
        └────────┬─────────┘        └──────┬──────────┘
                 │                         │
        ┌────────▼─────────┐        ┌──────▼──────────┐
        │ GHCR images:     │        │ GHCR images:    │
        │ polaris-cw       │        │ polaris-ea      │
        │ v4.14.0-r1       │        │ v1.6.0-r18      │
        └────────┬─────────┘        └──────┬──────────┘
                 │                         │
        ┌────────▼─────────────────────────▼──────┐
        │ docker-compose.prod.yml (pulls forks)   │
        │ Polaris-own api/portal/admin built on box│
        └────────┬─────────────────────────────────┘
                 │
        ┌────────▼──────────────────────────────────┐
        │ VPS: Caddy → api/portal/admin + chat(CW)   │
        │ + calendar(EA); Supabase DB; Redis ×2      │
        └────────────────────────────────────────────┘
```

## 1. Chatwoot Fork (`polaris-cw-fork`)

**Repo:** https://github.com/Jiun01/polaris-cw-fork  
**Upstream:** chatwoot/chatwoot @ v4.14.0 (rebased v4.13.0→v4.14.0 on 2026-05-30)  
**Image:** `ghcr.io/jiun01/polaris-cw:v4.14.0-r3`  
**Status:** Built, E2E tested, VPS live (2026-06-07); r3 includes internal token endpoint (2026-06-09)  
**License:** AGPL-3.0 (same as upstream)

### Architecture

Rails monolith forked from Chatwoot v4.14.0. Topic branches rebased, white-labeled (brand assets, locales, mailers), then merged to `polaris/integration` final state. **Zero new migrations** — fork ships code/config patches only.

**Fork-relay (Chatwoot-native rehaul):** since the rehaul makes Chatwoot the WABA webhook owner, non-message WhatsApp events (delivery/read receipts, template-status, account/MM-Lite) no longer reach Polaris directly. A **`prepend_mod_with` module on `Webhooks::WhatsappEventsJob`** relays those raw events to Polaris (`wa_status` / `meta_template_status` / `wa_account` routers — logic unchanged). Customer messages flow the normal agent-bot → `/v1/webhooks/chatwoot` path. Config: the fork posts to Polaris's webhook base URL with the shared HMAC secret.

### Topic Branches

| Branch | Purpose | Status |
|---|---|---|
| `polaris/remove-enterprise` | Delete enterprise-only code paths | Merged to integration |
| `polaris/strip-features` | Remove features not in Polaris scope | Merged to integration |
| `polaris/defaults` | Bake Polaris defaults (SMTP, support email, contact form) | Merged to integration |
| `polaris/whitelabel` | Brand assets, 869 locale files, 21 mailer templates | Merged to integration |
| `polaris/suspension-enforcement` | Account suspension logic (payment_overdue flow) | LOCKED (ADR-049 sunk-cost lock 2026-05-15) |
| `polaris/integration` | Merge of all topic branches; ship-ready state | Tagged as v4.14.0-r1 |

### Polaris Dependencies (Phase 2 → 10)

| Phase | Service | Endpoint | Purpose |
|---|---|---|---|
| **2 (rehaul)** | `webhooks/chatwoot_router.py` | `POST /v1/webhooks/chatwoot` (Polaris receives) | **PRIMARY inbound.** Agent-bot `message_created` → HMAC verify → human-handling gate (`status=='pending'`) → resolve real channel/recipient → enqueue on the REAL provider. Human-agent reply (`outgoing`+`sender.type=='agent'`) is forwarded to the customer by Chatwoot NATIVELY (phase-10: `_dispatch_agent_forward` DELETED — Polaris re-sending double-sent on Messenger/IG); Polaris only calls `_record_email_agent_reply` on email inboxes (email-draft acceptance). Bot's own mirror (`agent_bot`) echo-dropped. `conversation_updated` label-removal / `conversation_status_changed`→resolved → reverse-handoff resume. |
| **6 (rehaul)** | `services/pipeline.py` + `services/chatwoot/client.py` | `POST /api/v1/accounts/{id}/conversations/{cid}/messages` (mirror) | **`_link_cw_conversation` LINKS the existing CW conversation** from the agent-bot payload ids (idempotent UPDATE) — never creates (the old `_ensure_cw_conversation` find-or-create produced a duplicate CW conversation; deleted). Mirror bot reply via `post_outbound_message(source_id=provider_message_id)` to prevent re-delivery (#90). |
| **3 (rehaul)** | `services/chatwoot/agent_bot.py` | (resolve bot by name + attach to inbox) | Auto-attach the shared platform "Polaris Bot" to a new inbox on channel provision (post-txn, best-effort) so conversations start `pending` (bot-owned). Backfill: `scripts/backfill_agent_bot_attach.py`. |
| **7** | `services/chatwoot/handoff.py` | `POST /api/v1/accounts/{id}/conversations/{id}/labels` | Apply `human-handling` conversation label + flip status → open + assign team (handoff) |
| **7** | `services/chatwoot/label_sync.py` | `POST /api/v1/accounts/{id}/contacts/{id}/labels` | Apply `pol_opted_out` to contact on STOP; `pol_bot_suspended` to all open conversations on suspension |
| **9** (admin) | `routers/admin_tenants.py` | `POST /api/admin/tenants/{id}/suspend` + `POST /api/admin/tenants/{id}/unsuspend` | Suspend/unsuspend account (ADR-049); status + suspended_reason written to `tenants` table |
| **2026-06-08** (admin) | `routers/admin_tenants.py` + `services/chatwoot/client.py` | `GET /api/v1/accounts/{id}/inboxes/{id}`, `GET /api/v1/accounts/{id}/inboxes`, `PATCH /api/v1/accounts/{id}/inboxes/{id}/channel` | NEW: Inbox lookup for auto-fill (`get_inbox`), list inboxes for email config (`list_inboxes`), update inbox channel (`update_inbox_channel`). All via **agent token** (CHATWOOT_AGENT_API_TOKEN). Admin panel auto-fill + email secrets surface. |
| **v1.0.18** (channel-recovery, PR #86) | `services/pipeline.py`, `webhooks/chatwoot_router.py`, `services/webhook_resolver.py` | `POST /v1/webhooks/chatwoot` (updated) | **`ensure_contact(identifier=...)`** — WhatsApp uses E.164 phone; IG/Messenger/Telegram use identifier path (`{channel}:{channel_user_id}`) via Chatwoot `/contacts/filter`. **`_dispatch_agent_forward`** (historical) extended agent-reply forwarding to IG + Messenger — ⚠️ this function is DELETED by phase-10 (CW now forwards human-agent replies natively). **`AgentForwardContext`** (added `page_token`) still EXISTS, now consumed by `pipeline.py`. |
| **v1.0.19** (channel auto-provision, PR #87 + fork PR #2) | `routers/admin_tenants.py`, `services/chatwoot/client.py` | `GET /internal/inboxes/:id/token` (NEW, Chatwoot fork internal endpoint; auth `X-Polaris-Internal-Token`, 422 unsupported channel) | **Channel token auto-capture** — `add_channel` endpoint now calls `get_channel_token()` to fetch the channel's outbound token from Chatwoot (inbound-only Chatwoot doesn't return FB/IG page tokens publicly). Token auto-stored in `tenant_secrets` (whatsapp→`meta_waba_access_token`, messenger/instagram→`meta_fb_page_access_token`). `get_inbox_info` returns `token_captured` bool. Response field + toast in AddChannelModal. Polaris-Chatwoot internal call via docker network; `Caddyfile.prod` blocks external requests to `/internal/*`. |
| **10** | — | — | Unchanged; no Phase 10 additions |

### Webhook Signature Verification (Phase 2 requirement)

**Breaking change:** Chatwoot v4.13.0 adds signed webhook headers per [release notes (verified 2026-05-16)](https://github.com/chatwoot/chatwoot/releases/tag/v4.13.0).

**Polaris implementation** (`apps/api/polaris_api/webhooks/chatwoot_router.py`):

```python
# webhooks/_hmac.py
def verify_chatwoot_signature(raw: bytes, header: str | None, secret: str) -> bool:
    """Bare hex (no sha256= prefix). Constant-time compare."""
    if not secret or not header:
        return False
    expected_hex = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected_hex, header)

# webhooks/chatwoot_router.py
@router.post("/chatwoot")
async def chatwoot_inbound(request: Request) -> Response:
    raw = await request.body()
    header = request.headers.get("x-chatwoot-signature")
    secret = get_settings().chatwoot_webhook_hmac_secret  # platform-wide env var
    if not verify_chatwoot_signature(raw, header, secret):
        raise HTTPException(status_code=401)
    # Process payload...
```

**Secret storage**: Platform-wide env var `CHATWOOT_WEBHOOK_HMAC_SECRET` (not per-tenant KMS; same single-install model as `CHATWOOT_BOT_ACCESS_TOKEN` per Q-PHASE7-01). Config field: `settings.chatwoot_webhook_hmac_secret`.

### Suspension Enforcement Patch (ADR-049)

**Topic branch:** `polaris/suspension-enforcement` (LOCKED, sunk-cost per 2026-05-15 audit)

When the Chatwoot account is set to suspended (via `PATCH /platform/api/v1/accounts/{account_id}` on the CW fork's own platform API), the fork enforces suspension across three surfaces:

1. **Auth layer** (`Api::BaseController`): `before_action :verify_account_active` → returns `403 service_suspended` if `Current.account.suspended?`
2. **DeviseTokenAuth**: Disabled accounts kicked on API login
3. **Sidekiq worker guard**: Inbound message processing drops messages for suspended accounts
4. **Portal widget** (v2.1): Public web_widget bubble hidden when account suspended

**Used for:** `suspended_reason='payment_overdue'` (full lockdown, ADR-049). Does **NOT** fire for `suspended_reason='quota_exhausted'` (ADR-018 partial, Chatwoot account stays active).

## 2. Easy!Appointments Fork (`polaris-ea-fork`)

**Repo:** https://github.com/Jiun01/polaris-ea-fork  
**Upstream:** alextselegidis/easyappointments @ v1.6.0  
**Image:** `ghcr.io/jiun01/polaris-ea:v1.6.0-r18` (prod compose pins moving tag `v1.6.0-prod`)  
**Status:** DEPLOYED + live-verified (2026-06-24 v1.0.67 + EA r18). Booking-confirmation loop COMPLETE: EA emitter built (`Booking::notify_polaris_appointment_confirmed`); `?polaris_ref=` captured in `index()` → script_var → `register()` POST. Asset auto cache-bust via `EA_ASSET_TOKEN` build arg. "Go to booking page" links → `/book/{slug}` via `resolve_tenant_slug`. POLARIS_API_BASE_URL new env.  
**License:** GPL-3.0 (SaaS exempt per FSF FAQ; DO NOT distribute to tenant-owned infrastructure)  
**Thin fork:** No strip-features branch (EA has no enterprise tier)

### Architecture

PHP **CodeIgniter 3** app (not Laravel) forked from Easy!Appointments v1.6.0. White-labelled product name: **Polaris Calendar**. Multi-tenant via shared MySQL schema + `tenant_id` (NOT NULL) column on every multi-tenant EA table. Tenant context resolved by:
- **API path** (Bearer-token JWT hook, `BearerAuthHook`): `Authorization: Bearer {jwt_with_tenant_id}`
- **Public booking path** (`/book/{slug}`): URL slug resolver in `Booking::slug()` → stores `booking_tenant_id` in session for AJAX requests
- **Admin backend** (`EA_Controller::__construct`): `resolve_session_tenant()` reads `user_id` from session → looks up `tenant_id` from `ea_users`

**Thin fork rationale (ADR-013 Amendment 2026-05-16):** EA has no enterprise tier to strip; fork remains close to upstream for easier rebases.

### Topic Branches

| Branch | Purpose | Status |
|---|---|---|
| `polaris/whitelabel` | Brand assets, locales, mailer subjects, public booking views | Merged to integration |
| `polaris/defaults` | Polaris defaults + marketing-stub `Home.php` controller | Merged to integration |
| `polaris/integration` | `tenant_id` + `tenant_branch_id` migration + `MY_Model` enforcement + slug-resolver + PDPA + Turnstile + IP rate-limit | Merged; tagged v1.6.0-r3 |

### Multi-Tenancy Enforcement (Batch C Tests — 2026-05-26)

**Base model pattern** (`application/core/MY_Model.php`):

```php
class MY_Model extends EA_Model {
    public function get_all() {
        // Enforce: SELECT * WHERE tenant_id = $this->tenant_id
        return parent::get_all() /* filtered by tenant context */;
    }
    public function insert($data) {
        // Enforce: $data['tenant_id'] = $this->tenant_id (immutable)
        return parent::insert($data);
    }
    // ... and update(), delete()
}
```

**Multi-tenant EA tables** (13 core):
- appointments
- services
- providers
- customers
- settings
- service_categories
- provider_working_plan
- provider_services
- admin_roles
- services_providers
- appointment_status_types
- working_plan_exceptions
- holidays

Each has:
- `tenant_id UUID NOT NULL REFERENCES tenants ON DELETE CASCADE` (immutable after insert)
- `tenant_branch_id UUID NULL REFERENCES tenant_branches ON DELETE SET NULL` (branch-scoped bookings, optional)

**E2E test results (2026-05-26 — all passing):**
| Scenario | Result |
|---|---|
| Tenant A booking wizard (`/book/e2e-portal`) | ✅ Full 4-step wizard → success |
| Tenant B booking wizard (`/book/tenant-b`) | ✅ Only B-Only-Service/Bob visible; no A data |
| Renamed-tenant slug (`/book/renamed-tenant`) | ✅ Resolves to Tenant C; empty but no crash |
| Admin backend session tenant (login → calendar) | ✅ Calendar/services/filter scoped to Tenant A only |
| Manage-mode / reschedule (`/booking/reschedule/{hash}`) | ✅ Pre-filled wizard, UPDATE button, DB updated |

**Bug fixed (2026-05-26):** `application/helpers/rate_limit_helper.php` — CI3's `CI_Cache::is_supported()` uses a `static $support` array; a second `load->driver('cache', ...)` call (triggered by `EA_Cache extends CI_Cache`) left `$this->file` uninitialised → crash on `->get()`. Fixed with a `static $cache_driver_loaded` guard so `load->driver()` is called at most once per request.

**Seed invariant:** `ea_settings` source row has `tenant_id = ''` (empty string), NOT `NULL` — EA installer uses empty string. `docker/seed_e2e.sql` uses `WHERE tenant_id = ''` accordingly.

### Polaris Dependencies (Phase 7 → 10)

| Phase | Service | Endpoint | Purpose |
|---|---|---|---|
| **7** | `services/booking/client.py` | `GET /index.php/api/v1/services`, `GET /index.php/api/v1/availabilities`, `POST /index.php/api/v1/appointments` | Fetch services/slots + create booking (CI3 front-controller prefix; tenant scope via Bearer JWT claim) |
| **7** | `services/booking/client.py` | `DELETE /index.php/api/v1/appointments/{id}`, `PUT /index.php/api/v1/appointments/{id}` | Cancel or reschedule booking |
| **8** (portal) | `components/bookings/BookingConfigForm.tsx` | Link to `calendar.getpolaris.my` (separate domain) | Tenants manage Polaris Calendar settings via native Calendar admin (no iframe at GA) |
| **8** (portal) | `routers/tenant_bookings.py` | `GET /api/tenant/bookings/config` → `tenant_branches.*.ea_provider_id + metadata.booking.*` | Portal dashboard shows read-only booking summary (via REST API) |
| **10** | — | — | Unchanged; no Phase 10 additions |
| **v1.0.67 (booking-confirm loop)** | `webhooks/ea_router.py` | `POST /v1/webhooks/ea` (EA → Polaris, inbound) | EA fork emits HMAC-signed `{polaris_ref, appointment_id, start_datetime, end_datetime, service_name}` after each successful booking that came via a bot link. Polaris verifies `X-Polaris-Signature` (ea_webhook_hmac_secret), validates `polaris_ref` binding token, flips `bookings` row → `confirmed`. EA config: `polaris_api_base_url` (env `POLARIS_API_BASE_URL`, default `https://api.getpolaris.my`). Set on ea-php-fpm container. |

### Public Booking Surface (ADR-013 Amendment 2026-05-16)

**KEPT and multi-tenanted** (reverses 2026-05-15 deletion plan):

- **URL:** `https://calendar.getpolaris.my/book/{tenant-slug}`
- **Root redirect:** `GET /` → `Home.php` (Polaris marketing stub, not Calendar admin)
- **Tenant lookup:** `tenants.slug` column (unique constraint required)
- **Bot integration:** `[booking]` action tag constructs URL: `https://calendar.getpolaris.my/book/{tenant_slug}`

**Spam protection (Batch E work — ongoing):**
- PDPA opt-in checkbox (Malaysia compliance)
- Cloudflare Turnstile CAPTCHA
- IP rate-limit: 10 bookings per hour per IP

**Batch E new endpoint** (Internal API for Polaris → EA sync):
- `POST /api/v1/internal/tenants/sync` (HMAC-SHA256 webhook from Polaris)
- Syncs tenant metadata (name, branding) → EA database

## 3. LLM Provider Integrations

### Anthropic (Primary)

**Models:**
- `claude-sonnet-5` — LLM replies, hard-tier (streaming, `thinking` disabled; config: `llm_sonnet_model`)
- `claude-haiku-4-5-20251001` — Intent classification + simple/medium replies (config: `llm_haiku_model`)

**Rate limits:** Per-model concurrency caps enforced in `services/llm_pool.py`

**Cost tracking:** Per-tenant monthly + platform daily spend fetched via Grafana Cloud Mimir (`services/grafana_cost.py`); cost-ceiling enforcement in `services/cost_ceiling.py` (Phase 10)

### OpenRouter (Fallback)

**Provider models:** `openai/gpt-4o-mini` (fallback if Anthropic fails; ADR-031 Amendment 2026-05-15)

**Key:** Stored in `tenant_secrets.openrouter_api_key` (KMS-encrypted)

**Tier-gating:** Available to Pro+ tenants only (ADR-057 cost reason)

### Cohere (Reranking)

**Model:** `rerank-v3.5` (multilingual; constant `_COHERE_MODEL` in `services/rag.py`)

**Use case:** Hybrid search results re-ranking (Phase 4 PR-5)

**Tier-gating:** Available to Pro+ tenants only

**Auto-disable:** Hard cost-ceiling breach (Phase 10 PR-4) disables Cohere reranking to reduce spend

### Groq (Voice Transcription) — voice-notes M1 ✅ (SHIPPED main, batch-merged 2026-06-06)

**Model:** `whisper-large-v3` (full, not -turbo — best multilingual EN/MS/ZH; config `transcription_model`)

**Endpoint:** OpenAI-compatible `POST https://api.groq.com/openai/v1/audio/transcriptions` — multipart form (`file`, `model`, `response_format=verbose_json`); `language` omitted → Whisper auto-detects and returns the detected language in the response. Thin httpx client (`services/transcription.py`), no `groq` SDK.

**Use case:** Inbound voice notes (WhatsApp/Instagram/Messenger/Telegram). Audio downloaded per-channel by `services/media_download.py`, transcribed, transcript substituted as the message text so the normal RAG pipeline replies in the spoken language. Transcript mirrored to Chatwoot as a `Voice Note: …` private note.

**Key:** Platform-wide env var `GROQ_API_KEY`. Placeholder/empty → `TranscriptionError` (fail-closed; no call to Groq with a junk key). `TRANSCRIPTION_STUB_ENABLED=true` returns a canned transcript for offline E2E.

**Tier-gating:** **Pro+ only** (`voice_note_tiers` frozenset; Starter customers get a "please type" reply + a `Voice Note is a Pro+ Feature` agent note).

### Meta WhatsApp API

**Endpoints:**
- `POST /messages` (OUTBOUND send — per-tenant WABA token)
- `POST /media/upload` (media messages)
- `POST /media/{media_id}` (media fetches, for knowledge ingestion)
- **Inbound:** Meta delivers to **Chatwoot** (the WABA webhook owner post-rehaul), NOT Polaris.
  Customer messages → Chatwoot agent-bot → `/v1/webhooks/chatwoot`. Non-message events
  (delivery/read/template/account) → Chatwoot `WhatsappEventsJob` → fork-relay → Polaris
  `wa_status` / `meta_template_status` / `wa_account` routers.

**Token storage:** `tenant_secrets.meta_waba_access_token` (KMS-encrypted; per-tenant send)

**Webhook signing:** Chatwoot verifies the Meta webhook; the fork-relay + agent-bot use
`CHATWOOT_WEBHOOK_HMAC_SECRET` to Polaris.

## 4. External Services Checklist

| Service | Phase | Secret | Webhook | Rate-limit | Notes |
|---|---|---|---|---|---|
| Chatwoot API (public) | 2–7 | `CHATWOOT_BOT_ACCESS_TOKEN` + `CHATWOOT_AGENT_API_TOKEN` | signed v4.14.0-r3 (timestamp + HMAC) | 100 req/s | Platform-wide env vars (bot token for most verbs; agent token for contacts/labels) |
| Chatwoot internal API | v1.0.19 | `CHATWOOT_INTERNAL_TOKEN` | — | — | NEW: v1.0.19 channel auto-provision. Polaris→Chatwoot-fork internal call (docker network). `GET /internal/inboxes/:id/token` returns channel token; `X-Polaris-Internal-Token` header auth (const-time); fails soft (None). |
| Chatwoot webhook sig | 2 | `CHATWOOT_WEBHOOK_HMAC_SECRET` | X-Chatwoot-Signature + X-Chatwoot-Timestamp | — | Platform-wide env var (not KMS). v1.0.18: supports sha256= prefix + timestamp replay window. |
| Chatwoot internal base URL | v1.0.19 | — | — | — | NEW: `CHATWOOT_INTERNAL_BASE_URL` (e.g. http://chatwoot-rails:3000 for docker; empty disables auto-provision) |
| Easy!Appointments API (Polaris → EA) | 7 | `ea_api_token` (Bearer JWT) | `POST /api/v1/internal/tenants/sync` | 50 req/s | HMAC-SHA256 (Batch E); tenant sync + provider/service list/create |
| EA → Polaris confirm callback | v1.0.67 | `EA_WEBHOOK_HMAC_SECRET` / `ea_webhook_hmac_secret` | `POST /v1/webhooks/ea` (EA → Polaris) | — | EA posts after each bot-link booking; HMAC `X-Polaris-Signature`; env `POLARIS_API_BASE_URL` on EA container sets destination |
| Anthropic API | 4 | `ANTHROPIC_API_KEY` | — | model-specific | Platform-wide secret |
| OpenRouter API | 4 | `openrouter_api_key` | — | tier-gated | KMS-encrypted; Pro+ only |
| Cohere API | 4 | `cohere_api_key` | — | tier-gated; auto-disable on cost-ceiling | KMS-encrypted; Pro+ only |
| Groq (Whisper) | voice-notes M1 | `GROQ_API_KEY` | — | tier-gated Pro+ | Platform-wide env var; placeholder→TranscriptionError fail-closed; inbound voice-note transcription |
| Meta WhatsApp API | 2–6 | `meta_access_token` | signed | 1000 msg/s | KMS-encrypted |
| Meta webhook verify | 2–6 | `meta_webhook_verify_token` | webhook HMAC | — | Plain (not KMS) |
| Supabase Auth | 1 | `SUPABASE_JWT_SECRET` | — | — | Platform-wide; 3-role RLS |
| Redis | all | — | — | — | Local dev + prod (TBD) |
| Grafana Cloud | 10 | `OTEL_HEADERS` | — | — | Optional; graceful degrade |
| Sentry | 10 | `SENTRY_DSN` | — | — | Optional; graceful degrade |

## 5. Rebase Cadence

| Trigger | Fork | Action | Owner |
|---|---|---|---|
| Upstream security patch (e.g. v4.13.1) | CW / EA | Rebase topic branches within 7 days; bump `-rN` suffix; push image | Operator |
| Upstream minor version (e.g. v4.14.0) | CW / EA | Evaluate breaking changes; rebase within 30 days; test against staging; bump version + `-r1` | Operator + Eng |
| Upstream major version (e.g. v5.0.0) | CW / EA | Create `-prep` branch; plan migration; may take a quarter | Operator + Eng |
| Polaris white-label change (new logo, new locale) | CW / EA | Bump `-rN` only; no upstream rebase | Operator |
| Polaris feature gate (e.g. disable feature X) | CW / EA | Add feature flag to `polaris/integration`; bump `-rN`; deploy new image | Eng |

**Maintenance owners:** Documented per fork in `docs/CW_Whitelabel/FORK_MAINTENANCE.md` + `docs/EA_Whitelabel/FORK_MAINTENANCE.md`.

## 6. Known Rebase Landmines

| Fork | Issue | Impact | Workaround |
|---|---|---|---|
| CW | Chatwoot core refactors agent inbox UI | Merge conflicts in `whitelabel` branch | Rebase `strip-features` first (biggest delete), then `whitelabel` |
| EA | `tenant_id` migration index position | If `tenant_id` index not last, performance cliff + isolation risk | Always re-apply `tenant_id` migration as final index in `polaris/integration` |
| EA | Slug-routing patch in `Appointments.php` | Public `/book/{slug}` path breaks on upstream refactor | Document patch location in `FORK_MAINTENANCE.md`; test public booking path post-rebase (Q-EA-6) |
| CW | `polaris/suspension-enforcement` branch | Account suspension enforcement may diverge from upstream auth flow | Keep branch **LOCKED** to current state; only merge via deliberate ADR-049 amendment |

## 7. CI Integration (Outside `polaris` Monorepo)

**Chatwoot fork CI** (at `polaris-cw-fork`):
- Linter (rubocop)
- Test suite (rspec)
- Docker build + push to GHCR on tag `polaris/*`

**EA fork CI** (at `polaris-ea-fork`):
- Linter (psalm, phpstan)
- E2E tests (Playwright)
- Docker build + push to GHCR on tag `polaris/*`

**Polaris monorepo CI** (at `polaris`):
- Does **NOT** build forks
- Consumes fork images from GHCR by tag
- `docker-compose.prod.yml` references images; `docker pull` on deploy

## 8. Deployment Workflow

```
1. Fork source change (e.g. white-label tweak)
   ↓
2. Topic branch updated + tested locally
   ↓
3. Merge to polaris/integration
   ↓
4. Tag: polaris/v4.14.0-r2 (or bump version if upstream change)
   ↓
5. GitHub Actions: docker build + push to ghcr.io/jiun01/polaris-cw:v4.14.0-r2
   ↓
6. Edit polaris/infra/docker-compose.prod.yml: bump image tag, commit + push
   ↓
7. Polaris CI: validate docker-compose syntax
   ↓
8. On VPS: ./infra/deploy/vps-update.sh (git pull → docker pull → recreate
   changed services; auto-refreshes the ea_app volume if the EA image changed)
```

**Deploy scripts:** `infra/deploy/vps-install.sh` (first-time) + `vps-update.sh`
(redeploy) — see `infra/deploy/README.md`. Hybrid model: forks pulled from GHCR,
Polaris-own (api/portal/admin) built on the box.

**Rollback:** Change tag back to prior `-rN` version; no Git revert needed (images are immutable by tag).

---

**Related codemaps:** [architecture.md](architecture.md) (fork intake), [backend.md](backend.md) (integration endpoints), [data.md](data.md) (schema + migrations).

**Reference docs:** `../docs/Polaris V2/10_FORK_INTEGRATION.md` (SSOT), `../docs/CW_Whitelabel/HANDOFF.md`, `../docs/EA_Whitelabel/HANDOFF.md`.
