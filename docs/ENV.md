
# Environment Variables

Copy `.env.example` → `.env` and fill in real values. `.env` is gitignored.

Phase 0 → 7 vars documented below. Phase 9 adds admin audit + impersonation config.

## Required vs optional

"Required" here means: the app refuses to boot OR a key feature fails at runtime without it.

| Variable | Required | Format / valid values | Notes |
|---|---|---|---|
| `SUPABASE_URL` | optional | `https://<project-ref>.supabase.co` | REST API URL. NOT the Postgres DSN. |
| `SUPABASE_ANON_KEY` | optional | JWT (`eyJ...`) | Public anon key. |
| `SUPABASE_SERVICE_KEY` | optional in dev | JWT (`eyJ...`) | Service-role JWT. Lock to `service_pool` only. |
| `SUPABASE_DB_URL` | **yes** for `apply.py` | `postgresql://...` | Raw Postgres DSN for migrations. Free tier: use Session pooler (IPv4); Direct connection is IPv6-only. |
| `POLARIS_DATABASE_URL` | optional | `postgresql://...` | Overrides `SUPABASE_DB_URL` for CI + local docker. |
| `POLARIS_DB_URL_ANON` | **yes** in Phase 1+ | `postgresql://...` | asyncpg DSN for RLS-bound role. Set post-migration-010. **Supabase Free tier: use Session pooler (IPv4), not Direct (IPv6-only).** Password changes synced via `sync_role_passwords.py` — see ../docs/internal-ops/RUNBOOK.md §2a. |
| `POLARIS_DB_URL_SERVICE` | **yes** in Phase 1+ | `postgresql://...` | asyncpg DSN for BYPASSRLS role. Same pooler format as `_ANON`. |
| `JWT_SECRET` | **yes** in Phase 1+ | URL-safe base64 (≥48 chars) | HS256 shared secret. Real value never in `.env.example`. |
| `JWT_ALGORITHM` | optional | `HS256` | Only valid value at GA. |
| `JWT_ISSUER` | optional | string (default `polaris-portal`) | Expected issuer claim. |
| `JWT_LEEWAY_SECONDS` | optional | integer (default `10`) | Clock-skew tolerance. |
| `REDIS_URL` | **yes** in Phase 2+ | `redis://[user]:<pw>@<host>:<port>` | Lifespan-managed singleton. Webhook idempotency + Phase 4 conversation cache depend on it. |
| `REDIS_PASSWORD` | optional (compose-only) | string | Hard-coded in compose for local. |
| `POLARIS_KMS_KEY` | **yes** in staging/prod | 32 bytes hex (64 chars) | XChaCha20-Poly1305 key. Boot gate refuses non-local envs without it. **Regenerating post-Phase-1 = catastrophic.** |
| `LOG_LEVEL` | optional | `DEBUG \| INFO \| WARNING \| ERROR` | Default `INFO`. |
| `ENVIRONMENT` | optional | `local \| staging \| prod` | Controls KMS-key + Meta secret boot gates. |
| `META_APP_SECRET` | **yes** in Phase 2+ | string (HMAC key) | Platform-level HMAC for Meta `X-Hub-Signature-256`. Fails closed on empty. |
| `META_WEBHOOK_VERIFY_TOKEN` | **yes** in Phase 2+ | random URL-safe string | Subscribe-handshake token. Fails closed (403) on empty. **Must equal the Chatwoot fork's `POLARIS_META_WEBHOOK_VERIFY_TOKEN`** (the fork registers it on the WABA so Meta's GET handshake to `meta_router` verifies). |
| `META_GRAPH_API_VERSION` | optional | `v<major>.0` (default `v21.0`) | Pinned Graph API version. |
| `CHATWOOT_WEBHOOK_HMAC_SECRET` | **yes** for `/v1/webhooks/chatwoot` | string (HMAC key) | Bare-hex signature (no `sha256=` prefix). Fails closed on empty. |
| **`ANTHROPIC_API_KEY`** | **yes** for Phase 4 PR-1 | `sk-ant-...` | Primary LLM provider (Sonnet 5 + Haiku 4.5 per ADR-031). Lazy-validated by `_get_anthropic_client`; `placeholder-` prefix raises `LLMConfigError` before any SDK call. |
| **`OPENROUTER_API_KEY`** | **yes** for Phase 4 PR-1 fallback + PR-6 embeddings | `sk-or-...` | Two uses: (1) `services/llm.py` fallback to `openai/gpt-4o-mini` uncached on Anthropic 5xx/timeout/rate-limit per ADR-031 Am 2026-05-15; (2) `services/embed.py` for query-side embeddings via `qwen/qwen3-embedding-8b` MRL-1024 per ADR-059. |
| **`COHERE_API_KEY`** | optional until first Ultra+ tenant | `co-...` | Rerank-only, tier-gated to Ultra+ per ADR-026 Amendment 5th. Safe to leave as placeholder for Phase 4 dogfood (Starter-only paths never call Cohere — asserted by `test_starter_with_placeholder_cohere_key_works`). |
| **`LLM_HAIKU_MODEL`** | optional | model id (default `claude-haiku-4-5-20251001`) | Bump only after auditing the Anthropic API contract for breaking changes. |
| **`LLM_SONNET_MODEL`** | optional | model id (default `claude-sonnet-5`) | Same audit gate as above. Sonnet 5 defaults adaptive thinking ON; the hard-tier call pins `thinking={"type":"disabled"}` to hold the pre-bump (non-thinking) reply behaviour the tiers were tuned against. |
| **`LLM_MAX_TOKENS_SIMPLE`** | optional | int (default `500`) | Haiku simple-tier cap per ADR-031 Amendment 2026-05-15. |
| **`LLM_MAX_TOKENS_MEDIUM`** | optional | int (default `800`) | Haiku medium-tier cap. |
| **`LLM_MAX_TOKENS_HARD`** | optional | int (default `1000`) | Sonnet hard-tier cap (streamed). |
| **`LLM_CACHE_TTL`** | optional | `5m` \| `1h` (default `5m`) | Anthropic prompt-cache TTL on system prompt block. Revisit after PR-6 demo telemetry. |
| **`CONVERSATION_HISTORY_MAX_MESSAGES`** | optional | int (default `20`) | Window cap per ADR-031; loader trims to FIRST limit hit. |
| **`CONVERSATION_HISTORY_MAX_TOKENS`** | optional | int (default `8000`) | Companion token cap. |
| **`EMBEDDING_MODEL`** | optional | model id (default `qwen/qwen3-embedding-8b`) | Query-side embedding via OpenRouter, MRL-truncated to 1024. Locked by ADR-059. MUST match the desktop ingestion app's `EMBEDDING_MODEL` (Polaris Ingestion System V2) for vector parity — drift produces silently-wrong cosine scores. |
| **`FLOW_RUNTIME_MAX_NODES_PER_RUN`** | optional | int (default `500`) | *Phase 5* Flow definition validator hard cap. Definitions exceeding this abort with `FLOW_VALIDATION_FAILED`. |
| **`FLOW_RUNTIME_MAX_TRANSITIONS_PER_RUN`** | optional | int (default `200`) | *Phase 5* Flow validator runaway-graph guard — caps state-machine transitions to prevent infinite loops during runtime. |
| **`FLOW_RUNTIME_STEP_TIMEOUT_SEC`** | optional | int (default `30`) | *Phase 5* Per-node hard timeout (seconds). Outer kill-switch for misbehaving custom nodes (v2.1+ `http_call`/`webhook_emit`); LLM/RAG nodes inherit Phase 4's own SDK budgets. |
| **`FLOW_RUNTIME_DEFINITION_MAX_KB`** | optional | int (default `512`) | *Phase 5* JSONB size cap (KB) for `flow_versions.definition` column. Mirrors Postgres CHECK constraint in migration 033; drift checked by `test_flow_runtime_size_caps_match_check`. |
| **`FLOW_RUNTIME_VARS_MAX_KB`** | optional | int (default `32`) | *Phase 5* RunContext vars size cap (KB) per step. Mirrors migration 035 CHECK constraint; prevents unbounded context bloat. |
| **`FLOW_RUNTIME_SIMULATOR_DRY_RUN`** | optional | bool (default `true`) | *Phase 5* Simulator default dispatch mode. `true` = LLM/RAG calls cost real tokens but Meta/Chatwoot dispatch is suppressed; per-request `dry_run: false` overrides for full integration testing. |
| **`FLOW_TEMPLATES_SEED_ON_MIGRATE`** | optional | bool (default `true`) | *Phase 5* Seed gate for 4 starter flow templates (migration 038). Set `false` for test databases; real ops leaves at `true`. |
| **`LLM_STUB_ENABLED`** | optional (test/E2E only) | bool (default empty/`false`) | Deterministic LLM stub for offline/CI E2E runs — bypasses real Anthropic/OpenRouter calls. **Leave empty/false in staging + prod.** Note: an empty value (`LLM_STUB_ENABLED=`) trips pydantic bool-parsing on test boot; set explicit `false` or override per-run. |
| **`COST_STUB_ENABLED`** | optional (test/E2E only) | bool (default `false`) | Deterministic cost stub for the admin `/cost` endpoints (`services/grafana_cost._stub_cost`, wired via `admin_cost._fetch_cost`). When `true`, returns month-aware, band-spread (green/amber/red) fixture data for the seeded tenants instead of Grafana-Mimir zeros (no live Mimir locally); future months still resolve `unavailable` so the empty-state branch stays reachable. **Leave empty/false in staging + prod** (else `/cost` shows fake data). |

## Categorization

### Must be in `.env` (real values) — never in `.env.example`

These contain real secrets:

- `SUPABASE_SERVICE_KEY`
- `SUPABASE_DB_URL` — has a placeholder template; real password only in `.env`
- `REDIS_PASSWORD`
- `POLARIS_KMS_KEY`
- `META_APP_SECRET`
- `META_WEBHOOK_VERIFY_TOKEN` (placeholder shown; real in `.env`)
- `CHATWOOT_WEBHOOK_HMAC_SECRET`
- `CHATWOOT_BOT_ACCESS_TOKEN` (platform AgentBot token — same for all tenants per ADR-058)
- `CHATWOOT_AGENT_API_TOKEN` (user/agent token — Phase 12; leakage = full CW agent access)
- `ANTHROPIC_API_KEY` / `OPENROUTER_API_KEY` / `COHERE_API_KEY` (provider secrets — placeholders in `.env.example`, real values in `.env`)

### Safe in `.env.example` with placeholder values

- `SUPABASE_URL` / `SUPABASE_ANON_KEY` (public per-project; RLS enforces tenancy)
- `REDIS_URL` (with placeholder password segment)
- `LOG_LEVEL` / `ENVIRONMENT`
- `META_GRAPH_API_VERSION` (non-secret pin)
- `LLM_*_MODEL` / `LLM_MAX_TOKENS_*` / `LLM_CACHE_TTL` / `CONVERSATION_HISTORY_*` / `EMBEDDING_MODEL` (non-secret config)

## How env vars get loaded

| Loader | Reads from | Used by |
|---|---|---|
| `uv run --env-file .env <cmd>` | `.env` | Manual one-shots (`make migrate`, `apply.py`) |
| `pydantic_settings.BaseSettings(env_file=".env")` | `.env` | `apps/api/polaris_api/config.py` (FastAPI lazy singleton via `get_settings()`) |
| `docker compose` | `infra/docker-compose.yml` `environment:` block | `make dev` containers |
| GitHub Actions | repo secrets via `Settings → Secrets → Actions` | CI workflows |

### Precedence and the empty-string footgun (Phase 4)

`.env` is the source of truth. At module import, `config.py::_scrub_empty_env_vars()` deletes any OS env vars set to the empty string, so `.env` values always win.

Example: PowerShell `$env:ANTHROPIC_API_KEY=""` (or `export X=""` in bash) would normally shadow `.env` under pydantic-settings' default precedence. After the scrub, it doesn't — `.env` takes effect, and tests using `monkeypatch.setenv("X", "non-empty-value")` still work because non-empty OS env values are preserved.

This trades pydantic's upstream default for predictable single-source-of-truth behavior: "`.env` controls everything; the shell doesn't sneak in empty overrides."

## Phase history

**Phase 0** (foundation): Supabase basics, Redis, KMS, log/env.

**Phase 1** (RLS + auth): `POLARIS_DB_URL_ANON` / `POLARIS_DB_URL_SERVICE`, `JWT_*`.

**Phase 2** (webhooks): `META_APP_SECRET`, `META_WEBHOOK_VERIFY_TOKEN`, `META_GRAPH_API_VERSION`, `CHATWOOT_WEBHOOK_HMAC_SECRET`.

**Phase 3** (knowledge ingestion API): no API-side env vars per the existing `.env.example` comment block — ingestion defaults (crawl depth/pages, OCR languages, chunk shape) are owned by the desktop project at `../Polaris Ingestion System/` per ADR-057. Per-request overrides land on the `ingestion_requests` row from the tenant's POST body.

**Phase 4** (LLM pipeline) added:
- `ANTHROPIC_API_KEY` / `OPENROUTER_API_KEY` / `COHERE_API_KEY` — LLM provider secrets
- `LLM_HAIKU_MODEL` / `LLM_SONNET_MODEL` — model pins (env-overridable per `claude-api` skill convention)
- `LLM_MAX_TOKENS_SIMPLE` / `_MEDIUM` / `_HARD` — 3-tier routing per ADR-031 Amendment
- `LLM_CACHE_TTL` — prompt-cache TTL knob
- `CONVERSATION_HISTORY_MAX_MESSAGES` / `_MAX_TOKENS` — conversation window per ADR-031
- `EMBEDDING_MODEL` — query-side embedding model (must match desktop ingest for vector parity)

**Phase 5** (flow builder runtime) added:
- `FLOW_RUNTIME_MAX_NODES_PER_RUN` / `FLOW_RUNTIME_MAX_TRANSITIONS_PER_RUN` — validator hard caps
- `FLOW_RUNTIME_STEP_TIMEOUT_SEC` — per-node timeout (seconds)
- `FLOW_RUNTIME_DEFINITION_MAX_KB` / `FLOW_RUNTIME_VARS_MAX_KB` — JSONB size caps (mirrors SQL CHECK constraints)
- `FLOW_RUNTIME_SIMULATOR_DRY_RUN` — simulator dispatch suppression gate
- `FLOW_TEMPLATES_SEED_ON_MIGRATE` — starter template seeding gate (migration 038)

**Demo System v3 (ADR-066, migration 114, app v1.0.45)** removed three env vars that were load-bearing for the old shared-WABA demo dispatch (Phase 4 PR-6 pattern):
- ~~`META_DEMO_PHONE_NUMBER_ID`~~ — **REMOVED**. Demo sessions now route through the operator's live channels (the operator's dogfood tenant); no dedicated demo number.
- ~~`META_DEMO_WABA_ID`~~ — **REMOVED**. Same reason.
- ~~`META_DEMO_WABA_ACCESS_TOKEN`~~ — **REMOVED**. Demo dispatch now uses the gateway channel owner's per-tenant creds resolved by `_resolve_channel_owner`; no separate system-user token needed. The demo KB is populated by impersonating the hidden demo tenant in the portal (or direct KB ingest by `tenant_id`). Next migration = 115.

**Phase 6** (outbound — templates + broadcast + audience + MM Lite) added (per [.env.example](../.env.example) §Phase 6):
- `BROADCAST_TICK_SECONDS` / `BROADCAST_BATCH_SIZE` — drain-loop pacing
- `BROADCAST_PER_RECIPIENT_TIMEOUT_SEC` — per-send HTTP timeout to Meta (≤ meta_whatsapp global 10s)
- `BROADCAST_QUEUE_NAMESPACE` — Redis ZSET key prefix
- `BROADCAST_RECIPIENT_RETRY_MAX` — 5xx retry budget per recipient before fail
- `BROADCAST_DRAIN_MAX_RUNTIME_SEC` — wall-clock cap; worker self-marks `errored` if exceeded
- `BROADCAST_24H_WINDOW_CHECK_ENABLED` — IG/Messenger MARKETING 24h-window compliance gate (default `true`; only `false` literal disables — typo-strict)
- `TEMPLATE_SUBMISSION_MAX_PER_DAY_PER_TENANT` — soft rate-limit on Meta template submits
- `AUDIENCE_CSV_MAX_BYTES` / `AUDIENCE_CSV_MAX_ROWS` — CSV importer body caps
- `OPT_OUT_VIEWER_PAGE_SIZE` — default pagination on the PDPA opt-out viewer
- `WA_ACCOUNT_WEBHOOK_VERIFY_TOKEN` — falls back to `META_VERIFY_TOKEN` if unset
- `PERF_GATE` (test-time only) — `strict` (≤90s, default) / `ci` (≤300s) / `skip` on `tests/outbound/perf/`
- `PERF_BROADCAST_N` (test-time only) — recipient count for the drain harness (default 1000)

**Phase 7** (handoff + Chatwoot integration + Messenger Profile + EA booking) added (per [.env.example](../.env.example) §Phase 7):

Chatwoot outbound (PR-2/PR-3/PR-4):
- `CHATWOOT_BASE_URL` (default `https://chat.getpolaris.my`) — single platform URL per Q-PHASE7-07 (ADR-024)
- `CHATWOOT_BOT_ACCESS_TOKEN` (secret) — platform-wide `agent_bots` global token per Q-PHASE7-01; attaches across all `chatwoot_account_id` values
- `CHATWOOT_AGENT_API_TOKEN` (**Phase 12**, secret) — user/agent-level API token (from CW `/auth/sign_in`). Required for contacts search/create, conversation create, label list/create/apply — CW returns HTTP 401 for bot tokens on these endpoints. Empty string = CW contact+conversation creation disabled (non-fatal degradation). Set to agent-polaris@e2e.polaris-local.dev token in dev. **Multi-tenant note:** this single global token must belong to an agent that is a member of EVERY Chatwoot account (one per tenant), or the mirror 401s for all tenants except that agent's own account. The CW seed (`polaris-cw-fork/lib/tasks/polaris_seed.rake`) now adds the platform (account-1/HQ) agent to all accounts + inboxes for exactly this. Per-tenant human agents stay isolated to their own account.
- `CHATWOOT_API_TIMEOUT_S` (default 8) — per-request HTTP timeout
- `CHATWOOT_API_MAX_RETRIES` (default 2) — 5xx/timeout retry budget per call before declined per ADR-051
- `CHATWOOT_LABEL_AUTO_PROVISION` (default `true`) — runtime `ensure_label` on every apply; Q-PHASE7-02 hybrid (Phase 9 provisioning also seeds)
- `HANDOFF_PRIVATE_NOTE_MAX_LEN` (default 4000) — cap on rendered private note
- `HANDOFF_BUSINESS_HOURS_GRACE_MIN` (default 0) — Q-PHASE7-03 strict cutoff at GA; lever for v2.1
- `HANDOFF_RECOVERY_NOTE_TEMPLATE` — posted on next inbound after partial-failure handoff (Q-PHASE7-04); `{ts}` + `{reason}` substituted

Knowledge Gap Detection (Ultra-only — branch `v2/voice-notes/m1`, not yet on main):
- `KNOWLEDGE_GAP_CAPTURE_ENABLED` (default `true`) — master switch; false → no misses are logged at all
- `KNOWLEDGE_GAP_RETRIEVAL_THRESHOLD` (default `0.0`) — rrf_score floor for a `low_retrieval` miss; `0.0` disables it (only `no_retrieval` + `handoff` fire) until tuned on real data. `knowledge_gap_tiers` (Ultra/Enterprise gate, capture + dashboard) is a code-default frozenset, not env-configurable.

Meta Messenger Profile (PR-5):
- `META_GRAPH_API_VERSION` — **bumped v21.0 → v25.0** (latest per [Meta 2026-02-18 release](https://developers.facebook.com/blog/post/2026/02/18/introducing-graph-api-v25-and-marketing-api-v25/))
- `MESSENGER_PROFILE_API_TIMEOUT_S` (default 10) — Meta Graph API per-request timeout

EA booking client (PR-6):
- `EA_API_BASE_URL` (default `https://calendar.getpolaris.my`) — polaris-ea-fork image base; image tag `ghcr.io/jiun01/polaris-ea:v1.6.0-r3`. Prod uses the internal `http://ea-nginx:80`. **Local dev:** the EA fork runs on a separate docker network exposed at `localhost:8090`, so set `EA_API_BASE_URL=http://host.docker.internal:8090` for the polaris-api container to reach it (the prod URL is unreachable from the container).
- `EA_BOOKING_PUBLIC_URL` (optional, default `https://calendar.getpolaris.my`) — **customer-facing** base URL used when building booking links sent to end-users and the `GET /b/{code}` short-link redirect. Kept pointing at the public calendar domain even when `EA_API_BASE_URL` is overridden to the internal Docker host (`http://ea-nginx:80`) in prod. Distinct from `EA_API_BASE_URL` (which is how polaris-api *reaches* EA internally). Added in v1.0.64 booking-link-fixes.
- `EA_JWT_SIGNING_SECRET` (secret, placeholder-rejecting) — HS256 signing key; mirror to EA fork's `config/polaris.php`
- `EA_JWT_AUDIENCE` (default `ea-fork`) — JWT `aud` claim; EA-side verifier rejects mismatch
- `EA_JWT_ISSUER` (default `polaris`) — JWT `iss` claim
- `EA_JWT_TTL_SECONDS` (default 60) — short-lived per-call JWT; co-located service so replay window is tight per security-reviewer Nit 1
- `EA_API_TIMEOUT_S` (default 8) — per-request HTTP timeout
- `EA_API_MAX_RETRIES` (default 0) — interactive UX; retries hurt rather than help
- `EA_PUBLIC_URL` (default `https://calendar.getpolaris.my`) — the EA fork's own public base URL, written into its `config.php` `BASE_URL`. Distinct from `EA_API_BASE_URL` (which is how polaris-api *reaches* EA); this is the URL EA renders into its own pages/links.
- `EA_AUTO_MIGRATE` (default `true`) — EA fork applies its pending DB migrations on boot (no explicit step). Idempotent — pending-only.

Booking-via-EA-link binding (v2.1):
- `BOOKING_BINDING_SECRET` (secret, placeholder-rejecting) — HS256 key signing the `polaris_ref` binding token in the EA booking link; mint/verify fail closed on placeholder/empty. Distinct from `JWT_SECRET` so a portal-JWT rotation doesn't invalidate in-flight booking links.
- `BOOKING_BINDING_TTL_SECONDS` (default 3600, range 300–86400) — booking-link lifetime; pending bookings past this are swept to `cancelled`.
- `EA_WEBHOOK_HMAC_SECRET` (secret, placeholder-rejecting) — shared secret the polaris-ea-fork signs its appointment-webhook body with (`X-Polaris-Signature: sha256=<hex>`); empty/placeholder → `POST /v1/webhooks/ea` 401.

EA appointment-confirm callback (v1.0.67):
- `POLARIS_API_BASE_URL` (default `https://api.getpolaris.my`) — **also consumed by the EA fork** (`ea-php-fpm` service in `docker-compose.prod.yml`) as the target for the signed appointment-confirm POST to `POST /v1/webhooks/ea`. Set alongside `EA_WEBHOOK_HMAC_SECRET` — the two values together wire the confirmation loop. In prod the shared `.env` value (`https://api.getpolaris.my`) is passed verbatim to the EA container; portal/admin override it to the internal hostname in-compose. Also see the Phase 8 portal and Phase 9 admin sections above where this var controls API base URL for the portal/admin Next.js apps — it is the same env var, consumed by three different services.

EA build arg (v1.0.67):
- `EA_ASSET_TOKEN` (build-arg, not a runtime env var) — injected into the EA image at build time by `infra/docker-bake.hcl` (value = `${EA_TAG}`, the release tag e.g. `v1.6.0-r18`). The EA fork's `asset_url()` helper writes this into PHP's `app.php` `cache_busting_token` during the Docker build, so every new EA image auto-invalidates all front-end asset URLs. **Not set in `.env`** — it flows from `ship.sh` via the bake variable. If you build the EA image directly (e.g. `--no-cache` rebuild) set it explicitly: `docker buildx bake ea --set ea.args.EA_ASSET_TOKEN=<tag> --push`.

## Phase 8 portal — `apps/portal/.env`

The portal has its own `.env` distinct from the API's. Block landed in
PR-1 of Phase 8; documented inline in `04i_PHASE_8_DEEP.md §1.3`.

NextAuth + SMTP (PR-2):
- `NEXTAUTH_URL` (required) — `https://portal.getpolaris.my` in prod, `http://localhost:3000` in dev
- `NEXTAUTH_SECRET` (required, secret) — JWT signing secret. 32+ bytes random. Distinct from API secrets.
- `EMAIL_SERVER_HOST` (required) — reuse Chatwoot's SMTP host per Q-PHASE8-01
- `EMAIL_SERVER_PORT` (default `587`)
- `EMAIL_SERVER_USER` (required) — reuse Chatwoot's SMTP user
- `EMAIL_SERVER_PASSWORD` (required, secret) — reuse Chatwoot's SMTP password
- `EMAIL_FROM` (default `Polaris <no-reply@getpolaris.my>`) — magic-link From header
- `MAGIC_LINK_EXPIRY_MIN` (default `15`) — portal display copy reads this; matches NextAuth EmailProvider config
- `PORTAL_SESSION_MAX_AGE_S` (default `2592000` — 30 days) — NextAuth session lifetime once authenticated

API base URLs (PR-3):
- `POLARIS_API_BASE_URL` (required) — `https://api.getpolaris.my` in prod, `http://localhost:8000` in dev. Used by client + Server Actions.
- `POLARIS_API_INTERNAL_URL` (optional) — server-side override when API is on a private hostname (e.g. Docker network). Falls back to `POLARIS_API_BASE_URL` if unset.
- `NEXT_PUBLIC_POLARIS_API_BASE_URL` (required) — same value as `POLARIS_API_BASE_URL` but inlined into the client bundle. The portal's `lib/api/client.ts` reads this.

Flow editor (PR-7):
- `NEXT_PUBLIC_XYFLOW_LICENSE_KEY` (optional, unset at GA) — placeholder for v2.1+ React Flow Pro if ever needed. OSS xyflow at GA per 00b §10.7.3.

Pagination caps (server-side, read by API routers):
- `NOTIFICATIONS_PAGE_SIZE_MAX` (default `50`) — bell + full-page (PR-1)
- `CONVERSATIONS_PAGE_SIZE_MAX` (default `50`) — read-only viewer (PR-6)

Audience CSV caps (already in tree from Phase 6; documented here for completeness):
- `AUDIENCE_CSV_MAX_BYTES` (default `10485760` — 10 MB)
- `AUDIENCE_CSV_MAX_ROWS` (default `100000`)

E2E (PR-12, optional — CI provisions these via fixture step; locally unset = whole suite skips):
- `PLAYWRIGHT_BASE_URL` (default `http://localhost:3000`) — Playwright `baseURL`
- `PLAYWRIGHT_STORAGE_STATE_STARTER` — path to seeded Starter-tenant auth state JSON
- `PLAYWRIGHT_STORAGE_STATE_PRO` — path to seeded Pro-tenant auth state JSON
- `PLAYWRIGHT_STORAGE_STATE_SUSPENDED` — path to seeded suspended-tenant auth state JSON
- `MAILHOG_API_URL` — needed for `auth.spec.ts` magic-link round-trip in CI

**No portal-side secrets at runtime.** Channels, tenant_secrets (PyNaCl-encrypted), Chatwoot tokens — all stay in `apps/api`. The portal holds only the NextAuth session + tenant context claims.

## Phase 9 admin — quotas, secrets, audit, impersonation, billing (PR-4 to PR-7)

Admin routers (require `role=platform_admin` JWT) added:

**Quotas (PR-4)**:
- No new env vars; 16 columns on `tenant_quotas` are overridable per `/api/admin/quotas/{tenant_id}/override`

**Secrets rotation (PR-4)**:
- No new env vars; `services/meta_admin.py` wires Chatwoot fork admin token rotation

**Audit + Impersonation (PR-5)**:
- **`AUDIT_CHAIN_VERIFY_SCHEDULE`** (optional, default `04:00 MYT`) — APScheduler cron for nightly verification. No AWS/GCP scheduler required at GA; single-host cron sufficient. Format: `HH:MM ZZZ` (e.g., `04:00 Asia/Kuala_Lumpur`).
- **`AUDIT_CHAIN_IMPERSONATION_MAX_AGE_S`** (optional, default `3600` — 1 hour) — max TTL for `pol_impersonation` HttpOnly cookie. Admin ends a session early via `DELETE /api/admin/impersonate`.
- No `IMPERSONATION_COOKIE_DOMAIN` — hard-coded to `.getpolaris.my` at runtime (scoped to prod domain; localhost for dev).

**Billing suite (PR-7/8/9)**:
- **`INVOICE_PDF_BUCKET`** (optional, default `polaris-invoices`) — Supabase Storage bucket for rendered tenant invoices (POST/GET `/api/admin/billing/invoices`)
- **`SUPABASE_STORAGE_QUOTES_BUCKET`** (optional, default `polaris-sales-quotes`) — Supabase Storage bucket for sales quote PDFs (POST/GET `/api/admin/quotes/`)
- No new env vars for PDF generation; `weasyprint>=53,<60` and `jinja2>=3.1,<4` are installed dependencies (pyproject.toml PR-7/8)

**Admin JWT same as Phase 1**: `JWT_SECRET` (HS256) used to sign both `nextauth.js.web-signature` + `pol_impersonation` cookie.

## Phase 9 admin — `apps/admin/.env.local` (PR-1/PR-2 and later)

The admin app has its own env file distinct from the API root `.env` and the
portal's `apps/portal/.env`. Full variable list in `apps/admin/.env.example`.

**NextAuth (PR-2)**:
- **`ADMIN_NEXTAUTH_URL`** (required) — `https://admin.getpolaris.my` in prod; `http://localhost:3001` in dev. NextAuth uses this to build magic-link callback URLs.
- **`ADMIN_NEXTAUTH_SECRET`** (required, secret) — JWT signing secret for admin sessions. DISTINCT from portal's `NEXTAUTH_SECRET` — a portal-session compromise can't replay to admin. Generate: `openssl rand -base64 48`.
- **`ADMIN_EMAIL_SERVER_HOST/PORT/USER/PASSWORD`** (required for magic-link dispatch) — reuse Chatwoot's SMTP per Q-PHASE8-01 pattern.
- **`ADMIN_EMAIL_FROM`** (default `Polaris Admin <no-reply@getpolaris.my>`) — visually distinct from portal magic-link in the operator's inbox.

**Compensating controls (PR-2, per ADR-030 Amendment 2026-05-20)**:
- **`ADMIN_MAGIC_LINK_EXPIRY_MIN`** (default `10`) — stricter than portal's 15 min.
- **`ADMIN_MAGIC_LINK_REQUESTS_PER_HOUR`** (default `3`) — per-email cap.
- **`ADMIN_MAGIC_LINK_IP_BIND_PREFIX`** (default `24`) — /24 prefix match; set `32` for exact-IP.
- **`ADMIN_SESSION_MAX_AGE_S`** (default `86400` — 1 day) — forces daily magic-link cycle.
- **`ADMIN_CADDY_PROXY_TOKEN`** (optional in dev, required in prod) — shared secret Caddy injects as `X-Polaris-Caddy-Token`. Admin trusts `X-Forwarded-For` only when this token matches. Generate: `openssl rand -hex 32`.

**API base URLs (PR-3)**:
- **`POLARIS_API_BASE_URL`** (required) — `https://api.getpolaris.my` in prod; `http://localhost:8000` in dev.
- **`POLARIS_API_INTERNAL_URL`** (optional) — server-side override for Docker network (`http://polaris-api:8000`). Falls back to `POLARIS_API_BASE_URL`.

**Deploy badge**:
- **`NEXT_PUBLIC_DEPLOY_ENV`** (optional) — `prod` (red badge) / `staging` (blue) / `dev` (grey). Falls back to `NODE_ENV`.

## Phase 9 admin — `apps/admin/.env.local` — E2E (PR-12)

Playwright E2E vars for the admin suite. All optional; unset = whole suite skips locally.

- **`PLAYWRIGHT_ADMIN_BASE_URL`** (default `http://localhost:3001`) — Playwright `baseURL`.
- **`PLAYWRIGHT_ADMIN_STORAGE_STATE`** — path to pre-generated Next.js cookies JSON for a seeded platform_admin session. CI writes this during setup; locally run `pnpm e2e:seed-state`.
- **`PLAYWRIGHT_PORTAL_BASE_URL`** (default `http://localhost:3000`) — portal URL used by the impersonation spec.
- **`ADMIN_MAILHOG_API_URL`** (optional) — MailHog API base for magic-link interception. Auth specs skip if unset. Start via `docker compose --profile e2e up mailhog`.
- **`POLARIS_API_INTERNAL_URL`** (optional) — seed helper base URL (falls back to `POLARIS_API_BASE_URL`).
- **`POLARIS_E2E_INTERNAL_SECRET`** (optional) — bearer token for `/internal/e2e/*` seed routes (only mounted when `ENVIRONMENT=test`).

## Phase 9 MRR + Cost (PR-10 / PR-11) — API-side additions

Already documented via `.env.example`; listed here for cross-reference:
- **`GRAFANA_CLOUD_MIMIR_URL`** — Prometheus-compatible **query** base for the cost dashboard. For Grafana Cloud this is `https://<prometheus-host>.grafana.net/api/prom` (the service appends `/api/v1/query`). ⚠️ **Not** the remote-write URL `.../api/prom/push` — that path 404s on query (the classic "Mimir HTTP 404 for every tenant" symptom). The service strips a trailing `/push` defensively, but set the query base directly. `OTEL_ENDPOINT` (the OTLP gateway) is unrelated — it's the *write* side.
- **`GRAFANA_CLOUD_MIMIR_TOKEN`** (secret) — Basic-auth credential for the query API, i.e. `base64("<instanceID>:<token>")` where the token comes from a Grafana Cloud Access Policy scoped **`metrics:read`**. A write-only (remote-write) policy token returns `401` on query. Never in git.
- **`GRAFANA_DASHBOARD_URL`** (optional) — browser URL for the Grafana dashboard; rendered as a deep-link on the System Health "Recent failures" tile. Empty = no link. (2026-06-02)
- **`SENTRY_PROJECT_URL`** (optional) — browser URL for the Sentry project; deep-link on the same tile. Distinct from `SENTRY_DSN` (the ingest DSN). Empty = no link. (2026-06-02)
**Phase 9+** planned: per-channel toggles, Stripe webhook secret, per-inbox Chatwoot secret rotation, OTel SDK exporter config, retention-sweeper TTL knobs.

**Phase 12** (Chatbot ↔ Booking Trigger) added:
- `CHATWOOT_AGENT_API_TOKEN` (secret) — user/agent-level CW token for contacts/conversations/labels API. Complement to the bot token; CW rejects bot tokens with HTTP 401 on those endpoints. Must be a CW agent or admin user's access token (not the platform AgentBot token).

**Phase 13** (Booking Reminders) added:
- `REMINDER_TICK_SECONDS` (optional, default `60`) — poll interval for `run_reminder_worker.py`. Sets `asyncio.sleep()` between `tick_due()` calls.
- `REMINDER_MAX_ATTEMPTS` (optional, default `3`) — max send attempts per `booking_reminders` row before the reminder is marked `failed` and skipped. Prevents stuck reminders burning Meta API quota.

**v2.1 (2026-06-01)** — reminder send is now template-aware (NOT env vars; **per-tenant** in `tenants.settings.booking_config`):
- `reminder_template_name` (optional) — when set, reminders send via this approved WhatsApp template (delivers inside AND outside the 24h window). The template body must take 2 positional params: `{{1}}`=service, `{{2}}`=date/time. When unset, reminders fall back to free-form text (24h-window-limited).
- `reminder_template_language` (optional, default `en`) — language code for the template.
- A customer reply (CONFIRM/RESCHEDULE/CANCEL) to a reminder reopens the 24h window and routes through the inbound pipeline's conversational booking handler (`services/booking/conversational.py`).

**v1.0.27 (2026-06-10, PR #97 booking notifications)** — same per-tenant `tenants.settings.booking_config` blob (NOT env vars). The reminder is now actually scheduled on `confirm_booking` (was only the E2E endpoint), and the immediate confirmation now fires:
- `tenant_timezone` (optional; IANA e.g. `Asia/Kuala_Lumpur`) — appointment date/times in customer messages render in this zone (UTC fallback). Fixes the prior hardcoded-UTC in the reminder text.
- `confirmation_template_name` / `confirmation_template_language` (optional) — sent immediately on EA confirm; delivers outside the 24h window. Falls back to free-form `confirmation_message` (named vars `{{service}}`/`{{date}}`/`{{time}}`; 24h-window-limited) when unset.
- `cancellation_template_name` / `reschedule_template_name` (+ `_language`) — reserved; **not wired** (self-serve cancel/reschedule already replies inline → a template would double-message; needs an EA cancel webhook).
- Library adoption: `meta_whatsapp.create_from_library()` exists for the instant-approved Meta Template Library (UTILITY/AUTH); no portal entry point yet.

**v1.0.63 (2026-06-24, inline conversational booking)** — one new `booking_config` key (NOT an env var; per-tenant JSONB):
- `inchat_picker` (optional bool, default `false`) — when `true`, the inline intent router activates the Phase 2 NL slot picker (LLM-driven turn-by-turn slot collection). When `false` (default), all booking intents send a booking link (Phase 1 link mode). Widen of `bookings.channel` CHECK to include `telegram`/`email` ships as migration 116.

## Prod infra vars (infra/docker-compose.prod.yml only — 2026-05-28)

These are consumed exclusively by `polaris/infra/docker-compose.prod.yml` and are **not read by any Polaris application code**. Document them here for single-source completeness; they live in `polaris/.env` on the VPS alongside all Polaris API vars.

| Var | Service | Notes |
|---|---|---|
| `CW_PG_PASSWORD` | chatwoot-postgres | Superuser password for Chatwoot's Postgres (default `postgres`). `POSTGRES_HOST_AUTH_METHOD=trust` was removed 2026-06-03 — set a strong value for prod (takes effect on a **fresh** volume only). Injected into both CW `DATABASE_URL`s. |
| `CW_REDIS_PASSWORD` | chatwoot-redis | Auth password for Chatwoot's dedicated Redis instance. Injected into `REDIS_URL` for `chatwoot-rails` + `chatwoot-sidekiq`. |
| `EA_MYSQL_ROOT_PASSWORD` | ea-mysql | MySQL root password. Never exposed outside the internal Docker network. |
| `EA_MYSQL_USER` | ea-mysql + ea-php-fpm | App user (default `eauser`). EA reads from container env `DB_USERNAME`. |
| `EA_MYSQL_PASSWORD` | ea-mysql + ea-php-fpm | App user password. EA reads from container env `DB_PASSWORD`. |
| `CADDY_ADMIN_BASIC_AUTH_HASH` | caddy | bcrypt hash for `/admin.getpolaris.my` HTTP Basic Auth. Generate: `docker run --rm caddy caddy hash-password --plaintext <pw>` |
| `ADMIN_CADDY_PROXY_TOKEN` | caddy → polaris-admin | Shared secret Caddy injects as `X-Polaris-Caddy-Token`. Must match `ADMIN_CADDY_PROXY_TOKEN` in `apps/admin/.env.local`. Generate: `openssl rand -hex 32`. |
| `POLARIS_API_BASE_URL` | ea-php-fpm | Public Polaris API URL. The EA fork reads this (via `application/config/polaris.php` → `polaris_api_base_url`) to POST the HMAC-signed appointment-confirm callback to `POST /v1/webhooks/ea` when a customer completes a bot-linked booking. Defaults to `https://api.getpolaris.my`. Portal and admin override it in-compose with the internal hostname (`http://polaris-api:8000`). Added v1.0.67. |

## Observability (API-side) — `.env.example` parity sync 2026-06-03

> **2026-06-03 rename:** these keys now match the pydantic settings field names. The
> old names (`SENTRY_DSN_API`, `OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_EXPORTER_OTLP_HEADERS`)
> did **not** map to any settings field, so Sentry/OTel silently stayed disabled even
> when populated. Use the names below.

| Variable | Required | Format / valid values | Notes |
|---|---|---|---|
| `POLARIS_ENV` | optional | `local \| staging \| prod` (default `local`) | Deploy-environment tag for telemetry. Distinct from `ENVIRONMENT` (which gates KMS/Meta boot checks). |
| `SENTRY_DSN` | optional | Sentry DSN URL | API error reporting. Empty = Sentry disabled. (Renamed from `SENTRY_DSN_API`.) |
| `OTEL_ENDPOINT` | optional | URL | OTLP gateway (Grafana Cloud, e.g. `https://otlp-gateway-prod-<region>.grafana.net/otlp`). (Renamed from `OTEL_EXPORTER_OTLP_ENDPOINT`.) |
| `OTEL_HEADERS` | optional | `Authorization=Basic <base64>` | Auth header for the OTLP exporter. Real token in `.env` only. (Renamed from `OTEL_EXPORTER_OTLP_HEADERS`.) |
| `OTEL_SERVICE_NAME` | optional | string (default `polaris-api`) | `service.name` resource attribute on exported spans/metrics. |
| `POLARIS_PLATFORM_DAILY_SPEND_THRESHOLD_MYR` | optional | float (default `0.0`) | `0` = disabled. MYR threshold that fires a Sentry fatal on all-tenant daily LLM spend. |
| `RETENTION_SWEEP_ENABLED` | optional | `true \| false` (default `true`) | Toggles the data-retention sweep worker. |

## Portal invite emails (API-side) — parity sync 2026-05-29

API-side SMTP for tenant-invite emails (distinct from the portal's NextAuth magic-link `EMAIL_SERVER_*`).

| Variable | Required | Format / valid values | Notes |
|---|---|---|---|
| `PORTAL_BASE_URL` | optional | URL (default `https://portal.getpolaris.my`) | Public portal origin used to build invite links. |
| `PORTAL_NEXTAUTH_SECRET` | **yes** for invite-link verify | matches portal `NEXTAUTH_SECRET` | API-side copy of the portal NextAuth secret — must match exactly. |
| `PORTAL_INVITE_SMTP_HOST` | **yes** to send invites | hostname | SMTP host for invite delivery. |
| `PORTAL_INVITE_SMTP_PORT` | optional | integer (default `587`) | SMTP port. |
| `PORTAL_INVITE_SMTP_USER` | **yes** to send invites | string | SMTP username. |
| `PORTAL_INVITE_SMTP_PASSWORD` | **yes** to send invites | string | SMTP password. `.env` only. |
| `PORTAL_INVITE_SMTP_FROM` | optional | email (default `noreply@getpolaris.my`) | From address on invite emails. |
| `PORTAL_INVITE_SMTP_TIMEOUT_S` | optional | float (default `10.0`) | SMTP socket timeout seconds. |
| `PORTAL_INVITE_EXPIRY_MINUTES` | optional | integer (default `15`) | Invite-link validity window. |

## Admin magic-link + platform alerts email — parity sync 2026-05-29

API-side SMTP for admin magic-link + operator alert emails (`ADMIN_EMAIL_SERVER_HOST` / `ADMIN_EMAIL_FROM` documented above).

| Variable | Required | Format / valid values | Notes |
|---|---|---|---|
| `ADMIN_EMAIL_SERVER_PORT` | optional | integer (default `587`) | Admin SMTP port. |
| `ADMIN_EMAIL_SERVER_USER` | **yes** for admin email | string | Admin SMTP username. |
| `ADMIN_EMAIL_SERVER_PASSWORD` | **yes** for admin email | string | Admin SMTP password. `.env` only. |
| `ADMIN_EMAIL_TO` | optional | email (default `ops@getpolaris.my`) | Destination for platform alert emails. |

## Local-dev SMTP convention (MailHog) — 2026-07-03

All three SMTP-backed email paths above — portal invite (`PORTAL_INVITE_SMTP_*`), portal NextAuth magic-link (`EMAIL_SERVER_*`), and admin magic-link + platform alerts (`ADMIN_EMAIL_SERVER_*`) — route to the local **MailHog** container in dev, not a real SMTP relay. `infra/docker-compose.yml`'s `mailhog` service (profile `e2e`; container name `polaris-mailhog`; web UI `:8025`, SMTP `:1025`) is the trap; the `polaris-api`/`polaris-portal`/`polaris-admin` compose services override the relevant `*_HOST`/`*_PORT` env vars in-compose to point at the container hostname `polaris-mailhog` on port `1025`, with **blank** user/password (MailHog accepts unauthenticated mail and doesn't advertise STARTTLS). Real SMTP host/user/password only exist in the prod VPS `.env` (Chatwoot's SMTP relay, per Q-PHASE8-01).

`weekly_digest.py` and `cost_ceiling.py` (`smtplib`-based schedulers, distinct from the NextAuth/portal-invite send paths above) tolerate MailHog's no-STARTTLS behavior: each tries `smtp.starttls()` first and, only if the server doesn't advertise it, falls back to a plain-text connection — but **only when no `user`/`password` are configured**. If credentials are set and STARTTLS fails, the send raises rather than logging in over cleartext. So local dev (blank MailHog creds) gets a working plain-text send, while prod (real creds) never silently downgrades to cleartext auth.

## E2E / testing — parity sync 2026-05-29

Only active in test/E2E contexts; not required for normal app boot. (`POLARIS_E2E_INTERNAL_SECRET` + Playwright base-URL/storage-state vars are documented in the Phase 8/9 E2E sections above.)

| Variable | Required | Format / valid values | Notes |
|---|---|---|---|
| `EA_BASE_URL` | E2E only | URL (default `http://localhost:8090`) | Easy!Appointments base URL for E2E booking flows. |
| `EA_ADMIN_API_TOKEN` | E2E only | string | EA admin API token used by E2E seed/cleanup. |
| `E2E_ADMIN_EMAIL` | E2E only | email (default `e2e-admin@polaris-local.dev`) | Seeded platform-admin identity for admin E2E. |
| `PLAYWRIGHT_ADMIN_HTTP_USER` | E2E only | string | HTTP Basic-Auth user for the admin surface behind Caddy in E2E. |
| `PLAYWRIGHT_ADMIN_HTTP_PASS` | E2E only | string | HTTP Basic-Auth password companion. `.env` only. |

## Doc-drift pass (2026-07-02) — ~25 vars in config.py not previously documented here

Verified against `apps/api/polaris_api/config.py` Field definitions. All are optional with code defaults unless noted.

**API base URLs / internal service wiring:**
- `PUBLIC_API_BASE_URL` (default `https://api.getpolaris.my`) — `config.py:159`. Public-facing Polaris API base used when building short customer-facing links (e.g. the `/b/{jti}` booking short-link).
- `CHATWOOT_INTERNAL_TOKEN` (default `""`, `config.py:212`) — Bearer token for the Chatwoot fork's internal `/internal/inboxes/:id/token` endpoint (per-inbox channel token vending for channel-add auto-provisioning and lazy auto-provisioning). Empty = feature disabled (`get_channel_token` returns `None` without an HTTP call, no error).
- `CHATWOOT_INTERNAL_BASE_URL` (default `http://chatwoot-rails:3000`, `config.py:216`) — In-cluster base URL for the Chatwoot Rails container, used only by the `/internal/*` provisioning calls so they bypass the public Caddy proxy.
- `EA_HEALTH_URL` (default `https://calendar.getpolaris.my/`, `config.py:293`) — Public, customer-facing booking host used **only** by the admin health probe (QA finding H1). Distinct from `EA_API_BASE_URL` (overridden to the internal `http://ea-nginx:80` in prod for API calls) — a probe against the internal URL would false-green even when the PHP/booking origin is 502-ing; this one traverses Cloudflare → Caddy → nginx → php like a real customer request.
- `EA_PROVISIONING_HMAC_SECRET` (default `"placeholder-ea-provisioning-hmac-secret"`, `config.py:331`) — Shared secret Polaris signs internal EA provisioning calls with (e.g. `POST /api/v1/internal/tenants/provision`; `X-Polaris-Signature: sha256=<hexdigest>` over the raw body). MUST equal the EA fork's `POLARIS_WEBHOOK_SECRET` (`config/polaris.php` → `polaris_webhook_secret`). Placeholder/empty fails closed before any HTTP call (`EABookingConfigError`).

**Meta mock / API base overrides (E2E):**
- `META_API_BASE_URL` (default `https://graph.facebook.com`, `config.py:190`) — E2E override to redirect Meta Graph API calls to a local capture router (`http://localhost:8000/meta-mock` or the Docker-network equivalent). Real Graph API base in staging/prod.
- `META_INSTAGRAM_API_BASE_URL` (default `https://graph.instagram.com`, `config.py:194`) — Separate base URL for Instagram-API-with-Instagram-Login sends (via `graph.instagram.com`, not the Messenger edge on `graph.facebook.com`), so E2E can redirect it to the mock capture router independently of `META_API_BASE_URL`.
- `META_MOCK_ENABLED` (default `false`, `config.py:198`) — E2E-only: mounts the `/meta-mock` capture router. Must be `true` when `META_API_BASE_URL` points back at this same process; safe to leave `false` in staging/prod.

**Sentry read-API (second Sentry integration path, distinct from `SENTRY_DSN`):**
- `SENTRY_AUTH_TOKEN` (default `""`, `config.py:515`) — Sentry user-auth token with `project:read` scope. Powers a read-query surface (e.g. the admin health "apm" tile's 24h error-count probe) that is separate from `SENTRY_DSN` (the ingest DSN used for API error reporting). Empty = that probe is skipped (no error).
- `SENTRY_ORG_SLUG` (default `""`, `config.py:516`) — Sentry org slug, read from the Sentry project URL.
- `SENTRY_PROJECT_SLUG` (default `""`, `config.py:517`) — Sentry project slug, read from the Sentry project URL.

**Voice notes (Groq transcription):**
- `GROQ_API_KEY` (default `"placeholder-groq-key"`, `config.py:403`) — Placeholder-fail-closed. Groq Whisper transcription provider key for inbound voice notes.
- `TRANSCRIPTION_MODEL` (default `whisper-large-v3`, `config.py:407`).
- `TRANSCRIPTION_ENABLED` (default `true`, `config.py:410`) — master switch for voice-note transcription.
- `TRANSCRIPTION_STUB_ENABLED` (default `false`, `config.py:415`) — E2E stub; mirrors `LLM_STUB_ENABLED`. Inert/false in staging+prod.
- `TRANSCRIPTION_TIMEOUT_S` (default `15.0`, `config.py:419`) — per-call Groq timeout.
- `TRANSCRIPTION_MAX_DOWNLOAD_BYTES` (default set in `config.py:423`, 50 MB) — cap on downloaded audio size before transcription is attempted.
- `VOICE_NOTE_TIERS` (frozenset code-default, `config.py:429`) — plan tiers permitted to use voice-note transcription (Pro+; Starter excluded per the PRD). Mirrors the `services/rag.py::_RERANK_DEFAULT_TIERS` gating pattern — no migration, no per-tenant DB row; this is a code-level frozenset, not an env-configurable list in the usual sense (it is declared via `Field(default=...)` but is not meant to be widened per-tenant).

**Knowledge Gap Detection:**
- `KNOWLEDGE_GAP_TIERS` (frozenset code-default, `config.py:441`) — plan tiers gated into Knowledge Gap Detection capture + dashboard (Ultra+). Same code-default-frozenset pattern as `VOICE_NOTE_TIERS` — no migration, no per-tenant row.
- `KNOWLEDGE_GAP_MIN_SIMILARITY` (default `0.70`, `config.py:474`) — similarity floor used by the knowledge-gap classification path. Distinct from the already-documented `KNOWLEDGE_GAP_RETRIEVAL_THRESHOLD` (default `0.0`, off until tuned) — the two are separate knobs in the gap-detection scoring chain; confirm current call-sites before assuming interchangeability.

**Platform spend ceiling:**
- `PLATFORM_DAILY_SPEND_THRESHOLD_MYR` (default `0.0`, `config.py:525`) — MYR threshold; `0` = disabled. Fires a Sentry fatal when all-tenant daily LLM spend crosses it (`cost_ceiling_check.check_platform_daily_spend()`). Same field as the already-documented `POLARIS_PLATFORM_DAILY_SPEND_THRESHOLD_MYR` entry above — confirm the exact env-var name mapping (pydantic-settings prefix conventions) against `config.py` before assuming these are two distinct vars; they describe the same underlying `platform_daily_spend_threshold_myr` setting.

**LLM request timeout:**
- `LLM_REQUEST_TIMEOUT_S` (default `20.0`, range `0 < x <= 120`, `config.py:366`) — Per-request timeout for the Anthropic client (QA finding L6; SDK default is 600s). A hang fails fast and falls back to OpenRouter (`APITimeoutError` is already a transient-fallback trigger).

**DB pool init retry/backoff:**
- `DB_POOL_INIT_ATTEMPTS` (default `5`, range `1-20`, `config.py:122`) — retry attempts for asyncpg pool init on boot (self-heals a momentary Supabase outage; set to `1` to restore fail-fast).
- `DB_POOL_INIT_BASE_DELAY_S` (default `1.0`, range `0 < x <= 30`, `config.py:123`) — base delay for the exponential backoff (`base * 2**(n-1)`) between pool-init retries.

**Version tag:**
- `VERSION` (default `"dev"`, `config.py:549`) — app version string field; surfaced wherever the running build identifies itself (health/status responses). Real value is set by the deploy pipeline (image tag), not hand-edited in `.env` for normal operation.

## 2026-07-02 audit remediation — new + retired fields

**New (Batch B, opt-in RAG skip):**
- `SIMPLE_TIER_SKIP_RAG` (default `false`, `config.py::simple_tier_skip_rag`) — when `true`, `simple`-intent messages in the classic (non-agent) pipeline skip the embed call + `rag.search_hybrid` entirely (`services/pipeline.py`). Catalogue/promotions lookups are unaffected — they were already unconditionally gated on `intent != "simple"`. Default OFF: a simple greeting still runs the full hybrid RAG search exactly as before; this is a visible behavior change once enabled (a greeting won't retrieve KB context even if a chunk would technically match). Knowledge Gap Detection also skips its capture write when RAG was deliberately skipped (an empty chunk list here means "we didn't look", not "we looked and found nothing"). **Not yet added to `.env.example`** — add a placeholder line under a `# Phase 4 (LLM pipeline)` or dedicated RAG section if this becomes commonly toggled.

**Retired (Batch D, flow_runtime deletion, 2026-07-02) — still present in `.env.example` but dead:**

The Phase 5 flow-builder engine (`services/flow_runtime/`) was deleted outright in audit Batch D. The following 6 vars, documented above in the "Required vs optional" table (Phase 5 rows) and in `.env.example` (lines ~202-209), are **no longer read anywhere in `config.py`** — pydantic-settings' `extra="ignore"` means setting them is silently inert, not an error:
- ~~`FLOW_RUNTIME_MAX_NODES_PER_RUN`~~
- ~~`FLOW_RUNTIME_MAX_TRANSITIONS_PER_RUN`~~
- ~~`FLOW_RUNTIME_STEP_TIMEOUT_SEC`~~
- ~~`FLOW_RUNTIME_DEFINITION_MAX_KB`~~
- ~~`FLOW_RUNTIME_VARS_MAX_KB`~~
- ~~`FLOW_RUNTIME_SIMULATOR_DRY_RUN`~~
- ~~`FLOW_TEMPLATES_SEED_ON_MIGRATE`~~

The "Required vs optional" table rows above (originally added in Phase 5) are now historical — kept for archaeology, not because these vars do anything. A future `.env.example` cleanup pass should delete these 7 lines; out of scope for a docs-only pass (editing `.env.example` is a code change, not a doc change). Do not add new config knobs under these names.

