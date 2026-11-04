# docker-bake.hcl — parallel build definition for the Polaris release pipeline.
#
# Driven by infra/ship.sh (never call this by hand unless you know the vars).
# ship.sh exports ROOT + the per-component tags as env vars; buildx bake reads
# any env var whose name matches a `variable` block below.
#
# Targets:
#   api / portal / admin  — Polaris-own (context = repo root, ARG VERSION)
#   ea                    — EA fork    (context = ../polaris-ea-fork, target production)
#   cw                    — Chatwoot   (context = ../polaris-cw-fork, docker/Dockerfile)
#
# NOTE: the contexts are the operator's PRIVATE sibling checkouts — prod builds
# carry the enterprise overlay (ADR-060). The forks/ submodules in this repo
# are the public CE reference copies, not the deploy-time build sources.
#
# All run CONCURRENTLY when passed together, e.g.
#   docker buildx bake -f infra/docker-bake.hcl api portal admin ea cw --push
#
# ROOT is injected as an ABSOLUTE path by ship.sh so the sibling-fork contexts
# (${ROOT}/../polaris-ea-fork) resolve regardless of the caller's cwd.

variable "ROOT"          { default = "." }
variable "REGISTRY"      { default = "ghcr.io/jiun01" }
variable "APP_TAG"       { default = "dev" }
variable "EA_TAG"        { default = "dev" }
variable "CW_TAG"        { default = "dev" }
variable "GIT_SHA"       { default = "unknown" }
variable "VERSION_STAMP" { default = "dev" }
variable "LATEST"        { default = "true" }   # "true" → also tag :latest

# api/portal/admin get :TAG + :sha-<sha> (+ :latest), mirroring infra/build.sh.
function "app_tags" {
  params = [svc]
  result = LATEST == "true" ? [
    "${REGISTRY}/polaris-${svc}:${APP_TAG}",
    "${REGISTRY}/polaris-${svc}:sha-${GIT_SHA}",
    "${REGISTRY}/polaris-${svc}:latest",
  ] : [
    "${REGISTRY}/polaris-${svc}:${APP_TAG}",
    "${REGISTRY}/polaris-${svc}:sha-${GIT_SHA}",
  ]
}

# Forks get :TAG (+ :latest) only — no sha tag, matching their fork build.sh.
function "fork_tags" {
  params = [name, tag]
  result = LATEST == "true" ? [
    "${REGISTRY}/polaris-${name}:${tag}",
    "${REGISTRY}/polaris-${name}:latest",
  ] : [
    "${REGISTRY}/polaris-${name}:${tag}",
  ]
}

target "_app" {
  context   = "${ROOT}"
  platforms = ["linux/amd64"]
  args      = { VERSION = "${VERSION_STAMP}" }
}

target "api" {
  inherits   = ["_app"]
  dockerfile = "apps/api/Dockerfile"
  tags       = app_tags("api")
}

target "portal" {
  inherits   = ["_app"]
  dockerfile = "apps/portal/Dockerfile"
  tags       = app_tags("portal")
}

target "admin" {
  inherits   = ["_app"]
  dockerfile = "apps/admin/Dockerfile"
  tags       = app_tags("admin")
}

target "ea" {
  context    = "${ROOT}/../polaris-ea-fork"
  dockerfile = "Dockerfile"
  target     = "production"
  platforms  = ["linux/amd64"]
  # Stamp the unique per-build tag into asset_url()'s cache-busting token so every
  # EA image auto-busts all front-end asset URLs (no manual app.php bump).
  args       = { EA_ASSET_TOKEN = "${EA_TAG}" }
  tags       = fork_tags("ea", EA_TAG)
}

target "cw" {
  context    = "${ROOT}/../polaris-cw-fork"
  dockerfile = "docker/Dockerfile"
  platforms  = ["linux/amd64"]
  tags       = fork_tags("cw", CW_TAG)
}

# `bake` with no explicit targets builds just the Polaris-own trio.
group "default" {
  targets = ["api", "portal", "admin"]
}
