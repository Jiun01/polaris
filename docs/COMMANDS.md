
# Commands Reference

All commands run from the repo root (`polaris/`) unless noted.

## Make targets (canonical entry points)

| Command | Description |
|---|---|
| `make help` | Print Makefile target list. |
| `make dev` | Bring up local stack (`redis` + `polaris-api` + `polaris-portal` + `polaris-admin`) via `docker compose -f infra/docker-compose.yml up --build`. |
| `make dev-vps` | Local VPS-mimic: layers `infra/docker-compose.local-vps.yml` over the base (adds Caddy + overrides NextAuth URLs) — `docker compose --env-file .env -f infra/docker-compose.yml -f infra/docker-compose.local-vps.yml up --build`. |
| `make down` | Stop the compose stack. |
| `make down-vps` | Stop the VPS-mimic stack (same two-file compose as `dev-vps`, `down`). |
| `make migrate` | Apply pending DB migrations: `uv run python packages/db-migrations/apply.py`. |
| `make seed` | Stub. Demo seeds: `apps/api/scripts/seed_demo_knowledge.py` (Phase 4 PR-6). System-level 5-tenant E2E seed: `apps/api/scripts/seed_e2e_multitenant.py` (NEW 2026-05-27, see `docs/Polaris V2/e2e/01-seed-and-teardown.md` (private)). |
| `make seed-e2e-admin` | Seed the E2E platform-admin identity: `uv run python apps/api/scripts/seed_e2e_admin.py` (pairs with `E2E_ADMIN_EMAIL`). |
| `make e2e-admin-vps` | Run the admin Playwright E2E suite against the VPS-mimic stack: `cd apps/admin && pnpm e2e`. |
| `make test` | `uv run pytest` (workspace-wide). |
| `make lint` | `uv run ruff check .` + `pnpm run format` (prettier --check). |
| `make typecheck` | `uv run mypy <scoped dirs>` + `pnpm -r run typecheck`. |
| `make smoke` | `curl -fsS http://localhost:8000/v1/health \| python -m json.tool`. |
| **`make smoke-frontends`** | **(Phase 4)** `curl :3000 + :3001` to verify portal + admin boot. Run after `make dev` to check frontend startup. |
| `make verify` | The gate: `lint typecheck test smoke`. Run before opening any PR. |
| `make export-types` | Regenerate `typescript/schema.json` from Pydantic SSOT. |
| `make fmt` | `uv run ruff format .` + `pnpm run format:write`. |
| **`make eval`** | **(Phase 4 PR-5)** Run eval suites live against real Anthropic. `uv run python benchmarks/run.py --suite all`. Costs ~$0.05 per full run. |
| **`make eval-sandbox`** | **(Phase 4 PR-5)** Run eval suites with deterministic stubs — no API calls. CI-safe; smoke-tests harness shape. |
| **`make eval-expand`** | **(Phase 4 PR-5)** Sonnet-amplify `benchmarks/cost_archetypes.md` → `benchmarks/prompts/<archetype-id>.jsonl`. Manual one-shot; ~$0.10 per full sweep. |

## uv (Python) commands

Always use `uv run` to invoke Python — never plain `python`. `.venv/` has the pinned versions.

### Setup + everyday

| Command | Description |
|---|---|
| `uv sync --all-packages --all-extras` | Install all workspace member deps (mandatory `--all-packages`). |
| `uv run pytest` | Run the full test suite (818 collected; CI-gate skips some via `POLARIS_TEST_DATABASE_URL` / `PERF_GATE=skip`). |
| `uv run pytest apps/api/tests -v` | API tests verbose. |
| `uv run --env-file .env python <script>` | Load `.env` then run a script. |

### Migrations (Phase 0 → Phase 14b + v2.1: max migration 084 on main, all applied)

| Command | Description |
|---|---|
| `uv run python packages/db-migrations/apply.py` | Apply pending migrations. |
| `uv run python packages/db-migrations/apply.py --dry-run` | Print plan; no DB writes. |
| `uv run python packages/db-migrations/apply.py --json` | Machine-readable output. |

### Shared-types pipeline

| Command | Description |
|---|---|
| `uv run python packages/shared-types/scripts/export.py --check` | Drift check — fail if committed `schema.json` differs from Pydantic source. |
| `uv run python packages/shared-types/scripts/export.py --schema-only` | Regenerate `schema.json` only (skip Node TS generator). |
| `uv run python packages/shared-types/scripts/export.py` | Full pipeline: schema + TS. Writes `packages/shared-types/typescript/schema.json` + `typescript/polaris-shared-types.ts`. |

**Fixed 2026-07-02 (audit Batch F):** Stage 2 (TS generation) previously shelled out to `pnpm dlx json-schema-to-typescript` and silently 127'd / ENOENT'd on Windows (wrong bin name — the package's bin is `json2ts`, not `json-schema-to-typescript`; plus an intermittent `pnpm dlx` junction race). `export.py::invoke_generator()` now resolves `json-schema-to-typescript` from the workspace-root `node_modules/.bin/json2ts(.cmd)` directly — it's a root `devDependency` (`package.json`), installed via ordinary `pnpm install` (no `dlx`, no per-invocation temp dirs). Prerequisite: `pnpm install` at the workspace root at least once.

`typescript/polaris-shared-types.ts` is **no longer gitignored** (only `typescript/*.tsbuildinfo` still is) — it's committed and consumed as a real workspace package:

| Consumer | How |
|---|---|
| `apps/portal` | `"@polaris/shared-types": "workspace:*"` in `package.json` |
| `apps/admin` | `"@polaris/shared-types": "workspace:*"` in `package.json` |

`packages/shared-types/package.json` points `main`/`types` at `typescript/polaris-shared-types.ts`. After changing any Pydantic model in `MODELS` (export.py) or any of its fields, re-run the full pipeline (`uv run python packages/shared-types/scripts/export.py`) and commit both regenerated files — CI's drift check only re-verifies `schema.json`, not the `.ts` file, so a stale `.ts` file won't fail CI on its own.

### CI lint scripts

| Command | Description |
|---|---|
| `uv run python apps/api/scripts/lint_rls_grants.py` | **(audit Batch C+G, 2026-07-02)** For every table with an active `pg_policies` row (RLS in use), asserts at least one GRANT exists to `polaris_anon` and/or `polaris_service`. Guards the migration-118-class bug: RLS policies shipped with zero GRANTs → `polaris_anon` gets silent `permission denied` on every query even though the policy is logically correct (RLS is enforced ON TOP OF table privileges, not instead of them). Reads `POLARIS_DATABASE_URL` or `SUPABASE_DB_URL` from the environment; exit 0 = clean, exit 1 = orphaned policy found (offending table names printed to stderr). Companion pattern for a new tenant-facing table: pair the RLS-enabling migration with an immediate `_grants.sql` migration (see `118_knowledge_sources.sql` → `119_knowledge_sources_grants.sql`). **Not a standalone GitHub Actions step** — enforced indirectly via `apps/api/tests/test_rls_grant_lint.py`, which shells out to this script against the live test DB and is picked up by `pr-check.yml`'s `uv run pytest` step (after that workflow's own "Apply DB migrations" step). `migration-check.yml` does NOT run it (that workflow only runs `packages/db-migrations/tests`). |

### Code quality

| Command | Description |
|---|---|
| `uv run ruff check .` | Workspace-wide lint. |
| `uv run ruff check --fix .` | Auto-fix safe issues. |
| `uv run ruff format .` | Apply formatter. |
| `uv run mypy apps/api/polaris_api apps/ingestion-worker/polaris_ingest packages/shared-types/python/polaris_shared_types packages/db-migrations/apply.py` | Mypy strict (55 source files at Phase 4 close). |
| `uv run python apps/api/scripts/lint_service_pool.py` | Allowlist gate for `get_service_pool`. Phase 4 allowlist unchanged from Phase 2: `db/pools.py`, `main.py`, `services/admin/`, `services/webhook_resolver.py`, `services/demo_sessions.py`, `jobs/`. |

### Phase 4 test files (per PR)

| Command | PR |
|---|---|
| `uv run pytest apps/api/tests/test_llm_cache_strategy.py` | PR-1 — load-bearing cache_control invariant (6 tests incl. AST-level codebase lint) |
| `uv run pytest apps/api/tests/test_llm_classify.py` | PR-1 — Haiku intent classifier (11) |
| `uv run pytest apps/api/tests/test_llm_generate.py` | PR-1 — Sonnet response shape (10) |
| `uv run pytest apps/api/tests/test_llm_fallback.py` | PR-1 — Anthropic→OpenRouter fallback (7) |
| `uv run pytest apps/api/tests/test_llm_streaming.py` | PR-1 — Sonnet streaming (5) |
| `uv run pytest apps/api/tests/test_rag_hybrid.py` | PR-2 — hybrid search + tier cache (7) |
| `uv run pytest apps/api/tests/test_rag_vector_fallback.py` | PR-2 — BM25-empty → vector-only fallback (5) |
| `uv run pytest apps/api/tests/test_rag_cohere_tier_gate.py` | PR-2 — 12-cell tier × override matrix (18) |
| `uv run pytest apps/api/tests/test_conversation_load.py` | PR-3 — Redis hit / DB miss / TTL refresh (7) |
| `uv run pytest apps/api/tests/test_conversation_save_dual_write.py` | PR-3 — DB-first / Redis-after dual write (7) |
| `uv run pytest apps/api/tests/test_conversation_tenant_isolation.py` | PR-3 — same conversation_id across tenants stays isolated (5) |
| `uv run pytest apps/api/tests/test_conversation_window.py` | PR-3 — last 20 OR 8k tokens window-trim (8) |
| `uv run pytest apps/api/tests/test_pipeline_end_to_end.py` | PR-4 — full orchestrator chain with mocked SDKs (7) |
| `uv run pytest apps/api/tests/test_messages_persisted.py` | PR-4 + PR-7 — 11-field telemetry + intent_tier parity test (7); uses migration 031 for column-level RLS grant |
| `uv run pytest apps/api/tests/test_inbound_worker.py` | PR-4 — queue drain + per-message error survival (5) |
| `uv run pytest apps/api/tests/test_eval_regression.py` | PR-5 — YAML schema + minimum case count + scorer (15) |
| `uv run pytest apps/api/tests/test_embed_query.py` | PR-6 — OpenRouter call + dim mismatch + placeholder zero-fallback (8) |
| `uv run pytest apps/api/tests/test_demo_pipeline_dispatch.py` | PR-6 — demo dispatch enqueues IncomingMessage when channel resolves (5) |

### Phase 6 test files (outbound broadcasts — per PR)

| Command | PR |
|---|---|
| `uv run pytest apps/api/tests/outbound/test_meta_whatsapp_templates.py apps/api/tests/outbound/test_meta_whatsapp_send.py` | PR-2 — Meta WhatsApp client: templates + send (13) |
| `uv run pytest apps/api/tests/outbound/test_template_lifecycle.py apps/api/tests/outbound/test_meta_template_status_webhook.py apps/api/tests/outbound/test_tenant_templates_router.py` | PR-3 — Template lifecycle webhook + CRUD (14) |
| `uv run pytest apps/api/tests/outbound/test_audience_importer.py apps/api/tests/outbound/test_audience_opt_out.py apps/api/tests/outbound/test_tenant_audience_router.py` | PR-4 — Audience CSV importer + opt-out viewer; `test_audience_importer.py` includes PDPA phone-redaction assertions (22) |
| `uv run pytest apps/api/tests/outbound/test_audience_filter_compiler.py apps/api/tests/outbound/test_broadcast_service.py apps/api/tests/outbound/test_tenant_broadcasts_router.py` | PR-5 — Audience filter compiler + broadcast service + router (17) |
| `uv run pytest apps/api/tests/outbound/test_broadcast_worker.py apps/api/tests/outbound/test_broadcast_compliance.py` | PR-6 — Broadcast worker drain loop + compliance gate (16) |
| `uv run pytest apps/api/tests/outbound/test_wa_status_webhook.py apps/api/tests/outbound/test_wa_account_webhook.py apps/api/tests/outbound/test_broadcast_delivery.py` | PR-7 — Delivery webhook (wa-status) + MM Lite onboarding (wa-account) (15) |
| `uv run pytest apps/api/tests/outbound/test_outbound_event_types.py` | PR-8 — Outbound event-type pinning (7) |
| `uv run pytest apps/api/tests/outbound/perf/test_broadcast_drain_perf.py` | PR-9 — 1 000-recipient drain perf harness (2; set `PERF_GATE=skip` to bypass in CI) |
| `uv run pytest apps/api/tests/outbound/` | All Phase 6 outbound tests (106) |

### Phase 3 test files (knowledge ingestion API — on `main`)

| Command | PR |
|---|---|
| `uv run pytest apps/api/tests/test_knowledge_ingestion_service.py` | P3 PR-1 — ingestion intake |
| `uv run pytest apps/api/tests/test_knowledge_search_service.py` | P3 PR-2 — hybrid + vector-only RPC wrappers |
| `uv run pytest apps/api/tests/test_tenant_knowledge_router.py` | P3 PR-3 — tenant-facing API |
| `uv run pytest apps/api/tests/test_admin_chunking_config.py` | P3 PR-4 — admin chunk config |
| `uv run pytest apps/api/tests/test_admin_force_run.py` | P3 PR-5 — admin force-run |
| `uv run pytest apps/api/tests/test_tenant_knowledge_capacity_endpoint.py` | P3 PR-5 — quota check |

### Eval harness

| Command | Description |
|---|---|
| `uv run python benchmarks/run.py --suite intent` | Live intent suite against real Anthropic (Haiku). |
| `uv run python benchmarks/run.py --suite rag` | Sandbox RAG only (live RAG returns SuiteReport(skipped=True) — Phase 5+ integration). |
| `uv run python benchmarks/run.py --suite all --sandbox` | Both suites with deterministic stubs (CI mode). |
| `uv run python benchmarks/expand.py --archetypes benchmarks/cost_archetypes.md --output benchmarks/prompts` | Sonnet-amplify 50 archetypes → 1k variants. |

### RAG eval harness (offline CLI, NEW 2026-07-03)

`apps/api/scripts/rag_eval.py` scores the REAL tier-aware retrieval path (`services.rag.search_hybrid`) against a hand-curated golden Q&A set (`apps/api/tests/eval/golden.jsonl`) and reports **recall@k** / **MRR**, broken down by language. Pure scoring math lives in `services/rag_eval.py` (DB-free, unit-tested — `hit_rank`/`recall_at_k`/`mrr`/`summarize`). It's a report, not a CI gate: always exits `0`.

| Command | Description |
|---|---|
| `uv run python apps/api/scripts/rag_eval.py --tenant <uuid>` | Run the full golden set (default `--top-k 5`) against the given tenant's knowledge base. |
| `uv run python apps/api/scripts/rag_eval.py --tenant <uuid> --top-k 10 --lang en` | Override retrieval depth and filter the golden set to one language code. |
| `uv run python apps/api/scripts/rag_eval.py --tenant <uuid> --golden path/to/other.jsonl` | Score against an alternate golden file (default `apps/api/tests/eval/golden.jsonl`). |

Prerequisites:
- Run via `uv run` (loads `.env` + the pinned venv).
- `POLARIS_DB_URL_ANON` must be set — the eval opens one anon-pool connection and runs everything inside a single transaction (`SET LOCAL app.current_tenant_id`) so RLS scopes retrieval to the target tenant; missing it raises `SystemExit` before any query.
- `OPENROUTER_API_KEY` — `embed_query` calls OpenRouter for the query-side embedding; a placeholder key returns a zero vector and search degrades to the BM25 leg only (still runs, lower vector recall).
- The target tenant needs ≥1 row in `knowledge_chunks`, or the script fails fast with a clear message instead of reporting a meaningless recall of 0.
- Read-only: the eval transaction is always rolled back, never committed.

### Phase 4 diagnostics + smoke

| Command | Description |
|---|---|
| `uv run python -m apps.api.scripts.check_db_connections` | Verify `POLARIS_DB_URL_ANON` + `POLARIS_DB_URL_SERVICE` both resolve, connect, and authenticate. Tells you the failure mode (DNS, AUTH, SSL, etc.) instead of dumping asyncpg traceback. Read-only. Useful when bootstrapping a new dev env or rotating role passwords. |
| `uv run python -m apps.api.scripts.check_schema_migrations` | List what's in `schema_migrations` table vs `packages/db-migrations/` on disk. Flags drift (file present + not tracked, or vice versa). Read-only. Helpful after interrupted migration runs or Supabase project switches. |
| `uv run python -m apps.api.scripts.smoke_demo_pipeline` | Synthetic end-to-end smoke: builds `IncomingMessage` in-process, runs pipeline twice. Second call should show `cache_read_tokens > 0`, proving Anthropic prompt-cache is wired (Phase 4 exit criterion 1). Prerequisites: migrations ≤031 applied + `seed_demo_knowledge` run + real `ANTHROPIC_API_KEY` + `OPENROUTER_API_KEY`. Cost ~$0.02/run. |
| `uv run python apps/api/scripts/smoke_messenger_profile.py` | **Phase 7 PR-7** — operator-driven Meta Messenger Profile push against a sandbox FB Page. Prerequisites: `META_FB_PAGE_ID` + `META_FB_PAGE_ACCESS_TOKEN` in env (sandbox creds — never commit). Builds the 3-section bundle (icebreakers + persistent menu + get_started), POSTs to `/v25.0/me/messenger_profile`, re-GETs for drift detection, prints `diff_against_remote`. Exit 0 on clean push; non-zero on Meta 4xx. Bypasses the DB-routed `push_profile` so the operator validates the Meta wire contract directly. |

### Phase 4 demo seed

| Command | Description |
|---|---|
| `uv run python -m apps.api.scripts.seed_demo_knowledge` | Idempotent demo seed: verifies polaris-internal tenant, upserts demo `tenant_channels` row, inserts ~18 Polaris-content `knowledge_chunks`. Requires real `OPENROUTER_API_KEY` (embeds via `services/embed.py`). |

### Phase 5 flow runtime scripts — REMOVED (audit Batch D, 2026-07-02)

`build_template_seed` and `seed_demo_flow` backed the Phase 5 flow-builder
engine (`services/flow_runtime/`), which has been deleted (zero published
flows / zero flow_runs in prod at retirement time; ADR-063 already made the
tool-calling agent the primary reply path). Both scripts are gone.

### System-level E2E seed scripts (NEW 2026-05-27)

Spec: `docs/Polaris V2/e2e/01-seed-and-teardown.md` (maintainer-private).

| Command | Purpose |
|---|---|
| `uv run python apps/api/scripts/seed_e2e_multitenant.py [--reset] [--tenant=<slug>]` | **(E2E)** Idempotent. Seeds the 5-tenant fixture in Polaris DB: Polaris owner (ultra) + Tenant A/B (starter) + C/D (pro). Wires each tenant's quotas, channels, branches, secrets, audience, admin user. `--reset` calls `delete_e2e_tenant_noncascade_rows()` (migration 064) plus the FK-ordered deletes; `--tenant=<slug>` scopes to one of `polaris-owner | tenant-a | tenant-b | tenant-c | tenant-d`. Optional `--skip-flows` / `--skip-knowledge` for faster reseed. Requires `POLARIS_KMS_KEY`. |
| `uv run python apps/api/scripts/seed_e2e_ea_providers.py [--reset]` | **(E2E)** Creates 4 EA providers (Polaris HQ, Tenant C HQ, Tenant D HQ, Tenant D Branch-2) via REST against EA's `/index.php/api/v1/`. Captures auto-generated provider IDs from the responses and writes them back to `tenant_branches.metadata.ea_provider_id`. Requires `EA_ADMIN_API_TOKEN` + `EA_BASE_URL` (defaults to `http://localhost:80`; in dev with `start-all.sh` it's `http://localhost:8090`). |

The companion Chatwoot-side seed (Ruby) lives at:

```
polaris-cw-fork/lib/tasks/polaris_seed.rake
```

Invocation:

```powershell
docker compose -f polaris-cw-fork\docker-compose.yaml `
  -f polaris-cw-fork\docker-compose.override.yml `
  --project-name chatwoot exec rails `
  bundle exec rake polaris:seed_e2e_accounts [RESET=1] [TENANT=<slug>]
```

Creates 5 accounts + inboxes + agents (Devise password `e2e-agent-password!`)
+ bot-handling / human-handling labels + per-account AgentBot with the
configured webhook URL.

### Marketing screenshot capture (NEW 2026-06-04)

Real product screenshots for the marketing site (`../Polaris Website/`). Requires
the local stack up (`start-all.sh`) + MailHog; log in as `owner@getpolaris.my`
via magic-link, captured against the `http://portal.localhost` origin (Caddy).
Demo data is seeded into the `polaris-owner` tenant first (idempotent, tagged for
cleanup; no secrets — values from env). See the screenshots-pipeline notes.

| Command | Purpose |
|---|---|
| `docker exec -i polaris-api python - < scripts/demo/_mktg_seed.py` | Seed demo conversations, flows, knowledge, promotions, dashboard dials. |
| `docker exec -i polaris-api python - < scripts/demo/_mktg_seed_extra.py` | Seed 4 approved `tenant_templates` + 2 broadcasts. **`audience_filter` carries a valid `filter_kind`** (empty `{}` 500s the broadcasts list DTO). |
| `docker exec -i polaris-api python - < scripts/demo/_ea_appt_seed.py` | Seed EA demo appointments for the booking-calendar shot. |
| `docker exec -i chatwoot-rails-1 bundle exec rails runner - < scripts/demo/_mktg_cw_seed.rb` | Seed the Chatwoot agent inbox (separate DB). |
| `cd apps/portal && node e2e/capture-marketing.mjs` | Capture portal screens → `../Polaris Website/public/product/raw/`. |
| `cd apps/portal && node e2e/capture-broadcasts.mjs` | Focused `/broadcasts` re-capture → `e2e/.shots/`. |
| `cd apps/portal && node e2e/{capture-chatwoot,capture-ea-booking,capture-ea-calendar}.mjs` | Capture Chatwoot inbox / EA booking page / EA calendar. |

### Scripts

| Command | Purpose |
|---|---|
| `uv run --env-file .env python -m apps.api.scripts.sync_role_passwords` | **(Phase 1+)** Sync `polaris_anon` / `polaris_service` passwords from `.env` DSNs to the Postgres database. Parses passwords, connects as superuser, runs `ALTER ROLE`, and verifies through pooler with automatic 3-second retry for Supavisor cache lag. Exit codes: 0 = success, 1 = verification failed (wait + retry or restart DB), 2 = missing env var or role doesn't exist, 3 = unexpected exception. |
| `uv run --env-file .env python apps/api/scripts/seed_kms_sentinel.py` | KMS sentinel bootstrap (idempotent UPSERT). |
| `uv run python apps/api/scripts/lint_service_pool.py` | Service-pool allowlist gate. |
| `uv run python apps/api/scripts/run_reminder_worker.py` | **(Phase 13)** Booking reminder daemon. Bootstraps pools + KMS sentinel, then calls `services.booking_reminders.tick_due()` every `REMINDER_TICK_SECONDS` (default 60 s). SIGINT/SIGTERM drain cleanly. Run as a separate process alongside the broadcast worker in compose. |
| `uv run polaris-ingest --help` | Argparse stub (SOFT-RETIRED per ADR-057; real ingestion at `../Polaris Ingestion System/`). |

## pnpm (Node) commands

| Command | Description |
|---|---|
| `pnpm install` | Install all workspace deps. |
| `pnpm run format` | `prettier --check .`. |
| `pnpm run format:write` | `prettier --write .`. |
| `pnpm -r run typecheck` | Each workspace member's `typecheck`. |
| `pnpm --filter @polaris/portal dev` | Portal dev server (`http://localhost:3000`). |
| `pnpm --filter @polaris/admin dev` | Admin dev server (`http://localhost:3001`). |

## Docker

| Command | Description |
|---|---|
| `docker compose -f infra/docker-compose.yml up --build` | Same as `make dev`. |
| `docker compose -f infra/docker-compose.yml down` | Same as `make down`. |
| `docker compose -f infra/docker-compose.yml logs -f polaris-api` | Tail API container logs. |

## Direct uvicorn (no Docker, for fast iteration)

```bash
uv run uvicorn polaris_api.main:app --reload --app-dir apps/api
# then in another terminal:
curl http://localhost:8000/v1/health
```

## Phase 5+ runtime surface (merged to `main`)

After `make dev` is up:

| URL | Method | Purpose |
|---|---|---|
| `http://localhost:8000/v1/health` | GET | API health JSON |
| `http://localhost:8000/v1/webhooks/meta` | GET | Meta subscribe handshake |
| `http://localhost:8000/v1/webhooks/meta` | POST | Meta inbound — enqueues IncomingMessage + LLM pipeline dispatch |
| `http://localhost:8000/v1/webhooks/chatwoot` | POST | Chatwoot Agent-Bot loopback |
| `http://localhost:8000/api/tenant/knowledge/*` | various | Phase 3 knowledge ingestion API (intake + status + capacity) |
| `http://localhost:8000/api/admin/*` | various | Phase 3 admin endpoints (chunking config + force-run) |
| `http://localhost:8000/api/admin/billing/invoices` | GET/POST | *Phase 9 PR-7* List or issue tenant invoices (requires `role=platform_admin`) |
| `http://localhost:8000/api/admin/billing/invoices/{id}/pdf` | GET | *Phase 9 PR-7* 307-redirect to signed invoice PDF |
| `http://localhost:8000/api/admin/billing/invoices/{id}/mark-paid` | POST | *Phase 9 PR-7* Mark invoice as paid (idempotent) |
| `http://localhost:8000/api/admin/billing/archive` | GET | *Phase 9 PR-7* Invoice counts by (year_month, tenant_id) |
| `http://localhost:8000/api/admin/plan-changes/` | GET/POST | *Phase 9 PR-8* List or create plan-change requests |
| `http://localhost:8000/api/admin/plan-changes/{id}/approve` | POST | *Phase 9 PR-8* Approve with server-authoritative proration + invoice |
| `http://localhost:8000/api/admin/plan-changes/{id}/reject` | POST | *Phase 9 PR-8* Reject with decision_note |
| `http://localhost:8000/api/admin/quotes/` | GET/POST | *Phase 9 PR-9* List or generate sales quotes |
| `http://localhost:8000/api/admin/quotes/{id}/pdf` | GET | *Phase 9 PR-9* 307-redirect to signed quote PDF |
| `http://localhost:8000/api/admin/quotes/{id}/convert/{tenant_id}` | POST | *Phase 9 PR-9* Convert quote → subscription invoice + plan_tier flip |
| `http://localhost:8000/api/admin/demos` | GET/POST | *Demo System v3 (ADR-066)* List all demos or create a new demo (requires `role=platform_admin`) |
| `http://localhost:8000/api/admin/demos/{id}` | GET/PUT/DELETE | *Demo System v3* Fetch, update, or soft-delete a demo config |
| `http://localhost:8000/api/admin/demos/{id}/reset-visitor` | POST | *Demo System v3* Reset a visitor's one-demo-ever lock (allows re-entry for testing) |
| `http://localhost:8000/api/admin/demos/{id}/sessions` | GET | *Demo System v3* List active/past demo sessions for a given demo config |
| `http://localhost:3001/demo-system` | GET | *Demo System v3* Admin UI screen — create/manage demos; KB via portal tenant-impersonation |
| `http://localhost:8000/docs` | GET | FastAPI OpenAPI/Swagger UI |
| `http://localhost:3000/` | GET | Portal hello-world |
| `http://localhost:3001/` | GET | Admin hello-world |

Boot log surfaces which inbound consumer was selected (`main.py::_start_inbound_consumer`):

```
inbound_worker: Phase 4 pipeline consumer selected
```

Falls back to the Phase 2 logging consumer (with the missing-dependency reason logged) when pools aren't initialised, Redis isn't initialised, or there's no real `ANTHROPIC_API_KEY` (and `LLM_STUB_ENABLED` isn't set) — not gated on any specific migration count. (Corrected 2026-07-02 — the string here previously said "Phase 5 flow runtime consumer selected" and cited a migration-count gate, neither of which matches `main.py`; the flow_runtime engine referenced by the old string was deleted in audit Batch D.)

## CI commands (`.github/workflows/`)

Triggered automatically on PRs to `main` and pushes to `main`. To trigger manually: GitHub UI → Actions → workflow → "Re-run failed jobs" or "Run workflow".

| Workflow | Purpose |
|---|---|
| `pr-check.yml` | ruff + mypy + pytest + prettier + tsc |
| `migration-check.yml` | `apply.py` idempotence against `postgres:17`. Triggers on changes to `packages/db-migrations/**` or `.github/workflows/migration-check.yml`. (The `_template_defs/**` + `build_template_seed --check` triggers were removed with the Phase 5 flow-builder engine — audit Batch D, 2026-07-02.) |
