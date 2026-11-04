# Polaris — root Makefile
# Targets: dev migrate seed test lint typecheck smoke verify

SHELL := /usr/bin/env bash
.DEFAULT_GOAL := help

PYTHON ?= python
UV     ?= uv
PNPM   ?= pnpm

.PHONY: help dev down migrate seed test lint typecheck smoke smoke-frontends verify export-types fmt eval eval-sandbox eval-expand seed-e2e-admin e2e-admin-vps

help:
	@echo "Polaris root Makefile"
	@echo ""
	@echo "  make dev        — bring up redis + api + portal + admin via docker compose"
	@echo "  make down       — stop compose stack"
	@echo "  make migrate    — apply pending DB migrations (packages/db-migrations/apply.py)"
	@echo "  make seed       — (stub) seed dev data; Phase 1+ wires this up"
	@echo "  make test       — pytest across all uv workspace members"
	@echo "  make lint       — ruff check + prettier check"
	@echo "  make typecheck  — mypy + pnpm tsc --noEmit"
	@echo "  make smoke      — curl /health on local apps/api"
	@echo "  make verify     — lint + typecheck + test + smoke (the gate)"
	@echo "  make export-types — Pydantic -> JSON Schema -> TS"
	@echo "  make fmt        — ruff format + prettier write"
	@echo "  make eval       — run intent + RAG eval suites (LIVE; requires real Anthropic key)"
	@echo "  make eval-sandbox — run eval suites with deterministic stubs (no API calls)"
	@echo "  make eval-expand  — amplify benchmarks/cost_archetypes.md via Sonnet → benchmarks/prompts/"
	@echo "  make seed-e2e-admin — insert platform_admins row for VPS E2E test user"
	@echo "  make e2e-admin-vps  — run admin Playwright E2E against http://admin.localhost"

dev:
	docker compose -f infra/docker-compose.yml up --build

dev-vps:
	docker compose --env-file .env -f infra/docker-compose.yml -f infra/docker-compose.local-vps.yml up --build

down:
	docker compose --env-file .env -f infra/docker-compose.yml down

down-vps:
	docker compose --env-file .env -f infra/docker-compose.yml -f infra/docker-compose.local-vps.yml down

migrate:
	$(UV) run python packages/db-migrations/apply.py

seed:
	@echo "(stub) seed lands in Phase 1"

test:
	$(UV) run pytest

lint:
	$(UV) run ruff check .
	$(PNPM) run format

typecheck:
	$(UV) run mypy \
	  apps/api/polaris_api \
	  apps/ingestion-worker/polaris_ingest \
	  packages/shared-types/python/polaris_shared_types \
	  packages/db-migrations/apply.py
	$(PNPM) -r run typecheck

smoke:
	@curl -fsS http://localhost:8000/v1/health | python -m json.tool

# Phase-4 E2E sweep: confirm portal + admin Next.js apps boot under
# docker-compose. Bare scaffolds today — just checks HTTP 200/3xx on /.
smoke-frontends:
	@curl -fsS -o /dev/null -w "portal: %{http_code}\n" http://localhost:3000/
	@curl -fsS -o /dev/null -w "admin:  %{http_code}\n" http://localhost:3001/

verify: lint typecheck test smoke

export-types:
	$(UV) run python packages/shared-types/scripts/export.py

fmt:
	$(UV) run ruff format .
	$(PNPM) run format:write

# Phase 4 PR-5 — eval harness. `eval` hits real Anthropic for the intent
# suite (RAG suite needs nightly integration setup, deferred to Phase 5+).
# `eval-sandbox` is the CI-safe path: deterministic predictors, no API
# calls. `eval-expand` is the one-shot Sonnet amplification used when
# benchmarks/cost_archetypes.md changes.
eval:
	$(UV) run python benchmarks/run.py --suite all

eval-sandbox:
	$(UV) run python benchmarks/run.py --suite all --sandbox

eval-expand:
	$(UV) run python benchmarks/expand.py \
	  --archetypes benchmarks/cost_archetypes.md \
	  --output benchmarks/prompts

# Seed a platform_admins row for the VPS-mimic E2E test user.
# Run once before `make e2e-admin-vps`.
seed-e2e-admin:
	$(UV) run python apps/api/scripts/seed_e2e_admin.py

# Run the admin Playwright E2E suite against http://admin.localhost (VPS-mimic).
# Requires: make dev-vps running + make seed-e2e-admin executed.
# global-setup.ts signs in via MailHog and generates the storage-state file.
e2e-admin-vps:
	cd apps/admin && $(PNPM) e2e
