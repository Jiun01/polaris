
# Dependencies & External Services (Phase 10 - GA)

## Phase 10 additions

### Observability stack (Phase 10 PR-2)

**New pip dependencies (FastAPI)**:
- `sentry-sdk>=2.0,<3` — Error tracking + performance monitoring. `send_default_pii=False` per ADR-035. Lazy init in `observability.py` (DSN-gated).
- `opentelemetry-sdk>=1.20.0,<2` — OTel SDK runtime
- `opentelemetry-exporter-otlp-proto-http>=1.20.0,<2` — OTLP HTTP exporter (Grafana Cloud compatible)
- `opentelemetry-instrumentation-fastapi>=0.41b0` — FastAPI auto-instrumentation
- `opentelemetry-instrumentation-asyncpg>=0.41b0` — asyncpg tracing
- `opentelemetry-instrumentation-httpx>=0.41b0` — httpx tracing

**New env vars** (Phase 10 PR-2):
- `sentry_dsn` (empty → no-op; fastapi + portal + admin all respect)
- `otel_endpoint` (e.g. `https://otlp-gateway-prod-<region>.grafana.net/otlp`, empty → no-op)
- `otel_service_name` (default `polaris-api`)
- `otel_headers` (OTLP auth headers; empty string → none)
- `grafana_mimir_endpoint`, `grafana_mimir_key` (cost ceiling check only; empty → no cost alerts)

**Graceful degradation**:
- No DSN → Sentry init skipped (development)
- No OTLP endpoint → OTel init skipped (development)
- No Mimir endpoint → cost_ceiling_check logs info + exits early (no false alerts)

### Portal + Admin Sentry (Phase 10 PR-2)

**New Node deps**:
- `@sentry/nextjs@^8` — Sentry SDK for Next.js
- `instrumentation.ts` in portal + admin — calls `withSentryConfig` in `next.config.mjs`

---

# Dependencies & External Services (Complete)

Pin rationale per ADR-054 + Amendment 1 (`docs/Polaris V2/09_DECISION_LOG.md`).

## Python (uv workspace — `pyproject.toml` per member)

### Root deps + dev group

```toml
[dependency-groups]  # PEP 735
dev = [
  "pytest>=8,<9",
  "pytest-asyncio>=0.24,<0.30",
  "pytest-cov>=5,<7",
  "mypy>=2,<3",
  "ruff>=0.7,<1",
  "fakeredis>=2.26,<3",
  "freezegun>=1.5,<2",
  "pyyaml>=6,<7",
  "tzdata>=2025.1",
]
```

### `apps/api/pyproject.toml` (Phase 10 complete)

| Dep | Pin | Purpose |
|---|---|---|
| **ASGI + Framework** | | |
| fastapi | `>=0.135,<0.140` | HTTP framework |
| uvicorn[standard] | `>=0.47,<0.50` | ASGI server |
| **Validation + Config** | | |
| pydantic | `>=2.13,<3` | validation + JSON Schema |
| pydantic-settings | `>=2.6,<3` | env-var loader; empty-string OS vars scrubbed at import time |
| **Database + Caching** | | |
| asyncpg | `>=0.30,<0.40` | Postgres driver; custom SSL handling for Supabase pooler |
| redis | `>=5.2,<6` | redis-py; lifespan singleton, SETNX, conversation cache, tier cache, quota counters |
| **Crypto + Auth** | | |
| pynacl | `>=1.6,<2` | XChaCha20-Poly1305 AEAD for tenant_secrets vault |
| pyjwt | `>=2.10,<3` | HS256 JWT (Phase 1 TenantContext + Phase 9 impersonation cookie) |
| **HTTP + Webhooks** | | |
| httpx | `>=0.28,<0.30` | health probes + Phase 4 Meta WhatsApp client + Phase 9 Supabase Storage + Telegram Bot API + voice-note media download (`media_download.py`) + Groq Whisper transcription (`transcription.py`) — no provider SDKs, httpx reused throughout |
| **Logging** | | |
| python-json-logger | `>=2.0,<3` | structured JSON logs |
| **Dev Server** | | |
| watchfiles | `>=0.24,<2` | uvicorn `--reload` |
| **LLM + AI** | | |
| anthropic | `>=0.45,<1` | Haiku 4.5 + Sonnet 5 (was Sonnet 4.6, bumped 2026-07-02; thinking pinned off for hard tier) + prompt-cache `cache_control` |
| openai | `>=1.50,<2` | OpenRouter fallback + qwen/qwen3-embedding-8b via OpenRouter |
| cohere | `>=5.13,<6` | rerank-v3.5; tier-gated Ultra+ per ADR-026 Am 5th |
| **Observability (Phase 10 PR-2)** | | |
| sentry-sdk | `>=2.0,<3` | Error tracking + perf monitoring; `send_default_pii=False` |
| opentelemetry-sdk | `>=1.20.0,<2` | OTel SDK runtime |
| opentelemetry-exporter-otlp-proto-http | `>=1.20.0,<2` | OTLP HTTP→Grafana Cloud exporter |
| opentelemetry-instrumentation-fastapi | `>=0.41b0` | FastAPI tracing |
| opentelemetry-instrumentation-asyncpg | `>=0.41b0` | asyncpg tracing |
| opentelemetry-instrumentation-httpx | `>=0.41b0` | httpx tracing |
| **Scheduled Jobs (Phase 9+)** | | |
| apscheduler | `>=3.10,<4` | nightly quota flush, retention sweep, cost ceiling check, audit verification (Phase 9 PR-5 onwards) |
| **PDF + Templates (Phase 9 PR-7/9)** | | |
| weasyprint | `>=53,<60` | HTML→PDF (lazy import; invoices + sales quotes) |
| pydyf | `>=0.8,<0.9` | WeasyPrint PDF backend pin (pydyf 0.12 API change breaks render) |
| jinja2 | `>=3.1,<4` | Template engine (invoice.html.j2 + sales_quote.html.j2) |
| **Phone + Upload (Phase 6 PR-4)** | | |
| phonenumbers | `>=8.13,<10` | E.164 normalisation for audience CSV import |
| python-multipart | `>=0.0.18,<1` | multipart upload parser (FastAPI UploadFile) |
| **Email validation (Phase 9 PR-3)** | | |
| email-validator | `>=2.1,<3` | EmailStr validation on tenant admin invite |
| **Image (Phase 15)** | | |
| pillow | `>=10,<13` | Product-image downscale + re-encode on upload (≤1600px JPEG q80; lazy import in services/products/storage.py) |

### `apps/portal/package.json` (Phase 8 baseline + Phase 10 Sentry)

Core deps:
- `next@^14.2.15` (app-router)
- `react@^18.3.1`, `react-dom@^18.3.1`
- `next-auth@^4.24` (magic-link EmailProvider)
- `typescript@^5.5`

UI stack:
- `@radix-ui/react-*` (primitives)
- `tailwindcss@^3.4`, `class-variance-authority@^0.7`, `lucide-react@^1.16`
- `tailwind-merge@^3.6`, `tailwindcss-animate@^1.0.7`, `clsx@^2`
- `sonner@^2` (toast notifications)

Forms:
- `react-hook-form@^7.76`, `zod@^4.4`, `@hookform/resolvers@^5.2`

Flows + Canvas:
- `@xyflow/react@^12` (React Flow — node-based editor per PRD §10.7)
- `@dagrejs/dagre@^1` (auto-layout)
- `zustand@^5`, `zundo@^2.3` (flow state + undo)

DB + util:
- `pg@^8.13`, `nodemailer@^7`, `lru-cache@^11`

Observability (Phase 10 PR-2):
- `@sentry/nextjs@^8` — wrapped with `withSentryConfig()`

### `apps/admin/package.json` (Phase 9 + Phase 10 Sentry)

Mirrors portal baseline (minus flow canvas deps) + admin-specific:
- `next@^14.2.15`, `react@^18.3.1`, `next-auth@^4.24` (magic-link + IP-bind)
- `@radix-ui/*`, `tailwindcss@^3.4`, `lucide-react@^1.16`
- `tailwind-merge@^3.6`, `tailwindcss-animate@^1.0.7`, `clsx@^2`, `sonner@^2`
- `react-hook-form@^7.76`, `zod@^4.4`, `@hookform/resolvers@^5.2` (forms)
- `pg@^8.13`, `nodemailer@^7`, `lru-cache@^11`

Observability (Phase 10 PR-2):
- `@sentry/nextjs@^8`

---

## External services (Phase 10 - GA, v1.0.19)

### New in v1.0.19

**Chatwoot fork v4.14.0-r3 internal endpoints:**
- `CHATWOOT_INTERNAL_TOKEN` — Const-time auth header for `/internal/*` (Polaris-internal only; blocked by Caddy `Caddyfile.prod`)
- `CHATWOOT_INTERNAL_BASE_URL` — Docker-internal Chatwoot Rails base URL (e.g. `http://chatwoot-rails:3000`); empty disables auto-provision feature

No new **pip** or **npm** dependencies (uses existing `httpx`, per `services/chatwoot/client.py::get_channel_token()`).

| Service | Purpose | Contract | Env var | Graceful degrade |
|---|---|---|---|---|
| **Supabase** | Auth + DB + Storage | Session pooler IPv4, Free→Pro on trigger | `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`, `POLARIS_DB_URL_ANON`, `POLARIS_DB_URL_SERVICE` | PITR at Free = 24h RPO |
| **Redis** | Caching + counters + idempotency | `redis://` or `rediss://` TLS (prod) | `REDIS_URL`, `REDIS_PASSWORD` | Graceful degradation; /health still answers when down |
| **Anthropic API** | LLM inference (Haiku 4.5, Sonnet 5) | Streaming + prompt-cache headers | `ANTHROPIC_API_KEY` | Placeholder key → no-op (Phase 4 fallback to logging consumer) |
| **OpenRouter** | LLM fallback + embeddings | Gpt-4o-mini (uncached), qwen/qwen3-embedding-8b MRL-1024 | `OPENROUTER_API_KEY` | Fallback → logs; embeddings fallback → zero vector |
| **Cohere** | Hybrid search rerank | rerank-v3.5 (multilingual) | `COHERE_API_KEY` | Placeholder key → `CohereDisabledError` (tier-gated guard) |
| **Groq (Whisper)** | Inbound voice-note transcription, `whisper-large-v3` (EN/MS/ZH auto-detect; Pro+ only) — voice-notes M1, PR #84 | OpenAI-compatible multipart `POST /openai/v1/audio/transcriptions`, `verbose_json` | `GROQ_API_KEY` | Placeholder/empty key → `TranscriptionError` (fail-closed); `TRANSCRIPTION_STUB_ENABLED` for offline E2E; failure → polite "please type" fallback |
| **Meta Graph API** | Outbound send (per-tenant WABA token); message send + template lifecycle. **Inbound now via Chatwoot** (rehaul PR #102) — Meta delivers to Chatwoot, not Polaris | Send only; non-message events (delivery/template/account) arrive via the fork-relay | per-tenant `meta_waba_access_token` (tenant_secrets); `META_DEMO_*` fallback | rehaul: meta_router deleted |
| **Chatwoot (fork)** | **PRIMARY inbound** (agent-bot webhook) + conversation mirror/handoff/label sync + fork-relay for non-message WA events | Agent-Bot HMAC-signed webhooks (`message_created`), two-label sync, reverse-handoff | `CHATWOOT_BOT_ACCESS_TOKEN`, `CHATWOOT_AGENT_API_TOKEN`, `CHATWOOT_WEBHOOK_HMAC_SECRET` | Graceful handoff failure; boot-guard requires HMAC secret in non-local |
| **Easy!Appointments (fork)** | Booking node integration | Per-call HS256 JWT (tenant_id claim + ADR-024) | `EA_API_BASE_URL`, `EA_JWT_SIGNING_SECRET` | Booking node → `error` outcome; flow continues |
| **Sentry** | Error tracking + perf monitoring (Phase 10 PR-2; v1.0.15: active in prod) | HTTPS POST to DSN | `SENTRY_DSN` (empty → no-op); `SENTRY_PROJECT_URL` (display only) | No-op if DSN empty (development) |
| **Grafana Cloud Tempo** | OTel tracing (OTLP exporter; v1.0.15: flows confirmed) | OTLP HTTP POST to Tempo datasource | `OTEL_ENDPOINT` (e.g. https://otlp-gateway-prod-<region>.grafana.net/otlp), `OTEL_HEADERS` (OTLP auth headers; empty → no-op) | No-op if endpoint empty (development) |
| **Grafana Cloud Mimir** | Prometheus metrics + LLM cost dashboards (v1.0.15: active in prod) | OTLP metrics exporter + HTTP cost API | `GRAFANA_CLOUD_MIMIR_URL`, `GRAFANA_CLOUD_MIMIR_TOKEN` (empty → no cost alerts); `OTEL_RESOURCE_ATTRIBUTES: service.namespace=polaris` | No-op if endpoint empty; cost_ceiling_check logs info + exits |
| **Grafana Dashboard** | Interactive observability dashboards (v1.0.15: polaris-overview.json imported) | JSON import to Grafana Cloud; datasources = Tempo + Mimir | `GRAFANA_DASHBOARD_URL` (for links in alerts) | Display only; no functional impact if URL missing |

---

## Node.js (Portal + Admin)

- **Version**: 22 (LTS)
- **Package manager**: pnpm 9.12.0
- **Workspaces**: `apps/portal/`, `apps/admin/` (symlinked deps)

---

## Development environment

- **Python**: 3.12.x (uv managed via `.venv/`)
- **Docker**: Compose v2 (redis + api + portal + admin; MailHog for SMTP testing)
- **Postgres**: 17 (via Supabase Free tier for dev)
- **Make**: GNU make (Makefile orchestration)

## Prod infrastructure images (`infra/docker-compose.prod.yml`)

Deploy model is **GHCR-hybrid** (per ADR-060, scripts in `infra/deploy/`): the forks
are pulled from GHCR as baked images; the Polaris-own apps are built on the VPS.

| Service | Image | Pin | Source |
|---|---|---|---|
| Caddy (TLS + 5-domain proxy) | `caddy:2.8-alpine` | line | stock pull |
| Polaris Redis | `redis:7-alpine` | line | stock pull |
| Chatwoot Redis | `redis:7-alpine` | line | stock pull (was `redis:alpine`) |
| Chatwoot Postgres | `pgvector/pgvector:pg16` | major line | stock pull |
| EA nginx | `nginx:1.27-alpine` | line | stock pull (bumped from 1.23.3) |
| EA MySQL | `mysql:8.0` | line | stock pull |
| Chatwoot Rails | `ghcr.io/jiun01/polaris-cw:v4.14.0-r1` | exact | GHCR (pulled) |
| Chatwoot Sidekiq | `ghcr.io/jiun01/polaris-cw:v4.14.0-r1` | exact | GHCR (pulled, same image) |
| EA php-fpm | `ghcr.io/jiun01/polaris-ea:v1.6.0-r5` | exact | GHCR (pulled) |
| api / portal / admin | `ghcr.io/jiun01/polaris-{api,portal,admin}:v1.0.39` | exact | GHCR (pulled) |

App base images: `python:3.12-slim` (api), `node:22-slim` (portal/admin). Forks +
nginx pinned exact/line; `@sha256` digest-pinning deferred (needs Docker up to read).

## CI/CD

- **GitHub Actions** (`.github/workflows/`):
  - `pr-check.yml`: lint + mypy + pytest + coverage (Python)
  - `migration-check.yml`: dry-run `apply.py` against postgres:17 service
  - `e2e.yml`: full-stack Playwright E2E (portal + admin); nightly at 18:05 UTC + `workflow_dispatch`; requires `E2E_DOTENV` repo secret; CW/EA forks not booted in CI
- Local review skills run pre-PR (security/python/fastapi review, simplify, quality gate).

---

## Production Status (v1.0.39 DEPLOYED + LIVE, operator-confirmed 2026-06-15)

**Current (HISTORICAL SNAPSHOT, unverified this pass):** Prod was running v1.0.39 as of 2026-06-15 operator-confirmation — RAG remediation (hybrid OR-semantics + `vector_distance` confidence, migration 113); products + active promos in default bot reply; opt-in escalation; Cohere no-crash degrade; confidence-based KGD; em-dash sanitizer; STOP opt-out hardened; `infra/ship.sh` canonical release pipeline; `config.py` blank-bool coercion; KGD window param + /summary/examples drill-down; KnowledgeGapsList client island; 9 portal QA fixes; EA r5. Images at GHCR (as of that snapshot): `polaris-{api,portal,admin}:v1.0.39`, `polaris-ea:v1.6.0-r5`. Migrations then: **001–113 applied + live**; next = 114. **main has advanced significantly since** (the 6-batch audit-remediation program + this pass's 4 follow-up commits) and is NOT yet reimaged/redeployed — see `docs/SSOT/` for the currently-verified live prod pins.

**Release pipeline (v1.0.39+):** `infra/ship.sh` is the canonical release path — replaces manual `infra/build.sh --push`. Subcommands: `preflight → gitsync → build → release` (or `all`). Drives parallel `docker buildx bake` via `infra/docker-bake.hcl`, auto-bumps image tags, bumps `docker-compose.prod.yml` pins, prints the VPS deploy line. Stops before VPS deploy (operator runs `vps-update.sh` manually).

**Previously (v1.0.31, 2026-06-13):** SAFE_FETCH flag, quota_flush orphan-skip, Mimir cost-query fix, `messages.metadata` grant (mig 110), meta structured error logging, agent-reply transcript persistence (#108), systemic JSONB double-encode fixes + repairs (migs 111/112).

---

## Durable Gotchas (v1.0.30 onwards)

### 1. JSONB Codec Double-Encoding (PR #110 systemic fix, migration 112)

**Problem:** Both anon AND service asyncpg pools register a JSONB codec (`encoder=json.dumps`) in `db/pools.py::_init_conn`. Calling `json.dumps(x)` before passing to a `$N::jsonb` SQL param double-encodes — the codec applies another `json.dumps`, wrapping the string in a JSONB string literal. This breaks `col->>'key'` filters and corrupts stored data.

**ALWAYS do this:** Pass Python dict/list directly to `::jsonb` params. The codec is transparent.
```python
# ✅ CORRECT
conn.execute("UPDATE table SET data=$1::jsonb", {"key": "value"})

# ❌ WRONG — double-encodes to a jsonb string
conn.execute("UPDATE table SET data=$1::jsonb", json.dumps({"key": "value"}))
```

**Canonical example:** `services/audit.py:151-153` (audit row INSERT with metadata JSONB).

**Historic read guards:** Readers have intentional fallback `if isinstance(raw, str): json.loads(...)` — left in place to tolerate older data, now redundant post-fix but safe to keep.

**Scope:** Fixed across 14 writer files in PR #110 (audit, audience, conversation, dispatch, pipeline, chatwoot, billing, scheduling, etc.). Migrations 111–112 repair corrupted rows.

### 2. SAFE_FETCH_ALLOW_PRIVATE_NETWORK on Chatwoot (PR #103, v4.14.2+)

**Problem:** Chatwoot v4.14.2's `ssrf_filter` blocks private-IP webhook targets by default. Agent-bot webhook to internal `polaris-api:8000` silently fails → message dropped + conversation flipped open.

**Required:** Set `SAFE_FETCH_ALLOW_PRIVATE_NETWORK=true` on BOTH `chatwoot-rails` AND `chatwoot-sidekiq` containers in `infra/docker-compose.prod.yml`. This flag is permanent; failing to set it renders inbound completely silent.

**Symptom:** Messages arrive in Chatwoot inbox but never reach Polaris; no error logs (the request is blocked before reaching app).

### 3. Mimir Cost Query URL Must Be Query Base, Not Push (PR #105, v1.0.31)

**Problem:** Cost dashboard 404s on every query if `GRAFANA_CLOUD_MIMIR_URL` is set to the remote-write `/push` endpoint instead of the query base.

**Config fix:**
- `GRAFANA_CLOUD_MIMIR_URL` = `https://prometheus-blocks-prod-us-central-1.grafana.net/api/prom` (NOT `/push`)
- `GRAFANA_CLOUD_MIMIR_TOKEN` = `base64(instanceID:token)` with **metrics:read** scope (not metrics:write)

**Diagnosis:** Cost ceiling check logs `_normalize_mimir_base detected: 404 from {host}/push — expected query base .../api/prom. Check config.` Self-diagnosing; also hints at 401 for auth issues.

### 4. Telegram Native Chatwoot (v1.0.31)

**Status:** Telegram is now a native Chatwoot `Channel::Telegram` — inbound rides the agent-bot path (`resolve_chatwoot` admits channel_kind 'telegram'). Legacy own-webhook path DELETED (`telegram_router.py`, `resolve_telegram`, `_telegram_extract.py`).

**Connect:** `connect_telegram` (channel provision) no longer self-sets webhooks. Calling `register_telegram_webhook` → `validate_telegram_bot` (getMe-only, no setWebhook); `telegram_webhook_secret` now NULL (Chatwoot owns the webhook).

**Inbound:** Customer messages → Telegram → Chatwoot → agent-bot → `/v1/webhooks/chatwoot` → pipeline. **No direct Telegram→Polaris path.** Mirror + send work unchanged (direct via bot token).

**Gotcha:** Old Telegram channels with custom webhook secrets are deprecated. Reconfigure via the admin Channels UI (Chatwoot-native provision).

### 5. Messenger Bot Direct-Send 400 = Expired Page Token

**Problem:** When a Messenger (or IG) bot-reply send fails with 400 code (code=190: "Existing token has expired"), the reply quota under-counts and bot-sent images don't deliver.

**Root cause:** `meta_fb_page_access_token` in `tenant_secrets` is short-lived. Chatwoot's native-send mirror (source_id=None, fallback) still reaches the customer, so bot didn't silence entirely.

**Diagnosis (v1.0.31):** `services/meta_messaging.py` now logs structured error fields (code/error_subcode/type/fbtrace_id). Code=190 is visible immediately.

**Fix:** Provision a long-lived Page token (rotate in admin).

### 6. Instagram Human-Agent Replies Disabled (v1.0.31)

**Status:** `ENABLE_INSTAGRAM_CHANNEL_HUMAN_AGENT` flag was disabled post-cutover (Meta hasn't approved the Human Agent feature for Instagram → error code 10).

**Messenger:** Flag is ON (approved). Within-24h RESPONSE tag applies automatically.

**IG fallback:** Non-approved human-agent mode → replies send as bare RESPONSE (no HUMAN_AGENT tag). The fork applies HUMAN_AGENT tag unconditionally when the flag is on (no 24h window check) — `app/services/instagram/...merge_human_agent_tag` — but this fails on IG due to Meta's approval gate.

**Fix:** Wait for Meta to approve the feature, or switch to Messenger for early users.

### 7. Telegram Native Chat ID (v1.0.31)

**Problem:** Chatwoot's native Telegram send reads `chat_id` from `conversation.additional_attributes['chat_id']` (NOT `contact_inbox.source_id`). Legacy conversations missing this field fail "chat_id is empty" until backfilled.

**Diagnosis:** Check `chatwoot_conversations.metadata` for presence of `chat_id` in the Chatwoot UI (admin → conversations). If absent, the channel wasn't native Chatwoot when the conversation was created.

**Fix:** Backfill script TBD; for now, reconfigure channel (forces fresh conversation) or manually add chat_id to metadata.

### 8. polaris_service Role Password Drift (v1.0.31)

**Problem:** Supabase periodically resets the custom-role passwords → `InvalidPasswordError` on fresh connects. The `vps-update.sh` deployment script includes a step to re-sync passwords automatically.

**Symptom:** Nighttime job hangs or fails with auth error; manual `make migrate` works (uses SERVICE_KEY), but scheduled worker fails.

**Fix:** Automatic — `vps-update.sh` step 4b (`sync_role_passwords`) re-heals on every deploy. Also the trigger for Supabase auto IP-bans (multiple failed auth attempts).

**Preventive:** Never commit `polaris_service` password to repo; only `POLARIS_DB_URL_SERVICE` env var (with the password embedded).
