# syntax=docker/dockerfile:1.7
# Polaris portal — Next.js 14 standalone build

# ---------- deps ----------
FROM node:22-slim AS deps
ENV PNPM_HOME=/pnpm
ENV PATH="$PNPM_HOME:$PATH"
RUN corepack enable && corepack prepare pnpm@9.12.0 --activate

WORKDIR /app
COPY package.json pnpm-lock.yaml* pnpm-workspace.yaml ./
COPY apps/portal/package.json apps/portal/
COPY apps/admin/package.json apps/admin/
COPY packages/shared-types/package.json packages/shared-types/
RUN --mount=type=cache,id=pnpm,target=/pnpm/store \
    pnpm install --frozen-lockfile=false --filter @polaris/portal...

# ---------- builder ----------
FROM node:22-slim AS builder
ENV PNPM_HOME=/pnpm
ENV PATH="$PNPM_HOME:$PATH"
RUN corepack enable && corepack prepare pnpm@9.12.0 --activate

WORKDIR /app
COPY --from=deps /app /app
COPY apps/portal apps/portal
COPY packages/shared-types packages/shared-types
# Re-link pnpm virtual store so .bin/next resolves correctly in this stage
RUN pnpm install --frozen-lockfile=false --filter @polaris/portal...

WORKDIR /app/apps/portal
RUN pnpm run build

# ---------- runner ----------
FROM node:22-slim AS runner
ENV NODE_ENV=production
ENV PORT=3000
# Next.js standalone reads HOSTNAME to decide which interface to bind.
# Docker sets HOSTNAME to the container ID, causing it to bind only to the
# Docker network IP and not localhost. Override to bind all interfaces so
# the healthcheck (localhost:3000) succeeds.
ENV HOSTNAME=0.0.0.0

RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin nextjs
WORKDIR /app

COPY --from=builder --chown=nextjs:nextjs /app/apps/portal/.next/standalone ./
COPY --from=builder --chown=nextjs:nextjs /app/apps/portal/.next/static ./apps/portal/.next/static
COPY --from=builder --chown=nextjs:nextjs /app/apps/portal/public ./apps/portal/public

# Build-time version stamp (infra/build.sh --build-arg VERSION="<tag>-<sha>").
# Not rendered in the UI; available via `docker exec <ctr> printenv VERSION` to
# confirm which image is actually running. Defaults to "dev" for ad-hoc builds.
ARG VERSION=dev
ENV VERSION=${VERSION}

USER nextjs
EXPOSE 3000

HEALTHCHECK --interval=10s --timeout=3s --retries=5 \
  CMD node -e "fetch('http://localhost:3000').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"

CMD ["node", "apps/portal/server.js"]
