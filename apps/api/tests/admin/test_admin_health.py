"""Admin health endpoint — Phase 9 PR-6 tests (TDD).

Coverage:
  * Auth gate: missing JWT → 401, anon role → 403.
  * GET /health: happy path returns all 5 signals with correct shape.
  * GET /health: degraded Supabase (mocked to raise) → tile shows error but
    other 4 signals still return valid data.
  * GET /health: response matches HealthSnapshot schema.
  * GET /health: all 5 signals aggregate in ≤ 2s (performance gate).
"""

from __future__ import annotations

import os
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

pytestmark = pytest.mark.asyncio(loop_scope="function")

_TEST_JWT_SECRET = "pytest-secret-32-bytes-padded-xxxxxxxxxx"
_TEST_JWT_ISSUER = "polaris-portal"
os.environ.setdefault("JWT_SECRET", _TEST_JWT_SECRET)
os.environ.setdefault("JWT_ISSUER", _TEST_JWT_ISSUER)

import jwt  # noqa: E402
from polaris_api.main import app  # noqa: E402
from polaris_api.services.admin import get_admin_service_conn  # noqa: E402
from polaris_shared_types.platform_health import (  # noqa: E402
    ApmErrors,
    ServiceConnectivity,
    WebhookSurfaceHealth,
)


def _mint_jwt(*, role: str, sub: str | None = None) -> str:
    now = int(time.time())
    payload: dict[str, Any] = {
        "sub": sub or str(uuid4()),
        "role": role,
        "iss": _TEST_JWT_ISSUER,
        "iat": now,
        "exp": now + 300,
    }
    return jwt.encode(payload, _TEST_JWT_SECRET, algorithm="HS256")


class _FakeTxn:
    async def __aenter__(self) -> _FakeTxn:
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None


class _FakeRecord:
    def __init__(self, data: dict[str, Any]) -> None:
        self._d = data

    def __getitem__(self, key: str) -> Any:
        return self._d.get(key)

    def keys(self) -> list[str]:
        return list(self._d.keys())


class _FakeConn:
    def __init__(self) -> None:
        self.canned_rows: dict[str, list[dict[str, Any]]] = {}
        self.canned_row: dict[str, dict[str, Any] | None] = {}
        self.canned_val: dict[str, Any] = {}
        self.executed: list[tuple[str, tuple[Any, ...]]] = []

    def transaction(self) -> _FakeTxn:
        return _FakeTxn()

    async def fetch(self, sql: str, *args: Any) -> list[_FakeRecord]:
        for key, rows in self.canned_rows.items():
            if key in sql:
                return [_FakeRecord(r) for r in rows]
        return []

    async def fetchrow(self, sql: str, *args: Any) -> _FakeRecord | None:
        for key, row in self.canned_row.items():
            if key in sql:
                return _FakeRecord(row) if row is not None else None
        return None

    async def fetchval(self, sql: str, *args: Any) -> Any:
        for key, val in self.canned_val.items():
            if key in sql:
                return val
        return None

    async def execute(self, sql: str, *args: Any) -> str:
        self.executed.append((sql, args))
        return "OK"


_NOW_ISO = datetime.now(UTC).isoformat()

# Canned DB rows for the health signals
_LLM_MONTH_ROWS: list[dict[str, Any]] = [
    {"model_used": "haiku", "inp": 800, "outp": 300},
    {"model_used": "sonnet", "inp": 200, "outp": 200},
]

# ACME 90% (near), Beta 120% (over), Gamma negligible.
_QUOTA_ROWS: list[dict[str, Any]] = [
    {
        "tenant_id": str(uuid4()),
        "tenant_name": "ACME Sdn Bhd",
        "plan_tier": "pro",
        "consumed": 900,
        "cap": 1000,
    },
    {
        "tenant_id": str(uuid4()),
        "tenant_name": "Beta Corp",
        "plan_tier": "starter",
        "consumed": 1200,
        "cap": 1000,
    },
    {
        "tenant_id": str(uuid4()),
        "tenant_name": "Gamma",
        "plan_tier": "ultra",
        "consumed": 10,
        "cap": 5000,
    },
]

_META_ROWS: list[dict[str, Any]] = [
    {"surface": "whatsapp", "calls_remaining_pct": 82.0, "last_observed_at": _NOW_ISO},
    {"surface": "messenger", "calls_remaining_pct": 91.0, "last_observed_at": _NOW_ISO},
    {"surface": "messenger", "calls_remaining_pct": 95.0, "last_observed_at": _NOW_ISO},
]


_STUB_CONNECTIVITY = ServiceConnectivity(
    healthy=True, last_checked_at=_NOW_ISO, latency_ms=12, error=None
)
_STUB_WEBHOOK = WebhookSurfaceHealth(
    last_received_at=_NOW_ISO, events_1h=5, hmac_fail_1h=0, tenant_not_found_1h=0
)
_STUB_APM = ApmErrors(configured=False, error_count_24h=None, sentry_url=None, error=None)


def _make_fake_conn(
    *,
    queue_pending: Any = 5,
    queue_in_flight: Any = 2,
    queue_failed: Any = 0,
) -> _FakeConn:
    conn = _FakeConn()
    conn.canned_val["SELECT 1"] = 1
    # broadcasts probe (enum literals scheduled/running + failed recipients)
    conn.canned_val["status = 'scheduled'"] = queue_pending
    conn.canned_val["status = 'running'"] = queue_in_flight
    conn.canned_val["broadcast_recipients"] = queue_failed
    conn.canned_val["status = 'errored'"] = 0
    # messaging probe
    conn.canned_val["role = 'user'"] = 12
    conn.canned_val["role = 'assistant'"] = 11
    conn.canned_val["FROM tenant_usage"] = 1500
    conn.canned_val["MAX(created_at)"] = datetime.now(UTC)
    # llm probe (fetchrow 24h totals + fetch month-grouped). Keyed on "AS inp"
    # so it doesn't collide with the email/kg fetchrow queries that also
    # contain "INTERVAL '24 hours'".
    conn.canned_row["AS inp"] = {"inp": 1000, "outp": 500}
    conn.canned_rows["GROUP BY model_used"] = _LLM_MONTH_ROWS
    # quota probe
    conn.canned_rows["tenant_quotas"] = _QUOTA_ROWS
    # meta rate-limits probe
    conn.canned_rows["meta_api_usage"] = _META_ROWS
    # email_drafts probe — single fetchrow keyed on the table name.
    conn.canned_row["FROM email_drafts"] = {
        "pending": 4,
        "accepted_24h": 2,
        "edited_24h": 1,
        "ignored_24h": 3,
        "latest_at": datetime.now(UTC),
    }
    # knowledge_gaps probe — single fetchrow keyed on the table name.
    conn.canned_row["FROM knowledge_gap_misses"] = {
        "no_retrieval": 3,
        "low_retrieval": 0,
        "handoff": 2,
        "latest_at": datetime.now(UTC),
    }
    # active_tenants probe — COUNT(DISTINCT) inserted before the tenants key
    # because the active query embeds a "FROM tenants WHERE status" subquery.
    conn.canned_val["COUNT(DISTINCT tenant_id)"] = 7
    conn.canned_val["FROM tenants WHERE status"] = 15
    # telegram_webhooks probe — counts fetchrow + all-time MAX fetchval.
    conn.canned_row["source = 'telegram'"] = {
        "events_1h": 10,
        "hmac_fail_1h": 0,
        "tenant_not_found_1h": 0,
    }
    conn.canned_val[
        "MAX(received_at) FROM webhook_events WHERE source = 'telegram'"
    ] = datetime.now(UTC)
    # meta_webhooks probe — counts fetchrow (source = ANY) + MAX fetchval.
    conn.canned_row["source = ANY"] = {
        "events_1h": 20,
        "hmac_fail_1h": 0,
        "tenant_not_found_1h": 0,
    }
    conn.canned_val[
        "MAX(received_at) FROM webhook_events WHERE source = ANY"
    ] = datetime.now(UTC)
    # webhook_event_errors probe — grouped breakdown fetch + recent-rows fetch.
    conn.canned_rows["GROUP BY status, error_class"] = [
        {"status": "tenant_not_found", "error_class": "ChannelNotEntitled:whatsapp", "c": 3},
        {"status": "tenant_not_found", "error_class": "NoChannelMatch:chatwoot", "c": 2},
        {"status": "hmac_fail", "error_class": "HMACVerifyFailed", "c": 4},
        {"status": "no_recipient", "error_class": None, "c": 1},
    ]
    # Key on the recent-rows SELECT list (unique to this query) rather than the
    # generic "ORDER BY received_at DESC" so a future recent-rows probe can't
    # silently steal this canned data via substring match.
    conn.canned_rows["SELECT received_at, source, status, error_class"] = [
        {
            "received_at": datetime.now(UTC),
            "source": "chatwoot",
            "status": "tenant_not_found",
            "error_class": "ChannelNotEntitled:whatsapp",
        },
        {
            "received_at": datetime.now(UTC),
            "source": "meta",
            "status": "hmac_fail",
            "error_class": "HMACVerifyFailed",
        },
    ]
    return conn


@pytest_asyncio.fixture(autouse=True)
async def clear_health_cache() -> AsyncIterator[None]:
    """Clear the health endpoint TTL cache before every test.

    The cache is a module-level dict in admin_health; without this, a
    test that warms the cache causes subsequent tests to see the cached
    result rather than their own mocked probes.
    """
    import polaris_api.routers.admin_health as _health_mod

    _health_mod._cache.clear()
    _health_mod._cache_expires_at = 0.0
    yield
    _health_mod._cache.clear()
    _health_mod._cache_expires_at = 0.0


@pytest_asyncio.fixture
async def fake_conn() -> AsyncIterator[_FakeConn]:
    conn = _make_fake_conn()

    async def _override() -> AsyncIterator[_FakeConn]:
        yield conn

    app.dependency_overrides[get_admin_service_conn] = _override
    try:
        yield conn
    finally:
        app.dependency_overrides.pop(get_admin_service_conn, None)


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


_ADMIN_TOKEN = _mint_jwt(role="platform_admin")
_ADMIN_HEADERS = {"Authorization": f"Bearer {_ADMIN_TOKEN}"}


# ---- Auth gate ---------------------------------------------------------------


async def test_missing_jwt_returns_401(client: AsyncClient, fake_conn: _FakeConn) -> None:
    """No bearer token → 401."""
    res = await client.get("/api/admin/health")
    assert res.status_code == 401


async def test_anon_role_denied(client: AsyncClient, fake_conn: _FakeConn) -> None:
    """anon-role JWT → 403."""
    token = _mint_jwt(role="anon")
    res = await client.get(
        "/api/admin/health",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 403


# ---- Happy path --------------------------------------------------------------


async def test_health_happy_path_shape(client: AsyncClient, fake_conn: _FakeConn) -> None:
    """All signals return and the top-level shape is correct."""
    with (
        patch("polaris_api.routers.admin_health.get_redis") as mock_redis_getter,
        patch(
            "polaris_api.routers.admin_health._probe_chatwoot",
            new_callable=AsyncMock,
            return_value=_STUB_CONNECTIVITY,
        ),
        patch(
            "polaris_api.routers.admin_health._probe_ea",
            new_callable=AsyncMock,
            return_value=_STUB_CONNECTIVITY,
        ),
        patch(
            "polaris_api.routers.admin_health._probe_apm",
            new_callable=AsyncMock,
            return_value=_STUB_APM,
        ),
    ):
        mock_redis = AsyncMock()
        mock_redis.ping = AsyncMock(return_value=True)
        mock_redis.info = AsyncMock(return_value={"used_memory": 12 * 1024 * 1024})
        mock_redis_getter.return_value = mock_redis

        res = await client.get("/api/admin/health", headers=_ADMIN_HEADERS)

    assert res.status_code == 200, res.text
    data = res.json()

    # Original top-level keys
    assert "supabase" in data
    assert "redis" in data
    assert "broadcasts" in data
    assert "messaging" in data
    assert "llm" in data
    assert "quota" in data
    assert "failures" in data
    assert "meta_rate_limits" in data
    assert "refreshed_at" in data
    # New tiles
    assert "email_drafts" in data
    assert "knowledge_gaps" in data
    assert "active_tenants" in data
    assert "chatwoot" in data
    assert "ea" in data
    assert "telegram_webhooks" in data
    assert "meta_webhooks" in data
    assert "webhook_event_errors" in data
    assert "apm" in data


async def test_webhook_event_errors_breakdown(
    client: AsyncClient, fake_conn: _FakeConn
) -> None:
    """The webhook_event_errors signal groups by status + error_class and lists recent."""
    with (
        patch("polaris_api.routers.admin_health.get_redis") as mock_redis_getter,
        patch(
            "polaris_api.routers.admin_health._probe_chatwoot",
            new_callable=AsyncMock,
            return_value=_STUB_CONNECTIVITY,
        ),
        patch(
            "polaris_api.routers.admin_health._probe_ea",
            new_callable=AsyncMock,
            return_value=_STUB_CONNECTIVITY,
        ),
        patch(
            "polaris_api.routers.admin_health._probe_apm",
            new_callable=AsyncMock,
            return_value=_STUB_APM,
        ),
    ):
        mock_redis = AsyncMock()
        mock_redis.ping = AsyncMock(return_value=True)
        mock_redis.info = AsyncMock(return_value={"used_memory": 12 * 1024 * 1024})
        mock_redis_getter.return_value = mock_redis

        res = await client.get("/api/admin/health", headers=_ADMIN_HEADERS)

    assert res.status_code == 200, res.text
    block = res.json()["webhook_event_errors"]

    # total = sum of all grouped counts (3+2+4+1)
    assert block["total_errors_24h"] == 10
    # by_status sums error_class rows per status (tenant_not_found 3+2=5, hmac_fail 4, no_recipient 1)
    by_status = {c["key"]: c["count"] for c in block["by_status"]}
    assert by_status == {"tenant_not_found": 5, "hmac_fail": 4, "no_recipient": 1}
    # by_error_class keeps error_class buckets; NULL → '(none)'
    by_ec = {c["key"]: c["count"] for c in block["by_error_class"]}
    assert by_ec["ChannelNotEntitled:whatsapp"] == 3
    assert by_ec["HMACVerifyFailed"] == 4
    assert by_ec["(none)"] == 1
    # recent rows carry source/status/error_class
    assert len(block["recent"]) == 2
    assert block["recent"][0]["source"] == "chatwoot"
    assert block["recent"][0]["error_class"] == "ChannelNotEntitled:whatsapp"
    # happy path → no probe error (error is set only on DB-outage degradation)
    assert block["error"] is None
    # deterministic ordering: sorted by count desc (5, 4, 1) then key asc on ties
    assert [c["key"] for c in block["by_status"]] == [
        "tenant_not_found",
        "hmac_fail",
        "no_recipient",
    ]


async def test_supabase_signal_shape(client: AsyncClient, fake_conn: _FakeConn) -> None:
    """Supabase signal has healthy, db_latency_ms, error."""
    with (
        patch("polaris_api.routers.admin_health.get_redis") as mock_redis_getter,
        patch(
            "polaris_api.routers.admin_health._probe_chatwoot",
            new_callable=AsyncMock,
            return_value=_STUB_CONNECTIVITY,
        ),
        patch(
            "polaris_api.routers.admin_health._probe_ea",
            new_callable=AsyncMock,
            return_value=_STUB_CONNECTIVITY,
        ),
        patch(
            "polaris_api.routers.admin_health._probe_apm",
            new_callable=AsyncMock,
            return_value=_STUB_APM,
        ),
    ):
        mock_redis = AsyncMock()
        mock_redis.ping = AsyncMock(return_value=True)
        mock_redis.info = AsyncMock(return_value={"used_memory": 12 * 1024 * 1024})
        mock_redis_getter.return_value = mock_redis

        res = await client.get("/api/admin/health", headers=_ADMIN_HEADERS)

    data = res.json()
    sb = data["supabase"]
    assert "healthy" in sb
    assert "last_checked_at" in sb
    assert "db_latency_ms" in sb
    assert "error" in sb
    assert sb["healthy"] is True
    assert sb["error"] is None


async def test_redis_signal_shape(client: AsyncClient, fake_conn: _FakeConn) -> None:
    """Redis signal has healthy, used_memory_mb, error."""
    with (
        patch("polaris_api.routers.admin_health.get_redis") as mock_redis_getter,
        patch(
            "polaris_api.routers.admin_health._probe_chatwoot",
            new_callable=AsyncMock,
            return_value=_STUB_CONNECTIVITY,
        ),
        patch(
            "polaris_api.routers.admin_health._probe_ea",
            new_callable=AsyncMock,
            return_value=_STUB_CONNECTIVITY,
        ),
        patch(
            "polaris_api.routers.admin_health._probe_apm",
            new_callable=AsyncMock,
            return_value=_STUB_APM,
        ),
    ):
        mock_redis = AsyncMock()
        mock_redis.ping = AsyncMock(return_value=True)
        mock_redis.info = AsyncMock(return_value={"used_memory": 12 * 1024 * 1024})
        mock_redis_getter.return_value = mock_redis

        res = await client.get("/api/admin/health", headers=_ADMIN_HEADERS)

    data = res.json()
    rd = data["redis"]
    assert "healthy" in rd
    assert "last_checked_at" in rd
    assert "used_memory_mb" in rd
    assert "error" in rd
    assert rd["healthy"] is True
    assert isinstance(rd["used_memory_mb"], int)


async def test_broadcasts_signal_shape(client: AsyncClient, fake_conn: _FakeConn) -> None:
    """Broadcast queue signal has pending, in_flight, failed_last_hour."""
    with (
        patch("polaris_api.routers.admin_health.get_redis") as mock_redis_getter,
        patch(
            "polaris_api.routers.admin_health._probe_chatwoot",
            new_callable=AsyncMock,
            return_value=_STUB_CONNECTIVITY,
        ),
        patch(
            "polaris_api.routers.admin_health._probe_ea",
            new_callable=AsyncMock,
            return_value=_STUB_CONNECTIVITY,
        ),
        patch(
            "polaris_api.routers.admin_health._probe_apm",
            new_callable=AsyncMock,
            return_value=_STUB_APM,
        ),
    ):
        mock_redis = AsyncMock()
        mock_redis.ping = AsyncMock(return_value=True)
        mock_redis.info = AsyncMock(return_value={"used_memory": 1024})
        mock_redis_getter.return_value = mock_redis

        res = await client.get("/api/admin/health", headers=_ADMIN_HEADERS)

    data = res.json()
    bq = data["broadcasts"]
    assert "pending" in bq
    assert "in_flight" in bq
    assert "failed_last_hour" in bq
    assert bq["pending"] >= 0
    assert bq["in_flight"] >= 0
    assert bq["failed_last_hour"] >= 0


async def _get_with_redis(client: AsyncClient) -> dict[str, Any]:
    with (
        patch("polaris_api.routers.admin_health.get_redis") as mock_redis_getter,
        patch(
            "polaris_api.routers.admin_health._probe_chatwoot",
            new_callable=AsyncMock,
            return_value=_STUB_CONNECTIVITY,
        ),
        patch(
            "polaris_api.routers.admin_health._probe_ea",
            new_callable=AsyncMock,
            return_value=_STUB_CONNECTIVITY,
        ),
        patch(
            "polaris_api.routers.admin_health._probe_apm",
            new_callable=AsyncMock,
            return_value=_STUB_APM,
        ),
    ):
        mock_redis = AsyncMock()
        mock_redis.ping = AsyncMock(return_value=True)
        mock_redis.info = AsyncMock(return_value={"used_memory": 1024})
        mock_redis_getter.return_value = mock_redis
        res = await client.get("/api/admin/health", headers=_ADMIN_HEADERS)
    assert res.status_code == 200, res.text
    return res.json()  # type: ignore[no-any-return]


async def test_messaging_signal_shape(client: AsyncClient, fake_conn: _FakeConn) -> None:
    """Messaging volume has inbound/outbound 24h + bot replies MTD."""
    data = await _get_with_redis(client)
    msg = data["messaging"]
    assert msg["inbound_24h"] == 12
    assert msg["outbound_24h"] == 11
    assert msg["bot_replies_mtd"] == 1500


async def test_llm_signal_shape(client: AsyncClient, fake_conn: _FakeConn) -> None:
    """LLM token burn sums 24h totals + MTD per-model, with an est MYR figure."""
    data = await _get_with_redis(client)
    llm = data["llm"]
    assert llm["input_tokens_24h"] == 1000
    assert llm["output_tokens_24h"] == 500
    # MTD = sum of month-grouped rows (haiku 800/300 + sonnet 200/200).
    assert llm["input_tokens_mtd"] == 1000
    assert llm["output_tokens_mtd"] == 500
    assert llm["est_cost_myr_mtd"] is not None
    assert llm["est_cost_myr_mtd"] >= 0


async def test_quota_signal_counts(client: AsyncClient, fake_conn: _FakeConn) -> None:
    """Quota saturation counts near (>=80%) and over (>=100%) tenants."""
    data = await _get_with_redis(client)
    q = data["quota"]
    assert q["near_cap_count"] == 1  # ACME @ 90%
    assert q["over_cap_count"] == 1  # Beta @ 120%
    assert isinstance(q["top"], list)
    # Most-saturated first.
    assert q["top"][0]["usage_pct"] >= q["top"][-1]["usage_pct"] or len(q["top"]) == 1


async def test_failures_signal_shape(client: AsyncClient, fake_conn: _FakeConn) -> None:
    """Recent failures has the two counters + (nullable) APM links."""
    data = await _get_with_redis(client)
    f = data["failures"]
    assert "errored_broadcasts_last_hour" in f
    assert "failed_recipients_last_hour" in f
    assert "grafana_url" in f
    assert "sentry_url" in f


async def test_meta_rate_limits_is_list(client: AsyncClient, fake_conn: _FakeConn) -> None:
    """Meta rate limits is a list with surface + calls_remaining_pct."""
    with (
        patch("polaris_api.routers.admin_health.get_redis") as mock_redis_getter,
        patch(
            "polaris_api.routers.admin_health._probe_chatwoot",
            new_callable=AsyncMock,
            return_value=_STUB_CONNECTIVITY,
        ),
        patch(
            "polaris_api.routers.admin_health._probe_ea",
            new_callable=AsyncMock,
            return_value=_STUB_CONNECTIVITY,
        ),
        patch(
            "polaris_api.routers.admin_health._probe_apm",
            new_callable=AsyncMock,
            return_value=_STUB_APM,
        ),
    ):
        mock_redis = AsyncMock()
        mock_redis.ping = AsyncMock(return_value=True)
        mock_redis.info = AsyncMock(return_value={"used_memory": 1024})
        mock_redis_getter.return_value = mock_redis

        res = await client.get("/api/admin/health", headers=_ADMIN_HEADERS)

    data = res.json()
    mrl = data["meta_rate_limits"]
    assert isinstance(mrl, list)
    for item in mrl:
        assert "surface" in item
        assert "calls_remaining_pct" in item
        assert "last_observed_at" in item


# ---- Partial failure — Supabase degraded ------------------------------------


async def test_degraded_supabase_does_not_crash_other_tiles(
    client: AsyncClient, fake_conn: _FakeConn
) -> None:
    """Supabase probe raises → supabase tile shows error, other 4 tiles still present."""
    with (
        patch(
            "polaris_api.routers.admin_health._probe_supabase",
            new_callable=AsyncMock,
            side_effect=Exception("connection refused"),
        ),
        patch("polaris_api.routers.admin_health.get_redis") as mock_redis_getter,
        patch(
            "polaris_api.routers.admin_health._probe_chatwoot",
            new_callable=AsyncMock,
            return_value=_STUB_CONNECTIVITY,
        ),
        patch(
            "polaris_api.routers.admin_health._probe_ea",
            new_callable=AsyncMock,
            return_value=_STUB_CONNECTIVITY,
        ),
        patch(
            "polaris_api.routers.admin_health._probe_apm",
            new_callable=AsyncMock,
            return_value=_STUB_APM,
        ),
    ):
        mock_redis = AsyncMock()
        mock_redis.ping = AsyncMock(return_value=True)
        mock_redis.info = AsyncMock(return_value={"used_memory": 1024})
        mock_redis_getter.return_value = mock_redis

        res = await client.get("/api/admin/health", headers=_ADMIN_HEADERS)

    assert res.status_code == 200, res.text
    data = res.json()

    # Supabase tile shows error
    sb = data["supabase"]
    assert sb["healthy"] is False
    assert sb["error"] is not None
    assert "connection refused" in sb["error"]

    # Other tiles still present and well-formed
    assert "redis" in data
    assert "broadcasts" in data
    assert "messaging" in data
    assert "quota" in data
    assert "meta_rate_limits" in data


async def test_degraded_redis_does_not_crash_other_tiles(
    client: AsyncClient, fake_conn: _FakeConn
) -> None:
    """Redis probe raises → redis tile shows error, other tiles still present."""
    with (
        patch("polaris_api.routers.admin_health.get_redis") as mock_redis_getter,
        patch(
            "polaris_api.routers.admin_health._probe_chatwoot",
            new_callable=AsyncMock,
            return_value=_STUB_CONNECTIVITY,
        ),
        patch(
            "polaris_api.routers.admin_health._probe_ea",
            new_callable=AsyncMock,
            return_value=_STUB_CONNECTIVITY,
        ),
        patch(
            "polaris_api.routers.admin_health._probe_apm",
            new_callable=AsyncMock,
            return_value=_STUB_APM,
        ),
    ):
        mock_redis_getter.side_effect = RuntimeError("redis not initialised")

        res = await client.get("/api/admin/health", headers=_ADMIN_HEADERS)

    assert res.status_code == 200, res.text
    data = res.json()

    rd = data["redis"]
    assert rd["healthy"] is False
    assert rd["error"] is not None

    # Other tiles still present
    assert "supabase" in data
    assert "broadcasts" in data
    assert "messaging" in data
    assert "failures" in data
    assert "meta_rate_limits" in data


# ---- Performance gate --------------------------------------------------------


async def test_health_aggregates_in_under_2s(
    client: AsyncClient, fake_conn: _FakeConn
) -> None:
    """All signals must aggregate in ≤ 2s (uses asyncio.ensure_future internally)."""
    with (
        patch("polaris_api.routers.admin_health.get_redis") as mock_redis_getter,
        patch(
            "polaris_api.routers.admin_health._probe_chatwoot",
            new_callable=AsyncMock,
            return_value=_STUB_CONNECTIVITY,
        ),
        patch(
            "polaris_api.routers.admin_health._probe_ea",
            new_callable=AsyncMock,
            return_value=_STUB_CONNECTIVITY,
        ),
        patch(
            "polaris_api.routers.admin_health._probe_apm",
            new_callable=AsyncMock,
            return_value=_STUB_APM,
        ),
    ):
        mock_redis = AsyncMock()
        mock_redis.ping = AsyncMock(return_value=True)
        mock_redis.info = AsyncMock(return_value={"used_memory": 1024})
        mock_redis_getter.return_value = mock_redis

        start = time.monotonic()
        res = await client.get("/api/admin/health", headers=_ADMIN_HEADERS)
        elapsed = time.monotonic() - start

    assert res.status_code == 200
    assert elapsed < 2.0, f"Health endpoint took {elapsed:.2f}s — must be < 2s"


# ---- New tile assertions -------------------------------------------------------


async def test_active_tenants_values(client: AsyncClient, fake_conn: _FakeConn) -> None:
    """active_tenants tile returns canned active_24h and total_provisioned values."""
    data = await _get_with_redis(client)
    at = data["active_tenants"]
    assert at["active_24h"] == 7
    assert at["total_provisioned"] == 15


async def test_knowledge_gaps_values(client: AsyncClient, fake_conn: _FakeConn) -> None:
    """knowledge_gaps tile sums signal_type rows and computes total_24h."""
    data = await _get_with_redis(client)
    kg = data["knowledge_gaps"]
    assert kg["no_retrieval_24h"] == 3
    assert kg["low_retrieval_24h"] == 0
    assert kg["handoff_24h"] == 2
    assert kg["total_24h"] == 5


async def test_email_drafts_shape(client: AsyncClient, fake_conn: _FakeConn) -> None:
    """email_drafts tile has pending, accepted_24h, edited_24h, ignored_24h, latest_draft_at."""
    data = await _get_with_redis(client)
    ed = data["email_drafts"]
    assert ed["pending"] == 4
    assert ed["accepted_24h"] == 2
    assert ed["edited_24h"] == 1
    assert ed["ignored_24h"] == 3
    assert ed["latest_draft_at"] is not None


async def test_chatwoot_and_ea_shape(client: AsyncClient, fake_conn: _FakeConn) -> None:
    """chatwoot and ea tiles have the ServiceConnectivity shape from stub."""
    data = await _get_with_redis(client)
    for key in ("chatwoot", "ea"):
        tile = data[key]
        assert "healthy" in tile
        assert "last_checked_at" in tile
        assert "latency_ms" in tile
        assert "error" in tile
        assert tile["healthy"] is True


async def test_webhook_tiles_shape(client: AsyncClient, fake_conn: _FakeConn) -> None:
    """telegram_webhooks and meta_webhooks tiles have correct shape."""
    data = await _get_with_redis(client)
    for key in ("telegram_webhooks", "meta_webhooks"):
        tile = data[key]
        assert "last_received_at" in tile
        assert "events_1h" in tile
        assert "hmac_fail_1h" in tile
        assert "tenant_not_found_1h" in tile


async def test_apm_tile_unconfigured(client: AsyncClient, fake_conn: _FakeConn) -> None:
    """apm tile returns configured=False when sentry_auth_token is empty (stub)."""
    data = await _get_with_redis(client)
    apm = data["apm"]
    assert "configured" in apm
    assert "error_count_24h" in apm
    assert "sentry_url" in apm
    assert "error" in apm
    assert apm["configured"] is False
