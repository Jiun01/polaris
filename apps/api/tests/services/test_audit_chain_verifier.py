"""Audit chain verifier service -- Phase 9 PR-5 tests.

Unit-level tests (no DB required):
  - run_verification with an intact chain (mocked verify_chain returns True).
  - run_verification with a broken chain (mocked returns False with id).
  - run_verification writes a result row to audit_chain_verifications.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

pytestmark = pytest.mark.asyncio(loop_scope="function")


class _MockPool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _MockAcquire(self._conn)


class _MockAcquire:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *args):
        pass


class _MockConn:
    def __init__(self, *, tenants=None, rows_scanned=10):
        self.tenants = tenants or []
        self._rows_scanned = rows_scanned
        self.executed = []

    async def fetch(self, sql, *args):
        if "DISTINCT tenant_id" in sql:
            return [MagicMock(**{"__getitem__": lambda s, k: uuid4()}) for _ in self.tenants]
        return []

    async def fetchrow(self, sql, *args):
        # Return a minimal row for the broken-row detail lookup.
        return {"created_at": None, "prev_hash": None, "row_hash": None}

    async def fetchval(self, sql, *args):
        return self._rows_scanned

    async def execute(self, sql, *args):
        self.executed.append(sql)


@pytest.mark.asyncio
async def test_run_verification_intact_chain():
    from polaris_api.services.audit_chain_verifier import run_verification

    conn = _MockConn(tenants=[uuid4()], rows_scanned=42)
    pool = _MockPool(conn)

    with patch(
        "polaris_api.services.audit_chain_verifier.verify_chain",
        new=AsyncMock(return_value=(True, None)),
    ):
        result = await run_verification(
            triggered_by="on_demand", triggered_by_admin_id=None, pool=pool
        )

    assert result["status"] == "intact"
    assert result["rows_scanned"] >= 0
    assert result["broken_at_id"] is None
    assert result["triggered_by"] == "on_demand"


@pytest.mark.asyncio
async def test_run_verification_broken_chain():
    from polaris_api.services.audit_chain_verifier import run_verification

    broken_id = uuid4()
    conn = _MockConn(tenants=[uuid4()], rows_scanned=10)
    pool = _MockPool(conn)

    with patch(
        "polaris_api.services.audit_chain_verifier.verify_chain",
        new=AsyncMock(return_value=(False, broken_id)),
    ):
        result = await run_verification(
            triggered_by="scheduled", triggered_by_admin_id=None, pool=pool
        )

    assert result["status"] == "broken"
    assert result["broken_at_id"] == broken_id
    assert result["triggered_by"] == "scheduled"


@pytest.mark.asyncio
async def test_run_verification_writes_row_to_db():
    from polaris_api.services.audit_chain_verifier import run_verification

    conn = _MockConn(tenants=[], rows_scanned=0)
    pool = _MockPool(conn)

    with patch(
        "polaris_api.services.audit_chain_verifier.verify_chain",
        new=AsyncMock(return_value=(True, None)),
    ):
        await run_verification(
            triggered_by="on_demand", triggered_by_admin_id=uuid4(), pool=pool
        )

    assert any("audit_chain_verifications" in sql for sql in conn.executed), (
        "Expected INSERT to audit_chain_verifications, got: " + str(conn.executed)
    )
