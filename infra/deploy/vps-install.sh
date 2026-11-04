#!/usr/bin/env bash
#
# Polaris VPS — first-time install / bootstrap.
#
# Prereqs (all must be in place before running):
#   1. deploy.env filled in (copy deploy.env.example, set GITHUB_TOKEN)
#   2. /opt/polaris/.env filled in (copy from repo .env.example after first clone)
#   3. /opt/polaris-cw-fork/.env filled in (Chatwoot secrets)
#   4. /opt/polaris/certs/cf-origin.pem + cf-origin-key.pem copied to VPS
#
# Safe to re-run — never overwrites existing .env files.
#
# Usage:
#   ./vps-install.sh
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ -f "${HERE}/deploy.env" ] || { echo "✗ ${HERE}/deploy.env not found — copy deploy.env.example and fill it in." >&2; exit 1; }
# shellcheck source=/dev/null
source "${HERE}/deploy.env"

log()  { printf '\n\033[1;36m▶ %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m! %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

POLARIS_DIR="${DEPLOY_ROOT}/polaris"
CW_ENV="${DEPLOY_ROOT}/polaris-cw-fork/.env"
COMPOSE_FILE="${POLARIS_DIR}/infra/docker-compose.prod.yml"
CF_CERT_DIR="${POLARIS_DIR}/certs"
compose() { docker compose --env-file "${POLARIS_DIR}/.env" -f "${COMPOSE_FILE}" "$@"; }

# ── 1. Prerequisites ──────────────────────────────────────────────────────────
log "Checking prerequisites"
for bin in docker git curl openssl uv; do command -v "$bin" >/dev/null || die "missing: $bin"; done
docker compose version >/dev/null 2>&1 || die "docker compose v2 not available"

# ── 2. Fetch the monorepo ─────────────────────────────────────────────────────
log "Fetching the Polaris monorepo → ${POLARIS_DIR}"
mkdir -p "${DEPLOY_ROOT}"
if [ -d "${POLARIS_DIR}/.git" ]; then
  git -C "${POLARIS_DIR}" fetch origin "${POLARIS_BRANCH}"
  git -C "${POLARIS_DIR}" checkout "${POLARIS_BRANCH}"
  git -C "${POLARIS_DIR}" pull --ff-only
else
  git clone --branch "${POLARIS_BRANCH}" "${POLARIS_REPO_URL}" "${POLARIS_DIR}"
fi
[ -f "${COMPOSE_FILE}" ] || die "compose file not found at ${COMPOSE_FILE}"

# ── 3. Preflight — check all required files before doing any work ─────────────
log "Checking required files"
MISSING=0

if [ ! -f "${POLARIS_DIR}/.env" ]; then
  cp "${POLARIS_DIR}/.env.example" "${POLARIS_DIR}/.env"
  warn "Created ${POLARIS_DIR}/.env — fill it in then re-run."
  MISSING=1
fi

if [ ! -f "${CW_ENV}" ]; then
  mkdir -p "$(dirname "${CW_ENV}")"
  warn "Missing ${CW_ENV} — create it with Chatwoot secrets then re-run."
  warn "  SECRET_KEY_BASE: openssl rand -hex 64"
  MISSING=1
fi

mkdir -p "${CF_CERT_DIR}"
if [ ! -f "${CF_CERT_DIR}/cf-origin.pem" ] || [ ! -f "${CF_CERT_DIR}/cf-origin-key.pem" ]; then
  warn "Missing CF certs in ${CF_CERT_DIR}/ — scp cf-origin.pem + cf-origin-key.pem then re-run."
  MISSING=1
fi

[ "${MISSING}" -eq 0 ] || die "Fix the above then re-run vps-install.sh."

# ── 3b. Boot-guard preflight (Chatwoot-native rehaul) ────────────────────────
# polaris-api REFUSES TO BOOT in non-local environments without
# CHATWOOT_WEBHOOK_HMAC_SECRET (the agent-bot webhook is the ONLY inbound path
# after the rehaul). Catch an empty value here so `up -d --wait` doesn't fail
# later with a cryptic container crash-loop.
_hmac="$(grep -E '^CHATWOOT_WEBHOOK_HMAC_SECRET=' "${POLARIS_DIR}/.env" | head -1 | cut -d= -f2-)"
[ -n "${_hmac}" ] || die "CHATWOOT_WEBHOOK_HMAC_SECRET is empty in ${POLARIS_DIR}/.env — polaris-api will refuse to boot. Set it (must equal the Chatwoot fork's agent-bot HMAC secret) then re-run."

# ── 4. EA nginx.conf ─────────────────────────────────────────────────────────
# nginx.conf is vendored in the monorepo (infra/ea/nginx.conf) and bind-mounted
# straight into ea-nginx — nothing to fetch. (The EA fork is private, so the old
# raw.githubusercontent fetch 404'd; vendoring removes the network dependency.)

# ── 5. GHCR login + pull all images ──────────────────────────────────────────
if [ -n "${GITHUB_TOKEN}" ]; then
  log "Authenticating to GHCR"
  echo "${GITHUB_TOKEN}" | docker login ghcr.io -u "${GHCR_USER}" --password-stdin
else
  warn "GITHUB_TOKEN empty — assuming already logged in to ghcr.io"
fi
log "Pulling all images"
compose pull polaris-api polaris-portal polaris-admin chatwoot-rails chatwoot-sidekiq ea-php-fpm

# ── 6. Database migrations ────────────────────────────────────────────────────
if [ "${SKIP_MIGRATIONS:-0}" != "1" ]; then
  log "Applying DB migrations"
  ( cd "${POLARIS_DIR}" && uv sync --all-packages && set -a && source .env && set +a && uv run python packages/db-migrations/apply.py )
else
  warn "SKIP_MIGRATIONS=1 — skipping"
fi

# ── 7. Start stack ────────────────────────────────────────────────────────────
log "Starting stack"
compose up -d --wait --wait-timeout 300

# ── 8. Chatwoot DB bootstrap ──────────────────────────────────────────────────
log "Bootstrapping Chatwoot DB"
compose exec -T chatwoot-rails bundle exec rake db:chatwoot_prepare \
  || warn "db:chatwoot_prepare returned nonzero (usually fine if already prepared)"

# ── 9. Health ─────────────────────────────────────────────────────────────────
log "Stack status"
compose ps
log "API health"
compose exec -T polaris-api curl -fsS http://localhost:8000/v1/health && echo "  ← polaris-api OK" \
  || warn "polaris-api health check failed — inspect: compose logs polaris-api"

# ── 10. Meta webhook subscriptions ────────────────────────────────────────────
# Post Chatwoot-native rehaul (PR #102): inbound for EVERY Meta channel arrives
# via Chatwoot, not the deleted meta_router. The old `register-meta-webhooks.sh`
# (which subscribed `instagram` → /v1/webhooks/meta) is RETIRED — it would now
# point IG at a 404. Each channel's Meta webhook is flipped to Chatwoot MANUALLY
# per the cutover runbook (this is NOT automated by install):
#   ../docs/internal-ops/runbooks/chatwoot-native-cutover.md
log "Meta webhook subscriptions are configured per the cutover runbook (Chatwoot-native):"
log "  ../docs/internal-ops/runbooks/chatwoot-native-cutover.md"

log "Install complete."
