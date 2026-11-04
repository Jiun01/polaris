#!/usr/bin/env bash
#
# Polaris VPS — update / redeploy.
#
# Pulls the latest monorepo (Polaris-own source + any compose/image-tag changes),
# pulls updated fork images from GHCR, refreshes the EA shared volume when the EA
# image changed (flagged item #3), runs migrations, rebuilds the Polaris-own
# images, and recreates only the services that changed.
#
# Usage:  ./vps-update.sh
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ -f "${HERE}/deploy.env" ] || { echo "✗ ${HERE}/deploy.env not found." >&2; exit 1; }
# shellcheck source=/dev/null
source "${HERE}/deploy.env"

log()  { printf '\n\033[1;36m▶ %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m! %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

POLARIS_DIR="${DEPLOY_ROOT}/polaris"
COMPOSE_FILE="${POLARIS_DIR}/infra/docker-compose.prod.yml"
PROJECT="polaris-prod"            # from `name:` in the compose → volume prefix
# --env-file is required: invoked from infra/deploy/, Compose would otherwise miss
# polaris/.env and interpolate ${EA_MYSQL_PASSWORD}/${EA_JWT_SIGNING_SECRET}/etc.
# to empty/weak defaults. See vps-install.sh for the full rationale.
compose() { docker compose --env-file "${POLARIS_DIR}/.env" -f "${COMPOSE_FILE}" "$@"; }

[ -f "${COMPOSE_FILE}" ] || die "compose file not found — run vps-install.sh first"

# ── 0. Boot-guard preflight (Chatwoot-native rehaul) ─────────────────────────
# polaris-api refuses to boot in non-local envs without CHATWOOT_WEBHOOK_HMAC_SECRET
# (the agent-bot webhook is the only inbound path post-rehaul). Fail fast before
# spending time on migrations + recreate.
_hmac="$(grep -E '^CHATWOOT_WEBHOOK_HMAC_SECRET=' "${POLARIS_DIR}/.env" | head -1 | cut -d= -f2-)"
[ -n "${_hmac}" ] || die "CHATWOOT_WEBHOOK_HMAC_SECRET is empty in ${POLARIS_DIR}/.env — polaris-api will refuse to boot. Set it then re-run."

# ── 1. Pull latest source (api/portal/admin build from this; new tags land here)
log "Pulling latest monorepo"
_pre_pull_head="$(git -C "${POLARIS_DIR}" rev-parse HEAD)"
git -C "${POLARIS_DIR}" pull --ff-only

# ── 2. GHCR login + record current EA image, then pull updated images ────────
if [ -n "${GITHUB_TOKEN}" ]; then
  echo "${GITHUB_TOKEN}" | docker login ghcr.io -u "${GHCR_USER}" --password-stdin
fi
EA_BEFORE="$(compose images -q ea-php-fpm 2>/dev/null || true)"
log "Pulling updated images from GHCR"
compose pull polaris-api polaris-portal polaris-admin chatwoot-rails chatwoot-sidekiq ea-php-fpm || warn "image pull returned nonzero — continuing"
EA_AFTER="$(compose images -q ea-php-fpm 2>/dev/null || true)"

# ── 3. EA shared-volume refresh (flagged item #3) ────────────────────────────
# The ea_app named volume only auto-populates on FIRST creation. If the EA image
# changed, delete it so the new code repopulates; otherwise stale code persists.
if [ "${FORCE_EA_REFRESH:-0}" = "1" ] || { [ -n "${EA_AFTER}" ] && [ "${EA_BEFORE}" != "${EA_AFTER}" ]; }; then
  log "EA image changed (or forced) — refreshing ${PROJECT}_ea_app volume"
  compose rm -sf ea-php-fpm ea-nginx || true
  docker volume rm "${PROJECT}_ea_app" 2>/dev/null || warn "ea_app volume not present (will be created fresh)"
else
  log "EA image unchanged — leaving the shared volume in place"
fi

# ── 3b. Detect EA nginx.conf change (vendored in-repo) ───────────────────────
# nginx.conf is vendored at infra/ea/nginx.conf and bind-mounted into ea-nginx, so
# the step-1 pull already delivered any update (the EA fork is private and can't be
# curl'd by the VPS — that's why the old fork-fetch approach 404'd). A bind-mounted
# file change does NOT alter the compose config hash, so `up -d` won't restart
# ea-nginx on its own; force a recreate when this pull touched the file. (A change
# to the mount PATH itself — e.g. migrating off the old fork sibling path — makes
# `up -d` recreate ea-nginx anyway.)
EA_NGINX_CHANGED=0
if ! git -C "${POLARIS_DIR}" diff --quiet "${_pre_pull_head}" HEAD -- infra/ea/nginx.conf 2>/dev/null; then
  EA_NGINX_CHANGED=1
  log "EA nginx.conf changed in this pull — ea-nginx will be recreated"
else
  log "EA nginx.conf unchanged"
fi

# ── 4. Migrations ────────────────────────────────────────────────────────────
if [ "${SKIP_MIGRATIONS:-0}" != "1" ]; then
  log "Applying DB migrations"
  # Non-interactive SSH shells don't source ~/.profile, so uv (installed to
  # ~/.local/bin or ~/.cargo/bin) is off PATH. Add the standard install dirs
  # before the check so `build.sh --deploy` runs migrations hands-off.
  export PATH="${HOME}/.local/bin:${HOME}/.cargo/bin:${PATH}"
  command -v uv >/dev/null || die "uv not found — install it or set SKIP_MIGRATIONS=1"
  # Extract the DB URL from polaris/.env without sourcing the whole file.
  # Sourcing fails under set -u because .env contains Docker Compose
  # positional-variable syntax (e.g. ${2}) that bash treats as unbound params.
  _db_url="$(grep -E '^(SUPABASE_DB_URL|POLARIS_DATABASE_URL)=' "${POLARIS_DIR}/.env" \
             | head -1 | cut -d= -f2-)"
  [ -n "${_db_url}" ] || die "SUPABASE_DB_URL / POLARIS_DATABASE_URL not found in ${POLARIS_DIR}/.env"
  ( cd "${POLARIS_DIR}" && SUPABASE_DB_URL="${_db_url}" POLARIS_DATABASE_URL="${_db_url}" \
      uv run python packages/db-migrations/apply.py )
else
  warn "SKIP_MIGRATIONS=1 — skipping migrations"
fi

# ── 4b. Self-heal custom-role passwords ──────────────────────────────────────
# Supabase periodically resets the custom-role (polaris_anon / polaris_service)
# credentials. That surfaces as `InvalidPasswordError` on the next container
# recreate and aborts polaris-api startup. sync_role_passwords re-asserts the
# .env DSN passwords onto the roles (idempotent), so every deploy self-heals the
# alignment before `up -d` and we never hit a failed startup from drift.
# Best-effort: a transient Supavisor credential-cache lag (exit 1) must not block
# the deploy — the app reconnects once the cache catches up.
if [ "${SKIP_ROLE_SYNC:-0}" != "1" ]; then
  log "Syncing custom-role passwords (polaris_anon / polaris_service)"
  export PATH="${HOME}/.local/bin:${HOME}/.cargo/bin:${PATH}"
  if command -v uv >/dev/null; then
    # grep/cut each DSN (not `source`) — .env has Compose ${n} + templated
    # values that break shell sourcing and uv's --env-file parser alike.
    _anon="$(grep -E '^POLARIS_DB_URL_ANON=' "${POLARIS_DIR}/.env" | head -1 | cut -d= -f2-)"
    _svc="$(grep -E '^POLARIS_DB_URL_SERVICE=' "${POLARIS_DIR}/.env" | head -1 | cut -d= -f2-)"
    _sup="$(grep -E '^(SUPABASE_DB_URL|POLARIS_DATABASE_URL)=' "${POLARIS_DIR}/.env" | head -1 | cut -d= -f2-)"
    set +e
    ( cd "${POLARIS_DIR}" \
        && POLARIS_DB_URL_ANON="${_anon}" POLARIS_DB_URL_SERVICE="${_svc}" SUPABASE_DB_URL="${_sup}" \
           uv run python -m apps.api.scripts.sync_role_passwords )
    _sync_rc=$?
    set -e
    # Non-fatal by design (a transient Supavisor cache lag must not block the
    # deploy), but a REAL failure is surfaced loudly — silent drift is exactly
    # what floods InvalidPasswordError and gets the VPS IP network-banned.
    case "${_sync_rc}" in
      0) log "Role passwords aligned ✓" ;;
      1) warn "role-password sync: ALTER ran but pooler verify lagged (Supavisor cache) — usually benign; the app reconnects once the cache catches up." ;;
      *) warn "‼ ROLE-PASSWORD SYNC FAILED (exit ${_sync_rc}). polaris_anon/polaris_service may be DRIFTED → expect auth failures and a possible Supabase network ban. Fix before relying on this deploy: re-run infra/deploy/role-sync.sh, and check SUPABASE_DB_URL reachability + the .env DSNs." ;;
    esac
  else
    warn "uv not found — skipping role-password sync"
  fi
else
  warn "SKIP_ROLE_SYNC=1 — skipping role-password sync"
fi

# ── 5. Rebuild Polaris-own + recreate only changed services ──────────────────
log "Restarting changed services"
compose up -d

# A bind-mounted file change doesn't alter the compose config hash, so the
# `up -d` above won't restart ea-nginx when only nginx.conf changed (step 3b).
# Force it so the refreshed conf is actually loaded before the mesh assertion.
if [ "${EA_NGINX_CHANGED}" = "1" ]; then
  log "Recreating ea-nginx to load refreshed nginx.conf"
  compose up -d --force-recreate ea-nginx
fi

# ── 5b. Self-heal the Polaris agent-bot outgoing_url ─────────────────────────
# The agent-bot's outgoing_url (where Chatwoot forwards inbound to Polaris) is
# runtime config in the CW Postgres volume, set manually at bootstrap. If it is
# nil/wrong, Chatwoot silently never calls Polaris → no reply, no bot-handling
# label, conversations sit pending. Re-assert it every deploy so it self-heals
# after a bot recreate. NOTE: a full `compose down -v` wipe destroys the whole
# bootstrapped CW (inboxes, tokens, bot) — that needs a DB restore, not this.
log "Ensuring agent-bot outgoing_url"
compose exec -T chatwoot-rails bundle exec rails runner '
  url = "http://polaris-api:8000/v1/webhooks/chatwoot"
  b = AgentBot.find_by(id: 1) || AgentBot.where("name ILIKE ?", "%polaris%").order(:id).first
  if b.nil?
    puts "WARN: no Polaris agent-bot found — re-bootstrap Chatwoot, then re-run"
  elsif b.outgoing_url == url
    puts "agent-bot ##{b.id} outgoing_url OK"
  else
    b.update!(outgoing_url: url); puts "agent-bot ##{b.id} outgoing_url HEALED"
  end
' || warn "agent-bot outgoing_url heal skipped (continuing)"

# ── 5c. IPv6 dual-stack mesh assertion (FATAL) ───────────────────────────────
# prod-net is dual-stack (polaris-api needs IPv6 egress for the IPv6-only direct
# Supabase DSN), so every container resolves to BOTH A + AAAA. A service that binds
# a single family is silently unreachable over the other for hard-fail clients
# (Chatwoot Ruby agent-bot, polaris-api httpx) → dropped webhooks / failed internal
# calls. This recurred 3×. Assert every HTTP server answers on BOTH families and
# fail the deploy LOUDLY rather than ship a half-broken mesh again.
log "Asserting prod-net dual-stack reachability"
compose exec -T polaris-api python - <<'PY' || die "prod-net NOT dual-stack reachable — a server is bound to a single family. Inspect docker-compose.prod.yml binds (--host :: / HOSTNAME=:: / rails -b :: / nginx listen [::]:80)."
import socket, sys
targets = [("polaris-api", 8000), ("chatwoot-rails", 3000),
           ("polaris-portal", 3000), ("polaris-admin", 3001), ("ea-nginx", 80)]
bad = []
for host, port in targets:
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except Exception as e:
        bad.append(f"{host}:{port} DNS={type(e).__name__}"); continue
    for fam, label in ((socket.AF_INET, "v4"), (socket.AF_INET6, "v6")):
        addrs = [i[4][0] for i in infos if i[0] == fam]
        if not addrs:
            bad.append(f"{host}:{port} no-{label}"); continue
        s = socket.socket(fam, socket.SOCK_STREAM); s.settimeout(3)
        try:
            s.connect((addrs[0], port))
        except Exception as e:
            bad.append(f"{host}:{port} {label}={type(e).__name__}")
        finally:
            s.close()
print("mesh dual-stack:", "OK" if not bad else f"BROKEN {bad}")
sys.exit(1 if bad else 0)
PY

# The real client that broke is the Chatwoot Ruby agent-bot — assert from there too.
compose exec -T chatwoot-sidekiq bundle exec ruby -e '
  require "socket"
  infos = Addrinfo.getaddrinfo("polaris-api", 8000, nil, :STREAM)
  ok = ->(ai){ begin; ai.connect.close; true; rescue => e; warn "#{ai.ip_address}: #{e}"; false; end }
  v4 = infos.select(&:ipv4?).any?(&ok); v6 = infos.select(&:ipv6?).any?(&ok)
  abort "Chatwoot->polaris-api DOWN (v4=#{v4} v6=#{v6})" unless v4 && v6
  puts "Chatwoot->polaris-api OK on v4+v6"
' || die "Chatwoot cannot reach polaris-api on both families — agent-bot webhooks will be dropped."

# Sidekiq builds attachment data_urls + agent-bot payloads; a loopback FRONTEND_URL
# makes every voice-note data_url unreachable from polaris-api.
fu="$(compose exec -T chatwoot-sidekiq sh -lc 'printf %s "$FRONTEND_URL"')"
[ -n "$fu" ] || die "chatwoot-sidekiq FRONTEND_URL is empty — voice-note data_urls will fail. Set the override in docker-compose.prod.yml."
case "$fu" in
  *0.0.0.0*|*127.0.0.1*|*localhost*)
    die "chatwoot-sidekiq FRONTEND_URL=$fu is non-routable from polaris-api — voice-note data_urls will fail. Set the override in docker-compose.prod.yml." ;;
  *) log "chatwoot-sidekiq FRONTEND_URL OK ($fu)" ;;
esac

# ── 6. Reclaim disk from old image layers ────────────────────────────────────
log "Pruning dangling images"
docker image prune -f >/dev/null || true

# ── 7. Health ────────────────────────────────────────────────────────────────
log "Stack status"
compose ps
compose exec -T polaris-api curl -fsS http://localhost:8000/v1/health && echo "  ← polaris-api OK" \
  || warn "polaris-api health check failed — inspect: compose logs polaris-api"

log "Update complete."
