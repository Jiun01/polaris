# apps/api — Polaris API service

FastAPI on Python 3.12 (uv workspace member). Hosts the inbound pipeline
(intent classify → RAG → LLM → dispatch), all tenant and admin REST routes,
webhook routers (Chatwoot/Meta/EA), and the scheduled jobs.

## Local dev

```bash
# From repo root
uv sync
uv run uvicorn polaris_api.main:app --reload --app-dir apps/api
curl http://localhost:8000/v1/health
```

## Tests

```bash
# From repo root
uv run pytest apps/api/tests -v
```

## Layout

- `polaris_api/routers/` — tenant + admin + public REST routes
- `polaris_api/webhooks/` — Chatwoot, Meta status/template, EA routers
- `polaris_api/services/` — pipeline, RAG, LLM, dispatch, secrets, booking
- `polaris_api/scheduled/` — cron jobs (quota flush, MRR rollup, digest, ...)
- `polaris_api/db/` — dual asyncpg pools (anon = RLS, service = BYPASSRLS)
- `polaris_api/e2e/` — internal E2E router (`POLARIS_E2E_INTERNAL_SECRET`-gated)
- `polaris_api/scripts/` — operator scripts (seeds, smokes, linters)
- `tests/` — pytest suite
