#!/usr/bin/env bash
#
# Build, push, and deploy Polaris-own images (api / portal / admin).
#
# Usage:
#   ./infra/build.sh --tag v1.1.0                     # build only (local test)
#   ./infra/build.sh --tag v1.1.0 --push              # build + push + update compose + git push
#   ./infra/build.sh --tag v1.1.0 --push --deploy     # ... + SSH → vps-update.sh  (rolling update)
#   ./infra/build.sh --tag v1.1.0 --push --install    # ... + SSH → vps-install.sh (first-time bootstrap)
#
# With --push the script also:
#   1. Updates the image tags in infra/docker-compose.prod.yml
#   2. Commits and pushes the compose change to origin/main
#
# --deploy and --install both read VPS_HOST from infra/deploy/deploy.env and
# require passwordless SSH access to the VPS.
#
# Tags applied per image:
#   ghcr.io/jiun01/polaris-{service}:{tag}       semver pin (what the compose uses)
#   ghcr.io/jiun01/polaris-{service}:sha-{sha}   git SHA traceability
#   ghcr.io/jiun01/polaris-{service}:latest      floating alias
#
# Flags:
#   --tag <v>      required — semver tag e.g. v1.1.0
#   --push         push to GHCR, update compose, git push
#   --deploy       after --push, SSH to VPS and run vps-update.sh
#   --install      after --push, SSH to VPS and run vps-install.sh
#   --no-latest    skip the :latest tag
#   --no-git       update compose file but skip the git commit/push step
#
set -euo pipefail

REGISTRY=ghcr.io/jiun01
TAG=
PUSH=false
DEPLOY=false
INSTALL=false
LATEST=true
GIT_COMMIT=true

while [[ $# -gt 0 ]]; do
  case $1 in
    --tag)       TAG=$2; shift 2 ;;
    --push)      PUSH=true; shift ;;
    --deploy)    DEPLOY=true; PUSH=true; shift ;;
    --install)   INSTALL=true; PUSH=true; shift ;;
    --no-latest) LATEST=false; shift ;;
    --no-git)    GIT_COMMIT=false; shift ;;
    *) echo "Unknown argument: $1" >&2; exit 1 ;;
  esac
done

[[ -n $TAG ]] || { echo "Usage: $0 --tag vX.Y.Z [--push] [--deploy|--install] [--no-latest] [--no-git]" >&2; exit 1; }

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_PROD="${REPO_ROOT}/infra/docker-compose.prod.yml"
DEPLOY_ENV="${REPO_ROOT}/infra/deploy/deploy.env"
GIT_SHA="$(git -C "${REPO_ROOT}" rev-parse --short HEAD)"
# Baked into each image as ENV VERSION → surfaced at /v1/health (api) and via
# `printenv VERSION` (portal/admin). Lets you confirm which build is live.
VERSION_STAMP="${TAG}-${GIT_SHA}"

log()  { printf '\n\033[1;36m▶ %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m! %s\033[0m\n' "$*"; }

# SSH to VPS_HOST (from deploy.env) and run the given script under the deploy dir.
vps_remote() {
  local script=$1
  if [[ ! -f "${DEPLOY_ENV}" ]]; then
    warn "deploy.env not found at ${DEPLOY_ENV} — skipping remote step"; return
  fi
  # shellcheck source=/dev/null
  source "${DEPLOY_ENV}"
  if [[ -z "${VPS_HOST:-}" ]]; then
    warn "VPS_HOST not set in deploy.env — skipping remote step"; return
  fi
  log "Running ${script} on ${VPS_HOST}"
  # Invoke via `bash`, not `./` — a fresh git checkout on the VPS can land the
  # script without its execute bit, which makes `./script` fail with exit 126.
  ssh "${VPS_HOST}" "cd ${DEPLOY_ROOT}/polaris/infra/deploy && bash ${script}"
  log "Remote step complete."
}

SERVICES=(api portal admin)

# ── 1. Build ─────────────────────────────────────────────────────────────────
for svc in "${SERVICES[@]}"; do
  image_tag="${REGISTRY}/polaris-${svc}:${TAG}"
  image_sha="${REGISTRY}/polaris-${svc}:sha-${GIT_SHA}"
  image_latest="${REGISTRY}/polaris-${svc}:latest"

  log "Building polaris-${svc} → ${image_tag} (sha-${GIT_SHA})"

  extra_tags=("-t" "${image_sha}")
  [[ $LATEST == true ]] && extra_tags+=("-t" "${image_latest}")

  docker build \
    --platform linux/amd64 \
    --build-arg VERSION="${VERSION_STAMP}" \
    -t "${image_tag}" \
    "${extra_tags[@]}" \
    -f "${REPO_ROOT}/apps/${svc}/Dockerfile" \
    "${REPO_ROOT}"
done

if [[ $PUSH == false ]]; then
  log "Build complete (local only). Run with --push to push to GHCR."
  exit 0
fi

# ── 2. Push ───────────────────────────────────────────────────────────────────
for svc in "${SERVICES[@]}"; do
  log "Pushing polaris-${svc}"
  docker push "${REGISTRY}/polaris-${svc}:${TAG}"
  docker push "${REGISTRY}/polaris-${svc}:sha-${GIT_SHA}"
  [[ $LATEST == true ]] && docker push "${REGISTRY}/polaris-${svc}:latest"
done

# ── 3. Update compose.prod.yml ───────────────────────────────────────────────
log "Bumping image tags in infra/docker-compose.prod.yml → ${TAG}"
for svc in "${SERVICES[@]}"; do
  sed -i "s|${REGISTRY}/polaris-${svc}:.*|${REGISTRY}/polaris-${svc}:${TAG}|" "${COMPOSE_PROD}"
done

# ── 4. Commit + push ─────────────────────────────────────────────────────────
if [[ $GIT_COMMIT == true ]]; then
  log "Committing compose tag bump"
  git -C "${REPO_ROOT}" add infra/docker-compose.prod.yml
  git -C "${REPO_ROOT}" diff --cached --quiet \
    && warn "compose.prod.yml unchanged — already on ${TAG}?" \
    || git -C "${REPO_ROOT}" commit -m "infra(prod): bump Polaris images to ${TAG}"
  git -C "${REPO_ROOT}" push origin main
else
  log "Skipped git commit (--no-git). Run git commit + push manually."
fi

# ── 5. Remote VPS step ───────────────────────────────────────────────────────
if [[ $DEPLOY == true ]]; then
  vps_remote "vps-update.sh"
elif [[ $INSTALL == true ]]; then
  vps_remote "vps-install.sh"
else
  log "Done. SSH to VPS and run vps-update.sh to deploy."
fi
