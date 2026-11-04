"""Unit tests for services/knowledge_ingestion.py — capacity-gate math.

Q-PHASE3-17 sanity: ``estimate_current_usage_mb`` does the live SUM
correctly; ``get_effective_cap_mb`` honours the LEFT JOIN COALESCE
between ``tenant_billing.kb_mb_cap_override`` and
``tenant_quotas.knowledge_chunks_total_mb``.

Pure unit — no router, no JWT, just the service functions against an
AsyncMock asyncpg.Connection. Real-DB / RLS-validated coverage lands
in PR-2's end-to-end test.
"""

from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import uuid4

import asyncpg
import pytest
from polaris_api.services.knowledge_ingestion import (
    estimate_current_usage_mb,
    estimate_request_size_mb,
    get_effective_cap_mb,
)
from polaris_shared_types import IngestionRequestSubmit


@pytest.fixture
def mock_conn() -> AsyncMock:
    return AsyncMock(spec=asyncpg.Connection)


async def test_estimate_usage_returns_zero_for_empty_chunks(mock_conn: AsyncMock) -> None:
    mock_conn.fetchrow = AsyncMock(return_value={"used_mb": 0})
    result = await estimate_current_usage_mb(mock_conn, uuid4())
    assert result == 0


async def test_estimate_usage_returns_sum_floored_to_mb(mock_conn: AsyncMock) -> None:
    # 3.5 MB worth of content — SQL floor-divides at 1048576, so the MB
    # answer is 3 (the live SQL does the floor in Postgres, not Python).
    mock_conn.fetchrow = AsyncMock(return_value={"used_mb": 3})
    result = await estimate_current_usage_mb(mock_conn, uuid4())
    assert result == 3


async def test_effective_cap_uses_override_when_present(mock_conn: AsyncMock) -> None:
    """ADR-032 Amendment: kb_mb_cap_override wins via COALESCE."""
    mock_conn.fetchrow = AsyncMock(return_value={"effective_cap_mb": 10_000})
    result = await get_effective_cap_mb(mock_conn, uuid4())
    assert result == 10_000


async def test_effective_cap_falls_back_to_tier_default(mock_conn: AsyncMock) -> None:
    """No override → tenant_quotas.knowledge_chunks_total_mb."""
    mock_conn.fetchrow = AsyncMock(return_value={"effective_cap_mb": 100})
    result = await get_effective_cap_mb(mock_conn, uuid4())
    assert result == 100


async def test_effective_cap_missing_quota_row_raises(mock_conn: AsyncMock) -> None:
    """Provisioning bug: tenant has no quotas row. Surfaces as 500."""
    from fastapi import HTTPException
    mock_conn.fetchrow = AsyncMock(return_value=None)
    with pytest.raises(HTTPException) as excinfo:
        await get_effective_cap_mb(mock_conn, uuid4())
    assert excinfo.value.status_code == 500


# ---------------------------------------------------------------------------
# estimate_request_size_mb — pure function, no I/O
# ---------------------------------------------------------------------------


def test_estimate_size_url_returns_fixed_heuristic() -> None:
    body = IngestionRequestSubmit(
        source_type="url", source_url="https://example.com"  # type: ignore[arg-type]
    )
    assert estimate_request_size_mb(body) == 5


def test_estimate_size_file_scales_with_path_count() -> None:
    body = IngestionRequestSubmit(
        source_type="file", storage_paths=["a", "b", "c"]
    )
    assert estimate_request_size_mb(body) == 30  # 3 * 10 MB


def test_estimate_size_text_floors_to_minimum_1mb() -> None:
    body = IngestionRequestSubmit(source_type="text", source_text="hello world")
    assert estimate_request_size_mb(body) == 1


def test_estimate_size_text_full_payload_returns_zero_floor_to_1() -> None:
    # 100 KiB is well under 1 MiB → floor division yields 0 → clamped to 1.
    body = IngestionRequestSubmit(source_type="text", source_text="x" * 100_000)
    assert estimate_request_size_mb(body) == 1
