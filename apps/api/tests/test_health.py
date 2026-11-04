"""Tests for GET /health.

Covers happy path + Supabase-down + Redis-down + both-down. All return HTTP
200 by design (health endpoint must not 5xx itself).

Strategy: override the `gather_probes` dependency with a stub that returns
deterministic ProbeResult tuples. This is the FastAPI-idiomatic way to test
routes that depend on side-effecting probes, and is independent of where
`probe_supabase` / `probe_redis` live in the import graph.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from httpx import AsyncClient
from polaris_api.deps.health import ProbeResult
from polaris_api.main import app
from polaris_api.routers.health import gather_probes


def _override(supabase_ok: bool, redis_ok: bool) -> AsyncIterator[None]:
    async def _stub() -> tuple[ProbeResult, ProbeResult]:
        return (
            ProbeResult(reachable=supabase_ok),
            ProbeResult(reachable=redis_ok),
        )

    app.dependency_overrides[gather_probes] = _stub


def _clear() -> None:
    app.dependency_overrides.pop(gather_probes, None)


@pytest.mark.asyncio
async def test_health_returns_ok_when_all_reachable(client: AsyncClient) -> None:
    _override(supabase_ok=True, redis_ok=True)
    try:
        resp = await client.get("/v1/health")
    finally:
        _clear()
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["supabase"] == "reachable"
    assert body["redis"] == "reachable"
    assert "version" in body


@pytest.mark.asyncio
async def test_health_returns_degraded_when_supabase_down(client: AsyncClient) -> None:
    _override(supabase_ok=False, redis_ok=True)
    try:
        resp = await client.get("/v1/health")
    finally:
        _clear()
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["supabase"] == "unreachable"
    assert body["redis"] == "reachable"


@pytest.mark.asyncio
async def test_health_returns_degraded_when_redis_down(client: AsyncClient) -> None:
    _override(supabase_ok=True, redis_ok=False)
    try:
        resp = await client.get("/v1/health")
    finally:
        _clear()
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["supabase"] == "reachable"
    assert body["redis"] == "unreachable"


@pytest.mark.asyncio
async def test_health_returns_down_when_both_unreachable(client: AsyncClient) -> None:
    _override(supabase_ok=False, redis_ok=False)
    try:
        resp = await client.get("/v1/health")
    finally:
        _clear()
    assert resp.status_code == 200
    assert resp.json()["status"] == "down"
