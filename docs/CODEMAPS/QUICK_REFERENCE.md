
# Polaris Codemaps — Quick Reference

**Last Updated:** 2026-07-02 (migration-count correction + router/service-count refresh + 2026-07-02 audit-remediation batch note; rest of file is a historical snapshot pending full regen)
**Current Prod Image (historical, unverified this pass):** v1.0.39 (polaris-api, polaris-portal, polaris-admin); EA v1.6.0-r5 — see `docs/SSOT/` for current verified prod pins.
**Status (historical, unverified this pass):** DEPLOYED v1.0.39 — RAG remediation (hybrid OR-semantics + `vector_distance` confidence, mig 113); products + promos in default reply; opt-in escalation; `infra/ship.sh` release pipeline; `config.py` blank-bool coercion; KGD window param + /summary/examples; 9 portal QA fixes; EA r5.

## 📚 Documentation Files

| File | Purpose | Size | Read Time |
|------|---------|------|-----------|
| **[INDEX.md](INDEX.md)** | Phase timeline + status matrix + service overview (START HERE) | 15K | 5 min |
| **[SERVICES.md](SERVICES.md)** | All 45+ service modules with signatures | 22K | 7 min |
| **[SHIPPED_FEATURES.md](SHIPPED_FEATURES.md)** | Each shipped feature: entry points, migrations, tests, config | 21K | 6 min |
| **[migrations.md](migrations.md)** | All 120 migrations: purpose, constraints, indexes, RLS policies | 19K | 6 min |
| **[backend.md](backend.md)** | All 39 routers, 200+ endpoints, scheduled jobs (7 locked), pipeline steps | 72K | 12 min |
| **[architecture.md](architecture.md)** | Dual-pool RLS contract, E2E test infrastructure | 21K | 6 min |
| **[integrations.md](integrations.md)** | External service contracts (Chatwoot, EA, LLM, Meta, Telegram) | 20K | 6 min |
| **[data.md](data.md)** | Schema + FK relationships + invariants | 18K | 5 min |
| **[frontend.md](frontend.md)** | Portal + Admin app-router structure | 11K | 3 min |
| **[dependencies.md](dependencies.md)** | Pinned versions + PyJWT + pytest session loop | 9.2K | 2 min |
| **[admin.md](admin.md)** | Admin app specifics + E2E helpers | 19K | 5 min |
| **[portal.md](portal.md)** | Portal app + NextAuth magic-link + JWT proxy | 17K | 5 min |
| **[billing.md](billing.md)** | Invoices, quotes, MRR, cost tracking | 17K | 5 min |
| **[ea-fork.md](ea-fork.md)** | Easy!Appointments fork (v1.6.0-r5) | 9.2K | 3 min |

**Total:** ~292K documentation / ~80 min reading

---

## 🚀 First-Time Orientation (10 minutes)

1. **START:** Read **[INDEX.md](INDEX.md)** for phase timeline + status
2. **WHY:** Read **[SHIPPED_FEATURES.md](SHIPPED_FEATURES.md)** section for your area of interest
3. **WHERE:** Look up the file path in that section
4. **HOW:** Read the domain-specific codemap (backend, services, integrations, etc.)

---

## 🎯 Quick Lookup by Task

### "I need to add a new scheduled job"
→ `docs/CODEMAPS/backend.md` §Scheduled jobs summary  
→ `docs/CODEMAPS/migrations.md` §When to Add a New Migration  
→ File: `apps/api/polaris_api/scheduled/YOUR_JOB.py` + wrap in `scheduled/_locking.run_with_advisory_lock()`

### "How do the voice notes work?"
→ `docs/CODEMAPS/SHIPPED_FEATURES.md` §Voice Notes M1  
→ `docs/CODEMAPS/services.md` §Media & Attachments + Transcription  
→ `docs/CODEMAPS/backend.md` §Inbound voice notes — M1  
→ Files: `apps/api/polaris_api/services/{media_download,transcription}.py` (branch PR #84)

### "Where are the database migrations?"
→ Files: `packages/db-migrations/NNN_*.sql`

### "What endpoints exist for [feature]?"
→ `docs/CODEMAPS/backend.md` §Routes (39 routers, 200+ endpoints)  
→ Filter by router name (e.g., `tenant_products.py`)

### "How does MRR persistence work?"
→ `docs/CODEMAPS/SHIPPED_FEATURES.md` §MRR Persistence + Scheduler Locks  
→ `docs/CODEMAPS/services.md` §`mrr_rollup.py`  
→ `docs/CODEMAPS/backend.md` §Scheduled jobs summary  
→ Files: `apps/api/polaris_api/services/mrr_rollup.py` + `scheduled/_locking.py` + `scheduled/mrr_rollup_job.py`

### "What are the E2E test endpoints?"
→ `docs/CODEMAPS/backend.md` §Internal E2E (20+ endpoints)  
→ Files: `apps/api/polaris_api/e2e/router.py` (relocated from `routers/internal_e2e.py`, audit Batch E 2026-07-02 — same `/internal/e2e` mount)

### "Show me all Telegram integration points"
→ `docs/CODEMAPS/SHIPPED_FEATURES.md` §Off-Meta Channels: Telegram  
→ `docs/CODEMAPS/services.md` §Telegram (client, provision)  
→ `docs/CODEMAPS/integrations.md` §Telegram Bot API  
→ Files: `services/telegram_client.py` (outbound + getMe) + `services/channels/telegram_provision.py` (provision); inbound via `webhooks/chatwoot_router.py` (native Channel::Telegram — legacy `telegram_router.py` DELETED, uncommitted 2026-06-12)

### "How does the inbound pipeline work?"
→ Chatwoot-native (rehaul): Customer → Meta/Telegram → **Chatwoot inbox** → agent-bot `message_created` → `POST /v1/webhooks/chatwoot` → human-handling gate (`status=='pending'`) → resolve real channel/recipient → `pipeline.run` → reply DIRECT to Meta/Telegram + mirror to the linked CW conversation. `meta_router` is DELETED.  
→ `docs/CODEMAPS/architecture.md` §Data Flow — Inbound + `backend.md` §Pipeline + `services.md` §Core Pipeline  
→ Files: `apps/api/polaris_api/webhooks/chatwoot_router.py` + `services/pipeline.py`

### "What's the schema?"
→ `docs/CODEMAPS/data.md` (constraints, indexes, RLS policies)  
→ `docs/CODEMAPS/migrations.md` (all 120 migrations with CREATE TABLE/ALTER statements)  
→ Files: `packages/db-migrations/*.sql`

### "How does the RAG eval harness work?"
→ `docs/CODEMAPS/SHIPPED_FEATURES.md` §2026-07-03 Post-Remediation Chips  
→ `docs/CODEMAPS/services.md` §`rag_eval.py`  
→ Files: `apps/api/polaris_api/services/rag_eval.py` (scoring) + `apps/api/scripts/rag_eval.py` (CLI) + `apps/api/tests/eval/golden.jsonl` (fixture)

---

## 📊 Code Statistics

| Category | Count | Files |
|----------|-------|-------|
| **Services** | 55+ | `services/*.py` + `services/*/` (booking/ subpackage grew 12→19 files, pipeline split into 4 files — audit Batch E, 2026-07-02) |
| **2026-07-03 note** | — | 2 new service modules added since the last full regen (`services/rag_eval.py`, `services/channels/secret_kinds.py`) — folded into the existing "55+" headroom above, no count correction needed. |
| **Routers** | 42 | `routers/*.py` (verified count as of 2026-07-02: includes `admin_tenant_channels.py`/`admin_tenant_email.py` split from `admin_tenants.py`, `admin_demos.py`, `website_leads.py`) + `webhooks/*.py` + `e2e/router.py` (relocated from `routers/internal_e2e.py`) |
| **Scheduled Jobs** | 9 | `scheduled/*.py` (7 locked via ADR-062 + `ea_settings_sync.py` + NEW `reconcile_channels.py`, audit Batch A 2026-07-02) |
| **Database Tables** | 80+ | Schema in `migrations.md` |
| **API Endpoints** | 200+ | Listed in `backend.md` §Routes |
| **E2E Test Endpoints** | 20+ | Listed in `backend.md` §Internal E2E |
| **Frontend Pages** | 30+ | Portal + Admin app-router |
| **Tests** | 500+ | `apps/api/tests/` + `apps/{portal,admin}/e2e/` |

---

## 🚀 Production Deployment (v1.0.39 — LIVE, operator-confirmed 2026-06-15)

**Image Tags (all at GHCR):**
- `ghcr.io/jiun01/polaris-api:v1.0.39`
- `ghcr.io/jiun01/polaris-portal:v1.0.39`
- `ghcr.io/jiun01/polaris-admin:v1.0.39`
- Chatwoot fork: `ghcr.io/jiun01/polaris-cw:v4.14.2-r2` (cutover deployed 2026-06-13)
- EA fork: `ghcr.io/jiun01/polaris-ea:v1.6.0-r5` (r5 deployed 2026-06-14)

**Environment Settings (docker-compose.prod.yml):**
- `ENVIRONMENT: production` (changed from `prod`; supported in config.py since v1.0.15)
- **`SAFE_FETCH_ALLOW_PRIVATE_NETWORK=true`** on chatwoot-rails + chatwoot-sidekiq (PR #103; required for agent-bot webhook to internal polaris-api:8000)
- All services: `OTEL_RESOURCE_ATTRIBUTES: service.namespace=polaris` (polaris-api)
- Observability: `OTEL_ENDPOINT`, `OTEL_HEADERS`, `GRAFANA_CLOUD_MIMIR_URL`, `GRAFANA_CLOUD_MIMIR_TOKEN` (note: v1.0.31 cost query fix: MIMIR_URL must be `/api/prom` base, not `/push`), `SENTRY_DSN`, `SENTRY_PROJECT_URL`, `GRAFANA_DASHBOARD_URL`

**Critical Ops Gotcha (VPS):**
- ⚠️ `docker-compose up` without `--env-file` or via `bash vps-update.sh` causes env substitution to fall back to shell env → token 401 errors
- **Fix:** Always run `bash vps-update.sh` (sets `--env-file /opt/polaris/.env`) OR manually pass `--env-file` to docker-compose
- Variable substitution in docker-compose uses SHELL environment, NOT env_file (unless explicitly set via flag)

**Migration Status (verified 2026-07-02 by listing packages/db-migrations):**
- On main: 001–120 (all idempotent)
- Next: 121
- Cloud-apply-to-prod status for 001–119 not re-verified this pass — see `docs/SSOT/` for the currently-confirmed live prod pins.

---

## 🔍 Phase Status at a Glance

### ✅ Shipped (on main)

| Phase | Feature | Migration | Config | Tests |
|-------|---------|-----------|--------|-------|
| **15** | Product catalog | 089–090 | product_image_* | test_tenant_products.py |
| **13** | AI follow-ups | 091–092 | followup_* | test_followups.py |
| **10** | STOP/quota/observability | 085–088 + Grafana v1.0.15 | quota_*, stop_*, otel_*, sentry_* | test_quota.py, test_stop_keyword.py |
| **20** | Meta mock + broadcast E2E | — | meta_api_base_url | broadcast.spec.ts |
| **Off-Meta 1** | Telegram | 094 | telegram_enabled | test_telegram.py |
| **Off-Meta 2** | Website widget | 095–096 | widget_* | test_widget.py |
| **Observability** | Grafana Cloud + OTLP + Sentry | — | otel_*, grafana_*, sentry_* | (transparent; no new tests) |

### 🔄 In Review (branches)


### ✅ Recently Shipped (v1.0.27–v1.0.28)

| Feature | PR | Migration | Status |
|---------|-----|-----------|--------|
| Booking notifications | #97 | — | ✅ v1.0.27 |
| Cost metrics OTel dashboard | #98 | — | ✅ v1.0.28 |
| Agent tool surface (ADR-063) | #99 | 105 | ✅ v1.0.28 |
| HUMAN_AGENT Meta tag | #100 | — | ✅ v1.0.28 |
| Handoff + dispatch hardening | 6ebeb7f/9e41bf1 | 106 | ✅ v1.0.28 |

---

## 🔗 Cross-References

### By Tier-Gate

**Starter only:**
- STOP keyword (opt-out)
- DSAR (data export/delete)

**Pro+ only:**
- Product catalog
- Reranking (Cohere)
- Voice notes (M1)
- Telegram channel

**Ultra+ only:**
- Knowledge gap detection + dashboard
- Advanced admin features (cost dashboard, audit)

### By Channel

**WhatsApp:**
- Core pipeline, flow runtime, broadcasts, templates, bookings, reminders, follow-ups

**Instagram + Messenger:**
- Conversational send (Page token)
- Product catalog (image send)
- Human-agent reply forwarding is Chatwoot-native (phase-10: Polaris `_dispatch_agent_forward` deleted — re-sending double-sent on Messenger/IG)

**Telegram:**
- First-class channel; Pro+ gated; webhook integration; Bot API client

**Website Widget:**
- Read-only launcher; no attribution; deep links (t.me/wa.me)

**Email:**
- Chatwoot-native (no Polaris email stack)
- Bot-assist draft generation
- Branch: `v2/email-channel/assist-mvp`

### By Scheduled Job

All 7 jobs wrapped in `scheduled/_locking.run_with_advisory_lock()` (ADR-062):
1. **00:05 MYT** — `quota_flush` (Redis → DB)
2. **02:00 MYT** — `retention_sweep` (hard-delete) + `mrr_rollup` (persist)
3. **03:00 MYT** — `cost_ceiling_check` (alerting)
4. **04:00 MYT** — `audit_chain_verify` (hash integrity)
5. **09:00 MYT** — `invoice_overdue` (mark status)
6. **Mon 09:00 MYT** — `weekly_digest` (operator email)

---

## 🔐 Security & Compliance

### RLS (Row-Level Security)
- All tenant-local tables protected via policies
- `anon` role isolated per tenant via JWT sub
- `service` role BYPASSRLS (admin ops, scheduler)
- `platform_admin` role (admin-only routes)

### Secrets Management
- `POLARIS_KMS_KEY` → AES-256-GCM tenant_secrets encryption
- Rotation via `routers/admin_secrets.py`
- Phase 1 one-time setup; post-Phase-1 regeneration = **catastrophic data loss**

### Audit Trail
- `audit_logs` immutable append-only (hash-chaining)
- `admin_audit_logs` for platform admin actions
- Verified nightly via `audit_chain_verify` scheduler

### Webhook Signature Verification
- Meta: HMAC SHA256 + `X-Hub-Signature-256` header
- Chatwoot: HMAC SHA256 + `X-Chatwoot-API-Signature` header
- Telegram: Secret token via `X-Telegram-Bot-Api-Secret-Token` header
- EA: HMAC SHA256 + `X-Polaris-Signature` header

---

## 📖 Reading Path by Role

### Backend Engineer
1. INDEX → architecture → backend → services → migrations → data
2. Dive: scheduler, pipeline, flow runtime, booking lifecycle

### Frontend Engineer
1. INDEX → frontend → portal/admin
2. Dive: NextAuth, JWT proxy, app-router structure

### Product Manager
1. SHIPPED_FEATURES (overview of all shipped + in-review)
2. Dive: specific feature sections (Phase 15, voice, follow-ups, etc.)

### DevOps / Infrastructure
1. architecture → data → migrations
2. Dive: RLS policies, secrets, backup/restore (internal ops runbook, `../docs/internal-ops/RUNBOOK.md`)

### QA / E2E Tester
1. backend §Internal E2E endpoints
2. Dive: phase-checklist, E2E_TEST_SURFACE.md, Playwright test structure

---

## 🎓 Key Concepts

### Dual-Pool Architecture
- **ANON pool** (RLS enforced) → Tenant-scoped queries via JWT sub
- **SERVICE pool** (BYPASSRLS) → Admin ops, scheduler jobs, internal E2E

### Pipeline Steps (13 total)
0.5 (CW conv) → 1.0 (metadata) → 1.4 (audience) → 1.5 (STOP) → 1.6 (quota) → 1.7 (booking cmd) → 2.0 (intent) → 3.0 (history) → 4.0 (RAG) → 5.0 (generate) → 6.0 (dispatch) → 7.0 (save) → 8.0 (send) → 9.0 (event) → 10.0 (metadata) → 11.0 (audit) → 12.0 (telemetry) → 12a (CW mirror) → 13.0 (ACK)

### Advisory Locks (ADR-062)
Postgres session-level `pg_try_advisory_lock()` per scheduler job; prevents double-fire in multi-process/multi-replica deploys.

### Idempotency Patterns
- **E2E re-seeding:** NULL out `wa_message_id` before INSERT to avoid unique constraint
- **Migrations:** Always use `IF NOT EXISTS` / `IF NOT APPLIED` guards
- **Webhook handlers:** Verify `webhook_events.idempotency_key` before side-effects

---

## 📝 When to Update Codemaps

Regenerate the codemaps after:
- ✅ Phase completes
- ✅ Migration count changes by >2
- ✅ New service module added
- ✅ Scheduled job added/removed
- ✅ Router count changes by >3
- ✅ Major refactor moving files between layers
- ✅ New external service integrated (observability, payment gateways, etc.)
- ✅ Production deployment configuration changed significantly

Last updated: **2026-07-02** (audit-remediation batch: flow_runtime retirement, structural splits, pipeline hot-path work, migration 120, shared-types codegen fix; see `SHIPPED_FEATURES.md` for full detail. Prior: v1.0.28 agent surface + cost metrics + HUMAN_AGENT tag + dispatch hardening; v1.0.27 booking notifications)

