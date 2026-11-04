# syntax=docker/dockerfile:1.7
# Polaris admin — Next.js 14 standalone build

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
    pnpm install --frozen-lockfile=false --filter @polaris/admin...

# ---------- builder ----------
FROM node:22-slim AS builder
ENV PNPM_HOME=/pnpm
ENV PATH="$PNPM_HOME:$PATH"
RUN corepack enable && corepack prepare pnpm@9.12.0 --activate

WORKDIR /app
COPY --from=deps /app /app
COPY apps/admin apps/admin
COPY packages/shared-types packages/shared-types
# Re-link pnpm virtual store so .bin/next resolves correctly in this stage
RUN pnpm install --frozen-lockfile=false --filter @polaris/admin...

WORKDIR /app/apps/admin
RUN pnpm run build
# nodemailer is not captured by Next.js standalone file tracing (the tracer
# misses it because it is loaded via a dynamic string in next-auth's email
# provider path). Copy it to a known temp location for the runner stage.
RUN node -e "\
const r = require.resolve('nodemailer/package.json');\
const src = require('path').dirname(r);\
require('fs').cpSync(src, '/tmp/nodemailer', { recursive: true });\
console.log('nodemailer copied from', src);\
"

# ---------- runner ----------
FROM node:22-slim AS runner
ENV NODE_ENV=production
ENV PORT=3001
# Next.js standalone reads HOSTNAME to decide which interface to bind.
# Docker sets HOSTNAME to the container ID, causing it to bind only to the
# container's network IP (not 127.0.0.1). Override to bind all interfaces
# so the in-container healthcheck (localhost:3001) succeeds.
ENV HOSTNAME=0.0.0.0

# Install CA certificates so TLS connections to Supabase pooler (and any
# other remote HTTPS/TLS service) succeed. node:22-slim (Debian bookworm-slim)
# omits the ca-certificates package by default.
RUN apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates && \
    rm -rf /var/lib/apt/lists/*

RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin nextjs
WORKDIR /app

COPY --from=builder --chown=nextjs:nextjs /app/apps/admin/.next/standalone ./
COPY --from=builder --chown=nextjs:nextjs /app/apps/admin/.next/static ./apps/admin/.next/static
COPY --from=builder --chown=nextjs:nextjs /app/apps/admin/public ./apps/admin/public
# Restore nodemailer that standalone tracing dropped
COPY --from=builder --chown=nextjs:nextjs /tmp/nodemailer ./node_modules/nodemailer/

# Build-time version stamp (infra/build.sh --build-arg VERSION="<tag>-<sha>").
# Not rendered in the UI; available via `docker exec <ctr> printenv VERSION` to
# confirm which image is actually running. Defaults to "dev" for ad-hoc builds.
ARG VERSION=dev
ENV VERSION=${VERSION}

USER nextjs
EXPOSE 3001

HEALTHCHECK --interval=10s --timeout=3s --retries=5 \
  CMD node -e "fetch('http://localhost:3001').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"

CMD ["node", "apps/admin/server.js"]
