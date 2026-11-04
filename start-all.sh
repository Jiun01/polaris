#!/usr/bin/env bash
# start-all.sh — boot Polaris + Chatwoot + Easy!Appointments on a Linux server.
#
# Usage:
#   ./start-all.sh            # build + start all stacks (detached)
#   ./start-all.sh down       # stop + remove containers
#   ./start-all.sh ps         # show container status
#   ./start-all.sh logs       # tail logs (Ctrl+C to stop)
#
# Build behaviour:
#   Each stack is started with --no-build first (fast path — reuses cached
#   images). If that fails because an image is missing it retries with
#   --build (slow path — full rebuild from Dockerfile). This avoids
#   transient network errors during gem/npm installs aborting the whole
#   boot sequence.

set -uo pipefail   # -u: unbound vars are errors; -o pipefail: pipe failures visible
                   # intentionally NO -e: each stack is started independently and
                   # Next.js static-gen warnings return non-zero but are not fatal.

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ---------------------------------------------------------------------------
# Compose file paths
# ---------------------------------------------------------------------------

# Polaris: base + VPS overlay (Caddy reverse-proxy + MailHog)
POLARIS_BASE="$ROOT/infra/docker-compose.yml"
POLARIS_VPS="$ROOT/infra/docker-compose.local-vps.yml"

# Chatwoot fork: docker-compose.yaml + override at repo root.
# -f disables auto-discovery so both files are listed explicitly.
CHATWOOT_BASE="$ROOT/../polaris-cw-fork/docker-compose.yaml"
CHATWOOT_OVERRIDE="$ROOT/../polaris-cw-fork/docker-compose.override.yml"

# Easy Appointments fork: compose + override at repo root.
EA_BASE="$ROOT/../polaris-ea-fork/docker-compose.yml"
EA_OVERRIDE="$ROOT/../polaris-ea-fork/docker-compose.override.yml"

# ---------------------------------------------------------------------------
# Compose wrappers
# ---------------------------------------------------------------------------

polaris()  { docker compose --env-file "$ROOT/.env" \
               -f "$POLARIS_BASE" -f "$POLARIS_VPS" \
               --project-name polaris "$@"; }

chatwoot() { docker compose \
               -f "$CHATWOOT_BASE" -f "$CHATWOOT_OVERRIDE" \
               --project-name chatwoot "$@"; }

ea()       { docker compose \
               -f "$EA_BASE" -f "$EA_OVERRIDE" \
               --project-name polaris-ea-fork "$@"; }

# start_stack <name> <compose-fn>
# Tries --no-build first; falls back to --build if images are missing.
start_stack() {
  local name="$1"; shift
  echo ""
  echo "==> Starting $name..."
  if "$@" up --no-build -d 2>&1; then
    echo "    $name started (existing images)"
  else
    echo "    Images missing — building $name from scratch (this may take a while)..."
    "$@" up --build -d
    echo "    $name built and started"
  fi
}

# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

ACTION="${1:-up}"

case "$ACTION" in
  up)
    start_stack "Polaris  (API + Portal + Admin + Caddy + MailHog)" polaris
    start_stack "Chatwoot" chatwoot
    start_stack "Easy Appointments" ea

    echo ""
    echo "==> All stacks started."
    echo ""
    echo "  Polaris API       http://localhost:8000/v1/health"
    echo "  Polaris Portal    http://localhost:3000  (or http://portal.localhost via Caddy)"
    echo "  Polaris Admin     http://localhost:3001  (or http://admin.localhost via Caddy)"
    echo "  Chatwoot          http://localhost:3002"
    echo "  EA App            http://localhost:8090"
    echo "  EA phpMyAdmin     http://localhost:8080"
    echo "  EA Swagger UI     http://localhost:8001"
    echo "  EA MailPit        http://localhost:8026"
    echo "  MailHog (Polaris) http://localhost:8025"
    echo ""
    echo "  First-run only — set up Chatwoot DB:"
    echo "  docker compose -f ../polaris-cw-fork/docker-compose.yaml \\"
    echo "    -f ../polaris-cw-fork/docker-compose.override.yml \\"
    echo "    --project-name chatwoot exec rails bundle exec rake db:chatwoot_prepare"
    ;;
  down)
    echo "==> Stopping all stacks..."
    polaris  down || true
    chatwoot down || true
    ea       down || true
    echo "Done."
    ;;
  ps)
    echo "--- Polaris ---";   polaris  ps
    echo "--- Chatwoot ---";  chatwoot ps
    echo "--- EA ---";        ea       ps
    ;;
  logs)
    echo "Tailing logs from all stacks (Ctrl+C to stop)..."
    polaris  logs -f &
    chatwoot logs -f &
    ea       logs -f
    ;;
  *)
    echo "Unknown action '$ACTION'. Use: up | down | ps | logs"
    exit 1
    ;;
esac
