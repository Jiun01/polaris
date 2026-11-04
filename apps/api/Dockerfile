# syntax=docker/dockerfile:1.7
# Polaris v2 API — multi-stage build with uv

# ---------- builder ----------
FROM python:3.12-slim AS builder

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install uv — pin to a specific version (NOT a moving `latest` or major-only
# tag) so the build is reproducible and supply-chain substitution is blocked.
# Tracks the same major series CI uses (astral-sh/setup-uv@v6 → uv 0.11.x).
# Bump deliberately; Renovate/Dependabot can automate.
COPY --from=ghcr.io/astral-sh/uv:0.11.11 /uv /usr/local/bin/uv

WORKDIR /build

# Copy lockfile + pyproject first for cache reuse
COPY apps/api/pyproject.toml ./apps/api/pyproject.toml
COPY packages/shared-types/python ./packages/shared-types/python
COPY pyproject.toml ./pyproject.toml

# Create venv and install deps for the api package only
# (temp file avoids bash process substitution, which /bin/sh doesn't support)
RUN uv venv /opt/venv \
    && uv pip compile --no-header apps/api/pyproject.toml -o /tmp/api-reqs.txt \
    && uv pip install --python /opt/venv/bin/python --no-cache -r /tmp/api-reqs.txt

# Copy source
COPY apps/api/polaris_api ./apps/api/polaris_api

# Install shared types then the API package itself
RUN uv pip install --python /opt/venv/bin/python --no-cache ./packages/shared-types/python \
    && uv pip install --python /opt/venv/bin/python --no-cache ./apps/api

# ---------- runtime ----------
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH"

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates \
    # WeasyPrint native deps (invoice/quote PDF rendering). gobject-2.0 comes
    # from libglib2.0-0 (pulled by libpangoft2); pango/cairo/gdk-pixbuf render
    # the HTML→PDF; fontconfig + a base font are needed or text renders blank.
    libpango-1.0-0 libpangoft2-1.0-0 libgdk-pixbuf-2.0-0 \
    libcairo2 libffi8 libharfbuzz0b fontconfig fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home --shell /usr/sbin/nologin polaris

COPY --from=builder /opt/venv /opt/venv

# Invoice/quote PDF Jinja templates — the source layout isn't present in the
# installed image, so ship them explicitly + point pdf_renderer at them.
COPY infra/templates /opt/polaris/templates
ENV POLARIS_TEMPLATES_DIR=/opt/polaris/templates

USER polaris
WORKDIR /home/polaris

# Build-time version stamp. infra/build.sh passes --build-arg VERSION="<tag>-<sha>".
# Pydantic Settings (case-insensitive, no prefix) reads the VERSION env var into
# settings.version, surfaced at GET /v1/health. Defaults to "dev" for ad-hoc builds.
ARG VERSION=dev
ENV VERSION=${VERSION}

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=3s --retries=5 \
  CMD curl -fsS http://localhost:8000/v1/health || exit 1

CMD ["uvicorn", "polaris_api.main:app", "--host", "::", "--port", "8000"]
