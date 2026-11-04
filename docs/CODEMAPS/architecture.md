
# Polaris — System Architecture

## Services (docker-compose.prod.yml)

```
Internet → Caddy (443)
  ├── portal.getpolaris.my   → polaris-portal:3000  (Next.js 14, tenant self-serve)
  ├── admin.getpolaris.my    → polaris-admin:3001   (Next.js 14, platform ops)
  ├── api.getpolaris.my      → polaris-api:8000     (FastAPI, all business logic)
  ├── chatwoot.getpolaris.my → chatwoot-rails:3000  (fork v4.14, human handoff)
  └── ea.getpolaris.my       → ea-nginx:80          (EasyAppointments fork)

polaris-api ──→ Supabase Cloud (Postgres 17, pgvector, RLS)
polaris-api ──→ Redis 7 (rate-limit counters, quota Redis keys, broadcast queue)
polaris-api ──→ Groq (Whisper transcription — voice notes M1)
polaris-api ──→ Anthropic API (Claude — LLM replies, email drafts, KGD)
polaris-api ──→ Meta Graph API (send only — inbound now via Chatwoot agent-bot; rehaul)
polaris-api ──→ Telegram Bot API (send; inbound via Chatwoot agent-bot)
polaris-api ──→ Chatwoot internal API (`/internal/inboxes/{id}/token`, v1.0.19 auto-provision)
chatwoot-rails ─→ polaris-api (agent-bot webhook = PRIMARY inbound; + fork-relay for non-message WA events)
polaris-api ──→ Grafana Cloud (Tempo: OTLP traces; Mimir: metrics + cost; Sentry: errors)
polaris-portal ──→ Sentry (frontend error tracking)
polaris-admin ──→ Sentry (frontend error tracking)
chatwoot-rails → chatwoot-postgres (separate volume: /var/lib/postgresql/data)
chatwoot-rails → chatwoot-redis
```

## Auth Boundaries

| Surface | Auth | Role |
|---|---|---|
| /api/tenant/* | portal JWT (NextAuth) | tenant_user |
| /api/admin/* | admin JWT (NextAuth) | platform_admin |
| /webhooks/* | HMAC signature | n/a (source-verified) |
| admin.getpolaris.my | Caddy basic-auth + magic-link | platform_admin |
| portal.getpolaris.my | magic-link (email) | tenant_user |

## Data Flow — Inbound Message (Chatwoot-native, rehaul PR #102)

```
Customer → Meta/Telegram → CHATWOOT (owns the channel inbox + conversation)
  → Chatwoot agent-bot fires message_created on EVERY inbound
  → POST /v1/webhooks/chatwoot  (chatwoot_router — the PRIMARY front door)
    → verify HMAC (X-Chatwoot-Signature)
    → human-handling gate: process ONLY if conversation.status=='pending' (bot-owned);
       open/resolved/snoozed = a human owns it → record 'human_handling', stay silent
    → resolve real channel + recipient: tenant_channels by inbox_id;
       recipient = conversation.contact_inbox.source_id (WA wa_id / PSID / chat id)
    → idempotency claim (chatwoot.<msg_id>) → enqueue IncomingMessage on the REAL provider
       (carries chatwoot_conversation_id/_account_id so the mirror LINKS, never creates)
  → pipeline.run (normalize → STOP / quota / booking gates
    → agent (try_run_agent, if agent_config.enabled) else classify→rag→generate
    [flow-runtime branch REMOVED 2026-07-02, audit Batch D — services/flow_runtime/
     deleted outright, zero prod usage at retirement; was already portal-UI-removed
     under ADR-063 well before this])
  → reply sent DIRECT to Meta/Telegram (per-tenant token) — NOT back through Chatwoot
  → mirror-post the bot reply to the LINKED Chatwoot conversation with source_id
     (prevents Chatwoot re-delivery + echo); bot-handling label applied (deferred, post-txn)

ECHO-DROP invariant: the bot's own mirror-posts return as outgoing + sender.type=='agent_bot'
  and are dropped (never re-forwarded/re-processed). A human agent's reply (outgoing +
  sender.type=='agent') is NO LONGER forwarded by Polaris — Chatwoot delivers it to the
  customer NATIVELY (phase-10: _dispatch_agent_forward DELETED — re-sending double-sent on
  Messenger/IG). Polaris only calls _record_email_agent_reply(inbox_id, payload) on a
  Chatwoot-native EMAIL inbox (email-draft acceptance tracking); non-email inboxes are a no-op.

meta_router.py is DELETED — there is no longer a direct Meta→Polaris inbound path.
Non-message Meta webhooks (delivery/read receipts, template-status, account/MM-Lite) reach
Polaris via a FORK-RELAY: a prepend module on Webhooks::WhatsappEventsJob in polaris-cw-fork
→ the existing wa_status / meta_template_status / wa_account routers (unchanged).
```

## Observability (v1.0.15 — live in production)

- **Traces:** OTLP (HTTP) → Grafana Cloud Tempo 
- **Metrics:** OTLP metrics exporter → Grafana Cloud Mimir; `OTEL_RESOURCE_ATTRIBUTES: service.namespace=polaris` set in docker-compose.prod.yml
- **Dashboards:** `polaris/infra/grafana-dashboards/polaris-overview.json` imported to Grafana Cloud
- **Errors:** Sentry (`SENTRY_DSN`) wired on all services (polaris-api, polaris-portal, polaris-admin)
- **Config:** `ENVIRONMENT: production` now supported in `config.py` (line ~425) alongside `local` / `staging` / `prod`

## Channel Architecture (Chatwoot-native, rehaul PR #102)

```
INBOUND: Customer → Meta/Telegram → Chatwoot inbox → agent-bot webhook → chatwoot_router
         → (status==pending gate) → pipeline → LLM/flow → reply DIRECT to Meta/Telegram
         └─→ mirror bot reply to the LINKED Chatwoot conversation (source_id, dedup)

REVERSE (human agent reply): Chatwoot agent reply (outgoing + sender.type=='agent')
         → Chatwoot delivers to the customer NATIVELY (phase-10: Polaris no longer forwards;
           _dispatch_agent_forward DELETED — re-sending double-sent on Messenger/IG)
         → Polaris only records email-draft acceptance on email inboxes
           (_record_email_agent_reply → email_assist.record_agent_reply)

NON-MESSAGE WA (status/template/account): Meta → Chatwoot WhatsappEventsJob
         → fork-relay prepend module → POST to Polaris → wa_status / meta_template_status /
           wa_account routers (unchanged)
```

**Channels:** WhatsApp, Messenger, Telegram, **instagram_login**, Email. Legacy FB-page
`instagram` is DROPPED (migration 109 narrowed the channel CHECKs). `_BOT_PROVIDERS` =
`{whatsapp, messenger, telegram, instagram_login}`.

**Mirror = LINK, not create (rehaul):** the agent-bot payload already carries the Chatwoot
conversation + account ids, so `pipeline._link_cw_conversation` persists them onto the
`conversations` row and reuses the existing CW conversation. The old `_ensure_cw_conversation`
(find-or-create) is gone — it produced a duplicate CW conversation alongside the one the
webhook fired from.

**Human-handling gate:** the agent-bot fires `message_created` on every inbound regardless of
who owns the conversation; `chatwoot_router.is_bot_owned` processes ONLY `status=='pending'`
(open/resolved/snoozed = human-owned → record `human_handling`, stay silent). Two labels track
state: **bot-handling** / **human-handling** (`services/chatwoot/label_sync.py`).

**Agent-bot auto-attach:** `services/chatwoot/agent_bot.py` attaches the shared platform
"Polaris Bot" to a new inbox on channel provision (post-txn, best-effort) so conversations start
`pending`. Backfill: `scripts/backfill_agent_bot_attach.py`.

**Per-tenant send (v1.0.21):** `pipeline._dispatch_reply` resolves per-tenant WA creds
(`tenant_channels.phone_number_id` + `tenant_secrets.meta_waba_access_token`); channel-owner
creds resolved via `_resolve_channel_owner()` for demo turns. `META_DEMO_*` fallback REMOVED (v1.0.45). Replies go direct to Meta/Telegram, never back through Chatwoot.

**`use_chatwoot_inbound` flag:** per-tenant toggle in `admin_tenants.py` — VESTIGIAL post-rehaul
(cutover is an all-or-nothing image swap; the Meta-router defer branch it gated is deleted).

**v1.0.22 (admin remove-channel):** `DELETE /api/admin/tenants/{id}/channels/{kind}` →
`deprovision_channel()` (row + captured secret, sibling-safe) + UI "Remove" button.

⚠️ **Cutover gate:** deploying rehaul main to the VPS without first switching the Meta webhook
target from Polaris to Chatwoot 404s all inbound. Follow `../docs/internal-ops/runbooks/chatwoot-native-cutover.md`
(clean-slate cutover, keep `.env`).

## Key Invariants

- Enterprise `bot_replies` seeds to **0** (custom contract — admin must set via Quota Override).
- `audit_logs` is **append-only** (ADR-030); hard-deletes use `admin_hard_delete_tenant()` SECURITY DEFINER fn (migration 101).
- All 7 ADR-062-locked nightly jobs hold advisory locks (scheduled/_locking.py); 2 additional non-ADR-062 jobs also run on a schedule (`ea_settings_sync.py` hourly, `reconcile_channels.py` NEW hourly self-heal, audit Batch A 2026-07-02) — see INDEX.md §Scheduled Jobs.
- **Flow runtime retired (audit Batch D, 2026-07-02):** `services/flow_runtime/` deleted outright (zero prod usage confirmed at retirement) along with its two routers and 22 test files. The pipeline's branch order is now `try_run_agent OR classify→rag→generate` — no third flow leg. Flow DB tables (`flow_definitions`/`flow_versions`/`flow_runs`/`flow_templates`) intentionally left in place; a DROP-table migration is a deliberate separate follow-up, not an oversight.
- **Service pool sized 4→6 (audit Batch B, 2026-07-02):** `db/pools.py::_SERVICE_MAX_SIZE` raised to 6 after scheduled-job connection-scoping tightening in the same batch increased concurrent short-lived service-pool demand from background jobs.
- **Shared HTTP client singleton (audit Batch B, 2026-07-02):** new `polaris_api/http_client.py` — one `httpx.AsyncClient` per process (lifespan-managed, mirrors the Redis singleton), adopted by `meta_whatsapp.py`/`chatwoot/client.py`/`booking/client.py`/`media_download.py`. Falls back to a fresh per-call client outside the lifespan (tests/scripts) — existing test mocks unaffected.
- **Prod ops gotcha:** `docker-compose up` without `--env-file` or `bash vps-update.sh` causes variable substitution (e.g. `${REDIS_PASSWORD}`) to fall back to shell env instead of `.env` file → token 401 errors. Always use `bash vps-update.sh` or `docker-compose --env-file /opt/polaris/.env ...`.
- **`polaris_service` password drift:** a manual `docker compose up -d polaris-api` skips the role-password self-heal that `vps-update.sh` runs → `InvalidPasswordError: polaris_service` (service/BYPASSRLS pool) → silently breaks secret writes, channel-add token auto-capture, and scheduled jobs (anon-pool pipeline still works). Fix: `bash vps-update.sh`.
- **Demo System v3 (ADR-066, mig 114):** text-gated single-account roleplay on the operator's live channels (the operator's dogfood tenant). A customer types a configured **demo code** → bot roleplays as a hidden `is_demo=true` brand tenant (own KB/persona via RLS) for a capped session; on cap/expiry → revert + CW lead-flag note. One-demo-ever per contact (90-day reset). Gateway = the channel-owning tenant (`incoming.tenant_id`), NOT hardcoded. `META_DEMO_*` config + meta_whatsapp demo-WABA client + `reply_dispatch.is_demo` branch are all REMOVED.
- **Demo tenant `000…001` (`polaris-internal`)** is seeded `is_demo=true` by migration 114 backfill. Demo hidden tenants carry their own KB/persona; their `is_demo` flag causes MRR/admin-tenant-list/admin-health to exclude them.
- **Inbound is Chatwoot-native:** `pipeline.run` still REJECTS `provider='chatwoot'` (a Chatwoot inbound = a human-agent reply, Phase 7's path). The bot pipeline always runs on the REAL provider resolved by `chatwoot_router`. The `_meta_extract.py` / `_demo_branch.py` parsers were deleted with `meta_router.py`.
