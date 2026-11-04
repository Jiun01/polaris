"""cost_ceiling conn-scoping split — audit Batch B (B7-c).

Covers the fetch/apply phase split (fetch_tenant_ceiling_status is HTTP-only
and conn-free; apply_tenant_ceiling_breach is DB-only and HTTP-free) and
check_platform_daily_spend's own short-lived tenant-list acquire.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from polaris_api.services import cost_ceiling

pytestmark = pytest.mark.asyncio(loop_scope="function")


def _cost_result(total_myr: str, *, data_source: str = "mimir") -> Any:
    """Fake CostResult — only the two attributes cost_ceiling.py actually
    reads (``.data_source`` and ``.cost.total_cost_myr``)."""
    result = MagicMock()
    result.data_source = data_source
    result.cost.total_cost_myr = Decimal(total_myr)
    return result


async def test_fetch_tenant_ceiling_status_never_touches_a_conn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """fetch_tenant_ceiling_status takes no `conn` param at all — the whole
    point of the B7-c split is a scheduled job can call this without holding
    a service-pool slot."""
    monkeypatch.setattr(
        cost_ceiling, "fetch_tenant_cost_monthly",
        AsyncMock(return_value=_cost_result("150.00")),
    )

    check = await cost_ceiling.fetch_tenant_ceiling_status(
        tenant_id=uuid4(),
        tenant_name="Acme",
        ceiling_myr=Decimal("100.00"),
        yyyymm="202607",
        mimir_url="http://mimir",
        mimir_token="tok",
    )

    assert check.breach == "hard"
    assert check.cost_myr == Decimal("150.00")


async def test_fetch_tenant_ceiling_status_warn_band(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        cost_ceiling, "fetch_tenant_cost_monthly",
        AsyncMock(return_value=_cost_result("80.00")),
    )

    check = await cost_ceiling.fetch_tenant_ceiling_status(
        tenant_id=uuid4(),
        tenant_name="Acme",
        ceiling_myr=Decimal("100.00"),
        yyyymm="202607",
        mimir_url="http://mimir",
        mimir_token="tok",
    )

    assert check.breach == "warn"


async def test_fetch_tenant_ceiling_status_healthy_band(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        cost_ceiling, "fetch_tenant_cost_monthly",
        AsyncMock(return_value=_cost_result("10.00")),
    )

    check = await cost_ceiling.fetch_tenant_ceiling_status(
        tenant_id=uuid4(),
        tenant_name="Acme",
        ceiling_myr=Decimal("100.00"),
        yyyymm="202607",
        mimir_url="http://mimir",
        mimir_token="tok",
    )

    assert check.breach is None


async def test_fetch_tenant_ceiling_status_unavailable_yields_no_breach(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        cost_ceiling, "fetch_tenant_cost_monthly",
        AsyncMock(return_value=_cost_result("0", data_source="unavailable")),
    )

    check = await cost_ceiling.fetch_tenant_ceiling_status(
        tenant_id=uuid4(),
        tenant_name="Acme",
        ceiling_myr=Decimal("100.00"),
        yyyymm="202607",
        mimir_url="http://mimir",
        mimir_token="tok",
    )

    assert check.breach is None
    assert check.cost_myr is None


async def test_apply_tenant_ceiling_breach_hard_writes_conn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hard breach issues the cohere-disable UPDATE + an audit row on the
    conn passed to apply_tenant_ceiling_breach — no HTTP involved here."""
    monkeypatch.setattr(cost_ceiling, "_emit_sentry_fatal", lambda *a, **k: None)
    audit_calls: list[dict[str, Any]] = []

    async def _fake_audit(_conn: Any, **kwargs: Any) -> None:
        audit_calls.append(kwargs)

    monkeypatch.setattr(cost_ceiling, "audit_record", _fake_audit)
    # Reset the in-memory alert dedup so this test is order-independent.
    cost_ceiling._LAST_EMITTED.clear()  # noqa: SLF001

    conn = AsyncMock()
    check = cost_ceiling._TenantCeilingCheck(  # noqa: SLF001
        tenant_id=uuid4(), tenant_name="Acme", ceiling_myr=Decimal("100.00"),
        cost_myr=Decimal("150.00"), pct=1.5, breach="hard",
    )

    await cost_ceiling.apply_tenant_ceiling_breach(conn, check, settings=object())

    conn.execute.assert_awaited_once()
    assert len(audit_calls) == 1
    assert audit_calls[0]["action"] == "cost_ceiling.hard_breach"


async def test_apply_tenant_ceiling_breach_healthy_is_a_noop() -> None:
    """breach=None (healthy/unavailable) → no conn writes at all."""
    conn = AsyncMock()
    check = cost_ceiling._TenantCeilingCheck(  # noqa: SLF001
        tenant_id=uuid4(), tenant_name="Acme", ceiling_myr=Decimal("100.00"),
        cost_myr=None, pct=0.0, breach=None,
    )

    await cost_ceiling.apply_tenant_ceiling_breach(conn, check, settings=object())

    conn.execute.assert_not_called()


async def test_check_platform_daily_spend_releases_conn_before_http_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """check_platform_daily_spend now takes a POOL, acquiring a conn only for
    the short tenant-list read — the per-tenant Mimir loop runs conn-free."""

    class _Acquire:
        def __init__(self, conn: Any) -> None:
            self._conn = conn

        async def __aenter__(self) -> Any:
            return self._conn

        async def __aexit__(self, *exc: object) -> None:
            return None

    conn = AsyncMock()
    conn.fetch = AsyncMock(
        return_value=[{"id": uuid4(), "display_name": "Acme"}]
    )

    pool = AsyncMock()
    acquire_count = 0

    def _acquire() -> _Acquire:
        nonlocal acquire_count
        acquire_count += 1
        return _Acquire(conn)

    pool.acquire = _acquire

    monkeypatch.setattr(
        cost_ceiling, "fetch_tenant_cost_monthly",
        AsyncMock(return_value=_cost_result("10.00")),
    )

    await cost_ceiling.check_platform_daily_spend(
        pool, threshold_myr=1000.0, mimir_url="http://mimir", mimir_token="tok",
    )

    # Exactly ONE acquire — for the tenant-list read only.
    assert acquire_count == 1
