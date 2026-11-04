#!/usr/bin/env bash
#
# ship.sh — one deterministic Polaris release pipeline.
#
# Drives commit → merge → prune → parallel build → push → compose-bump → push,
# for the Polaris-own images (api/portal/admin) plus the EA and CW forks when
# they have changes. Stops BEFORE deploy: it prints the vps-update.sh line for
# you to run on the VPS. Designed to be invoked stage-by-stage by the
# /polaris-build-image skill (so a Sonnet agent absorbs the verbose build logs),
# or end-to-end with `ship.sh all`.
#
# Subcommands:
#   preflight   detect branch/changes, compute next tags, write .ship-state.env, print the plan
#   gitsync     verify gate → commit → (merge feature→main + prune) | (push main)
#   build       parallel `docker buildx bake` of every needed image → push to GHCR
#   release     bump image pins in docker-compose.prod.yml → commit → push → print VPS line
#   all         preflight → gitsync → build → release, non-interactive
#
# Common flags (apply to preflight unless noted):
#   --app-tag <v>     override the auto-bumped app tag (e.g. v1.0.40)
#   --ea-tag <v>      override the auto-bumped EA tag   (e.g. v1.6.0-r6)
#   --cw-tag <v>      override the auto-bumped CW tag   (e.g. v4.14.2-r4)
#   --ea / --no-ea    force-include / force-skip the EA fork build
#   --cw / --no-cw    force-include / force-skip the CW fork build
#   --no-verify       skip the verify gate (gitsync)
#   --no-app          skip api/portal/admin (fork-only ship; no app bump/build)
#   --no-latest       do not push the :latest alias
#   --sequential      build images one-at-a-time instead of in parallel
#   --verbose         stream full command output (default: quiet, logs to .ship-logs/)
#   --branch <name>   override the detected feature branch (gitsync)
#   --main <name>     override the integration branch name (default: main)
#   -m <msg>          commit message for the working-tree snapshot (gitsync)
#
set -euo pipefail

# ── Paths ────────────────────────────────────────────────────────────────────
INFRA_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${INFRA_DIR}/.." && pwd)"            # polaris/
WORKSPACE_ROOT="$(cd "${REPO_ROOT}/.." && pwd)"      # Chatbot System/
# Sibling PRIVATE fork checkouts are the deploy-time build sources (prod keeps
# the Chatwoot enterprise overlay, ADR-060) — NOT the forks/ submodules, which
# are the public CE reference copies.
EA_DIR="${WORKSPACE_ROOT}/polaris-ea-fork"
CW_DIR="${WORKSPACE_ROOT}/polaris-cw-fork"
COMPOSE_PROD="${INFRA_DIR}/docker-compose.prod.yml"
BAKE_FILE="${INFRA_DIR}/docker-bake.hcl"
STATE_FILE="${INFRA_DIR}/.ship-state.env"
LOG_DIR="${INFRA_DIR}/.ship-logs"
ENV_FILE="${REPO_ROOT}/.env"
DEPLOY_ENV="${INFRA_DIR}/deploy/deploy.env"

REGISTRY="ghcr.io/jiun01"
GHCR_USER="jiun01"
BUILDER="polaris-ship"

# ── Defaults (overridable by flags) ──────────────────────────────────────────
QUIET="${SHIP_QUIET:-1}"
NO_VERIFY=0
BUILD_APP=1       # --no-app sets 0: skip api/portal/admin (fork-only ship)
LATEST=true
SEQUENTIAL=0
MAIN_BRANCH="main"
COMMIT_MSG=""
OVERRIDE_APP_TAG=""
OVERRIDE_EA_TAG=""
OVERRIDE_CW_TAG=""
FORCE_EA=""      # "1" include, "0" skip, "" auto
FORCE_CW=""
OVERRIDE_BRANCH=""

# ── Pretty logging ───────────────────────────────────────────────────────────
log()  { printf '\n\033[1;36m▶ %s\033[0m\n' "$*"; }
ok()   { printf '\033[1;32m✔ %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m! %s\033[0m\n' "$*" >&2; }
die()  { printf '\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

# Run a command; in quiet mode capture to a log and only surface the tail on
# failure, so an agent's context never ingests a full build log.
run_logged() {
  local label="$1"; shift
  mkdir -p "${LOG_DIR}"
  local logf="${LOG_DIR}/$(printf '%s' "$label" | tr -c 'a-zA-Z0-9_.-' '_').log"
  if [[ "$QUIET" == "1" ]]; then
    if ! "$@" >"${logf}" 2>&1; then
      warn "step '${label}' failed — tail of ${logf}:"
      tail -n 40 "${logf}" >&2
      return 1
    fi
  else
    "$@" 2>&1 | tee "${logf}"
    return "${PIPESTATUS[0]}"
  fi
}

# ── Helpers ──────────────────────────────────────────────────────────────────

# Read the live tag of an image from the prod compose (first match).
live_tag() {  # $1 = image short name (api|portal|admin|ea|cw)
  local m
  m="$(grep -oE "${REGISTRY}/polaris-$1:[^[:space:]\"']+" "${COMPOSE_PROD}" | head -1)"
  printf '%s' "${m##*:}"   # everything after the last colon = the tag
}

# v1.0.39 → v1.0.40 (bump the final dotted numeric segment).
bump_dotted() {
  local cur="$1" base last
  base="${cur%.*}"; last="${cur##*.}"
  [[ "$last" =~ ^[0-9]+$ ]] || die "cannot auto-bump app tag '${cur}' — pass --app-tag"
  printf '%s.%s' "${base}" "$((last + 1))"
}

# v1.6.0-r5 → v1.6.0-r6 (bump the -rN release suffix; add -r1 if absent).
bump_release() {
  local cur="$1"
  if [[ "$cur" =~ -r([0-9]+)$ ]]; then
    printf '%s-r%s' "${cur%-r*}" "$(( ${BASH_REMATCH[1]} + 1 ))"
  else
    printf '%s-r1' "${cur}"
  fi
}

# Decide whether a fork needs rebuilding. Echoes a reason word:
#   dirty | ahead:<n> | untagged | clean | no-git
# Anything other than clean/no-git counts as "needs build".
fork_status() {  # $1 = fork dir, $2 = live tag, $3 = git-tag prefix ("polaris/" | "")
  local dir="$1" live="$2" prefix="$3" gittag
  [[ -d "${dir}/.git" ]] || { echo "no-git"; return; }
  if [[ -n "$(git -C "${dir}" status --porcelain 2>/dev/null)" ]]; then echo "dirty"; return; fi
  gittag="${prefix}${live}"
  if git -C "${dir}" rev-parse -q --verify "refs/tags/${gittag}" >/dev/null 2>&1; then
    local n; n="$(git -C "${dir}" rev-list "${gittag}..HEAD" --count 2>/dev/null || echo 0)"
    [[ "$n" -gt 0 ]] && echo "ahead:${n}" || echo "clean"
  else
    echo "untagged"
  fi
}

# Build a fork ONLY when its working tree has REAL modifications: "dirty"
# (uncommitted changes) or "ahead:N" (commits past the live tag). "untagged"
# (the live git tag is just absent locally — common when fork tags were created
# on another machine) and "clean" are NOT modifications, so they skip by default
# — rebuilding them would only re-push functionally identical images at a new tag.
# Pass --ea/--cw to force a rebuild when you know an untagged fork has real changes.
needs_build() { case "$1" in dirty|ahead:*) return 0 ;; *) return 1 ;; esac; }

# Log in to GHCR using the token from polaris/.env (extract ONLY that line —
# never source the whole secrets file). Idempotent; safe to call per stage.
ghcr_login() {
  local tok="${GITHUB_TOKEN:-}"
  if [[ -z "$tok" && -f "${ENV_FILE}" ]]; then
    tok="$(sed -n 's/^[[:space:]]*GITHUB_TOKEN[[:space:]]*=//p' "${ENV_FILE}" | tail -n1 | tr -d '\r')"
    tok="${tok#\"}"; tok="${tok%\"}"; tok="${tok#\'}"; tok="${tok%\'}"
  fi
  [[ -n "$tok" ]] || die "GITHUB_TOKEN not set and not found in ${ENV_FILE} — cannot push to GHCR"
  echo "$tok" | docker login ghcr.io -u "${GHCR_USER}" --password-stdin >/dev/null 2>&1 \
    || die "docker login ghcr.io failed — check the GITHUB_TOKEN (needs write:packages)"
}

load_state() {
  [[ -f "${STATE_FILE}" ]] || die "no ${STATE_FILE} — run 'ship.sh preflight' first"
  # shellcheck source=/dev/null
  source "${STATE_FILE}"
}

# ── Subcommand: preflight ────────────────────────────────────────────────────
cmd_preflight() {
  command -v docker >/dev/null || die "docker not found"
  command -v git    >/dev/null || die "git not found"
  [[ -f "${COMPOSE_PROD}" ]] || die "compose not found: ${COMPOSE_PROD}"

  local sha branch dirty
  sha="$(git -C "${REPO_ROOT}" rev-parse --short HEAD)"
  branch="${OVERRIDE_BRANCH:-$(git -C "${REPO_ROOT}" rev-parse --abbrev-ref HEAD)}"
  dirty=0; [[ -n "$(git -C "${REPO_ROOT}" status --porcelain)" ]] && dirty=1

  # App tag. With --no-app we keep the current tag (no bump, no rebuild).
  local cur_app app_tag
  cur_app="$(live_tag api)"; [[ -n "$cur_app" ]] || die "could not read live polaris-api tag from compose"
  if [[ "$BUILD_APP" == 1 ]]; then
    app_tag="${OVERRIDE_APP_TAG:-$(bump_dotted "$cur_app")}"
  else
    app_tag="${OVERRIDE_APP_TAG:-$cur_app}"
  fi

  # EA fork.
  local ea_status ea_build cur_ea ea_tag
  ea_status="$(fork_status "${EA_DIR}" "$(live_tag ea)" "polaris/")"
  cur_ea="$(live_tag ea)"; ea_tag="${OVERRIDE_EA_TAG:-$(bump_release "${cur_ea:-v0.0.0}")}"
  if   [[ "$FORCE_EA" == "1" ]]; then ea_build=1
  elif [[ "$FORCE_EA" == "0" ]]; then ea_build=0
  elif needs_build "$ea_status"; then ea_build=1; else ea_build=0; fi

  # CW fork.
  local cw_status cw_build cur_cw cw_tag
  cw_status="$(fork_status "${CW_DIR}" "$(live_tag cw)" "")"
  cur_cw="$(live_tag cw)"; cw_tag="${OVERRIDE_CW_TAG:-$(bump_release "${cur_cw:-v0.0.0}")}"
  if   [[ "$FORCE_CW" == "1" ]]; then cw_build=1
  elif [[ "$FORCE_CW" == "0" ]]; then cw_build=0
  elif needs_build "$cw_status"; then cw_build=1; else cw_build=0; fi

  local targets=""
  [[ "$BUILD_APP" == "1" ]] && targets="api portal admin"
  [[ "$ea_build" == "1" ]] && targets="${targets:+$targets }ea"
  [[ "$cw_build" == "1" ]] && targets="${targets:+$targets }cw"
  [[ -n "$targets" ]] || die "nothing to build (--no-app and no fork changes) — adjust flags"

  ghcr_login   # fail fast on a bad token before any heavy work

  cat > "${STATE_FILE}" <<EOF
# Written by ship.sh preflight — consumed by gitsync/build/release. Gitignored.
SHIP_GIT_SHA="${sha}"
SHIP_BRANCH="${branch}"
SHIP_MAIN_BRANCH="${MAIN_BRANCH}"
SHIP_DIRTY="${dirty}"
SHIP_BUILD_APP="${BUILD_APP}"
SHIP_APP_TAG="${app_tag}"
SHIP_EA_TAG="${ea_tag}"
SHIP_CW_TAG="${cw_tag}"
SHIP_EA_BUILD="${ea_build}"
SHIP_CW_BUILD="${cw_build}"
SHIP_TARGETS="${targets}"
SHIP_VERSION_STAMP="${app_tag}-${sha}"
SHIP_LATEST="${LATEST}"
SHIP_NO_VERIFY="${NO_VERIFY}"
SHIP_SEQUENTIAL="${SEQUENTIAL}"
SHIP_COMMIT_MSG="${COMMIT_MSG}"
EOF

  log "Preflight plan"
  printf '  branch        : %s%s\n' "${branch}" "$([[ "$dirty" == 1 ]] && echo '  (dirty — will commit)')"
  if [[ "$BUILD_APP" == 1 ]]; then
    printf '  app images    : api portal admin  → %s  (was %s)\n' "${app_tag}" "${cur_app}"
  else
    printf '  app images    : skip (--no-app)  (stay %s)\n' "${cur_app}"
  fi
  printf '  EA fork       : %s  [%s]%s\n' "$([[ "$ea_build" == 1 ]] && echo "BUILD → ${ea_tag}  (was ${cur_ea})" || echo "skip")" "${ea_status}" ""
  printf '  CW fork       : %s  [%s]\n' "$([[ "$cw_build" == 1 ]] && echo "BUILD → ${cw_tag}  (was ${cur_cw})" || echo "skip")" "${cw_status}"
  printf '  verify gate   : %s\n' "$([[ "$NO_VERIFY" == 1 ]] && echo 'SKIPPED (--no-verify)' || echo 'on')"
  printf '  build mode    : %s\n' "$([[ "$SEQUENTIAL" == 1 ]] && echo 'sequential' || echo 'parallel')"
  if { [[ "$ea_build" == 0 && "$ea_status" == untagged ]]; } || { [[ "$cw_build" == 0 && "$cw_status" == untagged ]]; }; then
    printf '  note          : untagged fork(s) skipped (live tag not found locally, no local changes); pass --ea/--cw to force a rebuild\n'
  fi
  ok "State written to ${STATE_FILE}"
}

# ── Subcommand: gitsync ──────────────────────────────────────────────────────
cmd_gitsync() {
  load_state
  [[ "${NO_VERIFY}" == "1" ]] && SHIP_NO_VERIFY=1  # CLI flag overrides state file
  cd "${REPO_ROOT}"

  # 1. Verify gate ------------------------------------------------------------
  if [[ "${SHIP_NO_VERIFY}" != "1" ]]; then
    log "Verify gate"
    if command -v make >/dev/null && grep -qE '^verify:' Makefile 2>/dev/null; then
      run_logged verify make verify || die "verify gate failed — fix, or re-run with --no-verify"
    else
      run_logged verify-ruff     uv run ruff check .            || die "ruff failed"
      run_logged verify-prettier pnpm exec prettier --check .   || die "prettier failed"
      run_logged verify-pytest   uv run pytest -q               || die "pytest failed"
    fi
    ok "Verify passed"
  else
    warn "verify gate skipped"
  fi

  # 2. Commit working tree ----------------------------------------------------
  if [[ "${SHIP_DIRTY}" == "1" ]]; then
    local msg="${SHIP_COMMIT_MSG:-chore: snapshot before release ${SHIP_APP_TAG}}"
    log "Committing working tree"
    git add -A
    git commit -m "${msg}"
    ok "Committed: ${msg}"
  else
    log "Working tree clean — nothing to commit"
  fi

  # 3. Integrate to main + prune ---------------------------------------------
  local main="${SHIP_MAIN_BRANCH}" branch="${SHIP_BRANCH}"
  if [[ "${branch}" == "${main}" || "${branch}" == "master" || "${branch}" == "HEAD" ]]; then
    log "On ${branch} — pushing directly"
    git push origin "${branch}"
    ok "Pushed ${branch}"
  else
    log "Merging ${branch} → ${main} and pruning"
    git checkout "${main}"
    git pull --ff-only origin "${main}" || die "could not fast-forward ${main} — resolve manually"
    if ! git merge --no-ff "${branch}" -m "merge ${branch} → ${main} (${SHIP_APP_TAG})"; then
      git merge --abort || true
      die "merge conflict ${branch} → ${main} — resolve manually, then re-run from 'build'"
    fi
    git push origin "${main}"
    git branch -d "${branch}" || warn "could not delete local ${branch} (unmerged?)"
    git push origin --delete "${branch}" 2>/dev/null || warn "no remote ${branch} to delete"
    ok "Merged ${branch} → ${main} and pruned"
  fi
}

# ── Subcommand: build ────────────────────────────────────────────────────────
cmd_build() {
  load_state
  ghcr_login
  cd "${REPO_ROOT}"

  # Ensure a container-driver builder exists (required to --push multiple targets).
  if ! docker buildx inspect "${BUILDER}" >/dev/null 2>&1; then
    log "Creating buildx builder '${BUILDER}'"
    docker buildx create --name "${BUILDER}" --driver docker-container >/dev/null
  fi
  docker buildx inspect "${BUILDER}" --bootstrap >/dev/null 2>&1 || true

  export ROOT="${REPO_ROOT}" REGISTRY \
         APP_TAG="${SHIP_APP_TAG}" EA_TAG="${SHIP_EA_TAG}" CW_TAG="${SHIP_CW_TAG}" \
         GIT_SHA="${SHIP_GIT_SHA}" VERSION_STAMP="${SHIP_VERSION_STAMP}" LATEST="${SHIP_LATEST}"

  log "Building & pushing: ${SHIP_TARGETS}"
  if [[ "${SHIP_SEQUENTIAL}" == "1" ]]; then
    local t
    for t in ${SHIP_TARGETS}; do
      log "  → ${t}"
      run_logged "build-${t}" \
        docker buildx bake --builder "${BUILDER}" -f "${BAKE_FILE}" "${t}" --push \
        || die "build failed: ${t}"
    done
  else
    # shellcheck disable=SC2086
    run_logged build-all \
      docker buildx bake --builder "${BUILDER}" -f "${BAKE_FILE}" ${SHIP_TARGETS} --push \
      || die "parallel build failed — retry with --sequential if it OOM'd"
  fi
  ok "Pushed to GHCR: ${SHIP_TARGETS} (${SHIP_APP_TAG})"
}

# ── Subcommand: release ──────────────────────────────────────────────────────
cmd_release() {
  load_state
  cd "${REPO_ROOT}"

  : "${SHIP_BUILD_APP:=1}"   # default for state files written before --no-app existed

  log "Bumping image pins in docker-compose.prod.yml"
  local svc
  if [[ "${SHIP_BUILD_APP}" == "1" ]]; then
    for svc in api portal admin; do
      sed -i "s|${REGISTRY}/polaris-${svc}:[^[:space:]\"']*|${REGISTRY}/polaris-${svc}:${SHIP_APP_TAG}|g" "${COMPOSE_PROD}"
    done
  fi
  [[ "${SHIP_EA_BUILD}" == "1" ]] && \
    sed -i "s|${REGISTRY}/polaris-ea:[^[:space:]\"']*|${REGISTRY}/polaris-ea:${SHIP_EA_TAG}|g" "${COMPOSE_PROD}"
  # CW image is referenced twice (chatwoot-rails + chatwoot-sidekiq) — global sed hits both.
  [[ "${SHIP_CW_BUILD}" == "1" ]] && \
    sed -i "s|${REGISTRY}/polaris-cw:[^[:space:]\"']*|${REGISTRY}/polaris-cw:${SHIP_CW_TAG}|g" "${COMPOSE_PROD}"

  git add infra/docker-compose.prod.yml
  if git diff --cached --quiet; then
    warn "compose.prod.yml unchanged — tags already current?"
  else
    local bumped=""
    [[ "${SHIP_BUILD_APP}" == "1" ]] && bumped="app=${SHIP_APP_TAG}"
    [[ "${SHIP_EA_BUILD}" == "1" ]] && bumped="${bumped:+$bumped }ea=${SHIP_EA_TAG}"
    [[ "${SHIP_CW_BUILD}" == "1" ]] && bumped="${bumped:+$bumped }cw=${SHIP_CW_TAG}"
    git commit -m "infra(prod): bump images (${bumped})"
    git push origin "${SHIP_MAIN_BRANCH}"
    ok "Compose bumped & pushed (${bumped})"
  fi

  # Summary + VPS hand-off line.
  local vps_host="" deploy_root=""
  if [[ -f "${DEPLOY_ENV}" ]]; then
    vps_host="$(sed -n 's/^[[:space:]]*VPS_HOST[[:space:]]*=//p'   "${DEPLOY_ENV}" | tail -n1 | tr -d '\r"'"'")"
    deploy_root="$(sed -n 's/^[[:space:]]*DEPLOY_ROOT[[:space:]]*=//p' "${DEPLOY_ENV}" | tail -n1 | tr -d '\r"'"'")"
  fi
  : "${deploy_root:=/opt/polaris}"

  log "Shipped — pushed to GHCR and pinned in compose:"
  [[ "${SHIP_BUILD_APP}" == "1" ]] && printf '    api/portal/admin : %s\n' "${SHIP_APP_TAG}"
  [[ "${SHIP_EA_BUILD}" == "1" ]] && printf '    polaris-ea       : %s  (vps-update.sh auto-refreshes the ea_app volume)\n' "${SHIP_EA_TAG}"
  [[ "${SHIP_CW_BUILD}" == "1" ]] && printf '    polaris-cw       : %s  (chatwoot-rails + chatwoot-sidekiq)\n' "${SHIP_CW_TAG}"
  printf '\n\033[1;32m▶ Next, on the VPS:\033[0m\n'
  if [[ -n "${vps_host}" ]]; then
    printf "    ssh %s 'cd %s/polaris/infra/deploy && bash vps-update.sh'\n" "${vps_host}" "${deploy_root}"
  else
    printf "    ssh <vps-host> 'cd %s/polaris/infra/deploy && bash vps-update.sh'\n" "${deploy_root}"
  fi
  rm -f "${STATE_FILE}"
}

# ── Arg parsing ──────────────────────────────────────────────────────────────
[[ $# -gt 0 ]] || die "usage: ship.sh {preflight|gitsync|build|release|all} [flags]"
SUBCMD="$1"; shift

while [[ $# -gt 0 ]]; do
  case "$1" in
    --app-tag)   OVERRIDE_APP_TAG="$2"; shift 2 ;;
    --ea-tag)    OVERRIDE_EA_TAG="$2";  shift 2 ;;
    --cw-tag)    OVERRIDE_CW_TAG="$2";  shift 2 ;;
    --ea)        FORCE_EA=1; shift ;;
    --no-ea)     FORCE_EA=0; shift ;;
    --cw)        FORCE_CW=1; shift ;;
    --no-cw)     FORCE_CW=0; shift ;;
    --no-verify) NO_VERIFY=1; shift ;;
    --no-app)    BUILD_APP=0; shift ;;
    --no-latest) LATEST=false; shift ;;
    --sequential) SEQUENTIAL=1; shift ;;
    --verbose)   QUIET=0; shift ;;
    --branch)    OVERRIDE_BRANCH="$2"; shift 2 ;;
    --main)      MAIN_BRANCH="$2"; shift 2 ;;
    -m)          COMMIT_MSG="$2"; shift 2 ;;
    *) die "unknown flag: $1" ;;
  esac
done

case "${SUBCMD}" in
  preflight) cmd_preflight ;;
  gitsync)   cmd_gitsync ;;
  build)     cmd_build ;;
  release)   cmd_release ;;
  all)       cmd_preflight; cmd_gitsync; cmd_build; cmd_release ;;
  *) die "unknown subcommand '${SUBCMD}' (preflight|gitsync|build|release|all)" ;;
esac
