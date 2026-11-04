"""WS1 — polaris-internal demo gateway protection + recovery.

Covers the guards added so the fixed-UUID gateway
(``POLARIS_INTERNAL_TENANT_ID``) can never be deleted via the admin lifecycle
routes, the internal E2E teardown endpoint, or the nightly retention purge, plus
the idempotent recovery helper ``ensure_polaris_internal_gateway``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from fastapi import HTTPException
from polaris_api.e2e import router as e2e_router
from polaris_api.routers import admin_tenants
from polaris_api.scheduled import retention_sweep
from polaris_api.services import demo_sessions
from polaris_api.services.demo_sessions import POLARIS_INTERNAL_TENANT_ID

# ── admin_tenants._assert_not_gateway ──────────────────────────────────────────


def test_assert_not_gateway_blocks_gateway() -> None:
    with pytest.raises(HTTPException) as exc:
        admin_tenants._assert_not_gateway(POLARIS_INTERNAL_TENANT_ID)
    assert exc.value.status_code == 403
    assert exc.value.detail == "gateway_protected"


def test_assert_not_gateway_allows_other_tenants() -> None:
    # A normal tenant id must pass through without raising.
    admin_tenants._assert_not_gateway(uuid4())


# ── e2e teardown endpoint guard ────────────────────────────────────────────────


class _ExplodingConn:
    """Any DB access proves the guard did NOT short-circuit."""

    async def execute(self, *_a: Any, **_k: Any) -> str:
        raise AssertionError("gateway guard did not short-circuit before DB access")

    async def fetch(self, *_a: Any, **_k: Any) -> list[Any]:
        raise AssertionError("gateway guard did not short-circuit before DB access")

    def transaction(self) -> Any:
        raise AssertionError("gateway guard did not short-circuit before DB access")


@pytest.mark.asyncio(loop_scope="function")
async def test_e2e_teardown_blocks_gateway() -> None:
    with pytest.raises(HTTPException) as exc:
        await e2e_router.teardown_tenant(
            tenant_id=POLARIS_INTERNAL_TENANT_ID,
            conn=_ExplodingConn(),
            _auth=None,
        )
    assert exc.value.status_code == 403


# ── retention_sweep._maybe_purge guard ─────────────────────────────────────────


@pytest.mark.asyncio(loop_scope="function")
async def test_retention_purge_skips_gateway() -> None:
    # status_updated_at is well past the grace window; without the guard this
    # would proceed to the cascade DELETEs. The exploding conn proves it does not.
    await retention_sweep._maybe_purge(
        _ExplodingConn(),
        POLARIS_INTERNAL_TENANT_ID,
        datetime(2020, 1, 1, tzinfo=UTC),
        datetime(2026, 1, 1, tzinfo=UTC),
    )


# ── ensure_polaris_internal_gateway recovery ───────────────────────────────────


class _RecoveryConn:
    def __init__(self, *, persona_restored: bool) -> None:
        self.calls: list[str] = []
        self._persona_restored = persona_restored

    async def execute(self, sql: str, *_args: Any) -> str:
        self.calls.append(sql)
        return "INSERT 0 1"

    async def fetchval(self, sql: str, *_args: Any) -> Any:
        # The persona restore is a `UPDATE ... RETURNING id` — returns the id when
        # it actually set the (empty) persona, None when it was left untouched.
        self.calls.append(sql)
        return uuid4() if self._persona_restored else None


@pytest.mark.asyncio(loop_scope="function")
async def test_ensure_gateway_restores_empty_persona() -> None:
    conn = _RecoveryConn(persona_restored=True)
    result = await demo_sessions.ensure_polaris_internal_gateway(conn)
    assert result == {"persona_restored": True}
    assert any(s.lstrip().startswith("INSERT INTO tenants") for s in conn.calls)
    assert any("UPDATE tenants" in s for s in conn.calls)


@pytest.mark.asyncio(loop_scope="function")
async def test_ensure_gateway_preserves_existing_persona() -> None:
    conn = _RecoveryConn(persona_restored=False)
    result = await demo_sessions.ensure_polaris_internal_gateway(conn)
    assert result == {"persona_restored": False}
