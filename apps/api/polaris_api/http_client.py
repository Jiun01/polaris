"""Shared httpx.AsyncClient singleton — audit Batch B (B6).

One ``httpx.AsyncClient`` per process, opened in the FastAPI lifespan and
closed at shutdown. Mirrors ``polaris_api.redis_pool``'s singleton pattern
exactly (same shape: module-global, ``init_*``/``close_*``/``get_*``).

Why a singleton: every outbound call on the inbound-message hot path
(``meta_whatsapp.py``, ``chatwoot/client.py``, ``booking/client.py``,
``media_download.py``) previously opened a FRESH ``httpx.AsyncClient`` per
call — a new TLS handshake per outbound HTTP request, including every
inbound-message Chatwoot mirror. httpx's ``AsyncClient`` already manages its
own connection pool (keep-alive) internally; constructing a new client per
request defeats that, same rationale as the Redis singleton.

``get_http_client_or_none()`` is the hot-path adoption hook: it returns
``None`` (no raise) when the singleton hasn't been initialised, so unit
tests / scripts that never run the lifespan silently fall back to a
per-call ``httpx.AsyncClient`` — the previous behaviour, verbatim. Callers
MUST pass their own per-call ``timeout=`` explicitly on every request made
through the shared client (its default 10s timeout is a floor, not a
per-call override).

``deps/health.py`` and ``routers/admin_health.py`` are intentionally NOT
adopted — health probes stay isolated per the audit's locked decisions.
"""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)

_client: httpx.AsyncClient | None = None

# Generous default — every adopted call site overrides this per-call with
# its own original timeout (booking's `self.timeout_s`, meta's
# `_HTTP_TIMEOUT_SECONDS`, media_download's per-fn values), so this default
# is effectively a floor, not the enforced value.
_DEFAULT_TIMEOUT_S = 10.0
_MAX_CONNECTIONS = 50
_MAX_KEEPALIVE_CONNECTIONS = 20


async def init_http_client() -> None:
    """Open the singleton client. Called from ``main.py`` lifespan startup.

    Idempotency note: re-calling raises rather than silently returning,
    same as ``init_redis`` — hides lifespan double-start bugs.
    """
    global _client  # noqa: PLW0603 — module-level singleton by design
    if _client is not None:
        raise RuntimeError("init_http_client called twice — lifespan double-start?")

    _client = httpx.AsyncClient(
        timeout=httpx.Timeout(_DEFAULT_TIMEOUT_S),
        limits=httpx.Limits(
            max_connections=_MAX_CONNECTIONS,
            max_keepalive_connections=_MAX_KEEPALIVE_CONNECTIONS,
        ),
    )
    logger.info("shared http client initialised")


async def close_http_client() -> None:
    """Drain + close. Called from ``main.py`` lifespan shutdown.

    Best-effort: cleanup must not raise into shutdown, which would
    otherwise obscure the underlying reason the process is stopping.
    """
    global _client  # noqa: PLW0603
    if _client is None:
        return
    try:
        await _client.aclose()
    except Exception as exc:  # noqa: BLE001 — cleanup must not raise
        logger.warning("http client close failed: %s", type(exc).__name__)
        logger.debug("http client close error detail: %s", exc)
    _client = None


def get_http_client_or_none() -> httpx.AsyncClient | None:
    """Return the singleton client, or ``None`` if uninitialised.

    The hot-path adoption hook (audit Batch B / B6): call sites in
    meta_whatsapp.py / chatwoot/client.py / booking/client.py /
    media_download.py use this to prefer the shared client in prod (where
    the lifespan always initialises it) while falling back to a fresh
    per-call ``httpx.AsyncClient`` in unit tests / scripts that never run
    the lifespan — preserving every existing ``monkeypatch``-the-module-
    attribute test unchanged.
    """
    return _client


__all__ = [
    "close_http_client",
    "get_http_client_or_none",
    "init_http_client",
]
