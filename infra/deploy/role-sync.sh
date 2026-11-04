#!/usr/bin/env bash
#
# Polaris VPS — out-of-band custom-role password self-heal.
#
# Re-asserts the polaris_anon / polaris_service role passwords from polaris/.env
# onto the Supabase roles — identical to vps-update.sh step 4b, but on a SCHEDULE
# (hourly systemd timer) so a SPONTANEOUS Supabase-side password reset is
# realigned WITHOUT waiting for the next deploy. Idempotent: a no-op when the
# roles already match .env.
#
# Why this exists: Supabase periodically resets custom-role credentials. Between
# deploys nothing re-aligns them, so the app pools start failing auth, flood
# Supabase with InvalidPasswordError, and the VPS IP gets network-banned (root
# cause confirmed 2026-06-20 — see ../docs/internal-ops/PRPs/plans/portal-supabase-ban-resilience).
# A scheduled re-sync keeps the drift window small.
#
# NOTE: the interval governs RECOVERY LATENCY, not ban avoidance. The actual
# ban-preventer is the app-side pool backoff (portal circuit breaker + the API's
# 503-on-acquire path). Do not shorten the interval to compensate — fix backoff.
#
# Invoked by the polaris-role-sync.service systemd unit. Safe to run by hand:
#   ./role-sync.sh
#
# Exit: 0 = synced (or benign Supavisor cache lag); nonzero = real failure
#       (missing env / uv / superuser unreachable) — investigate via the journal.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ -f "${HERE}/deploy.env" ] || { echo "role-sync: ${HERE}/deploy.env not found." >&2; exit 2; }
# shellcheck source=/dev/null
source "${HERE}/deploy.env"

POLARIS_DIR="${DEPLOY_ROOT}/polaris"
[ -d "${POLARIS_DIR}" ] || { echo "role-sync: ${POLARIS_DIR} not found." >&2; exit 2; }

# Non-interactive shells (systemd) don't source ~/.profile, so uv (installed to
# ~/.local/bin or ~/.cargo/bin) is off PATH. Add the standard install dirs.
export PATH="${HOME}/.local/bin:${HOME}/.cargo/bin:${PATH}"
command -v uv >/dev/null || { echo "role-sync: uv not found on PATH — cannot run." >&2; exit 2; }

# grep/cut each DSN (not `source`) — .env carries Compose ${n} positional syntax
# + templated values that break shell sourcing and uv's --env-file parser alike.
# Mirrors vps-update.sh step 4b exactly.
_anon="$(grep -E '^POLARIS_DB_URL_ANON='    "${POLARIS_DIR}/.env" | head -1 | cut -d= -f2-)"
_svc="$( grep -E '^POLARIS_DB_URL_SERVICE=' "${POLARIS_DIR}/.env" | head -1 | cut -d= -f2-)"
_sup="$( grep -E '^(SUPABASE_DB_URL|POLARIS_DATABASE_URL)=' "${POLARIS_DIR}/.env" | head -1 | cut -d= -f2-)"
if [ -z "${_anon}" ] || [ -z "${_svc}" ] || [ -z "${_sup}" ]; then
  echo "role-sync: one of POLARIS_DB_URL_ANON / _SERVICE / SUPABASE_DB_URL missing in ${POLARIS_DIR}/.env" >&2
  exit 2
fi

set +e
( cd "${POLARIS_DIR}" \
    && POLARIS_DB_URL_ANON="${_anon}" POLARIS_DB_URL_SERVICE="${_svc}" SUPABASE_DB_URL="${_sup}" \
       uv run python -m apps.api.scripts.sync_role_passwords )
rc=$?
set -e

case "${rc}" in
  0) echo "role-sync: roles aligned with .env ✓" ;;
  1) echo "role-sync: ALTER ran but pooler verify lagged (Supavisor cache) — benign, retry next cycle" >&2
     rc=0 ;;
  *) echo "role-sync: FAILED (exit ${rc}) — roles may be drifted; check env / uv / superuser reachability" >&2 ;;
esac
exit "${rc}"
