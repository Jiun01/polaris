"""Phase 15 PR-4 — product search service unit tests.

The search functions are thin wrappers over the migration-090 RPCs; full
ranking behaviour is DB-gated integration. These tests use a fake connection
to pin the row→``ProductHit`` mapping, the vector-only ``rrf_score`` synthesis,
and the image-grouping contract without a live DB.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from polaris_api.services.products.search import (
    fetch_images_for,
    search_products_hybrid,
    search_products_vector_only,
)


class _FakeRow:
    def __init__(self, **kw: object) -> None:
        self._d = dict(kw)

    def __getitem__(self, key: str) -> object:
        return self._d[key]


class _FakeConn:
    """Records the last SQL and returns canned rows from ``fetch``."""

    def __init__(self, rows: list[_FakeRow]) -> None:
        self._rows = rows
        self.last_sql: str | None = None

    async def fetch(self, sql: str, *args: object) -> list[_FakeRow]:
        self.last_sql = sql
        return self._rows


@pytest.mark.asyncio
async def test_hybrid_maps_rows_to_product_hits() -> None:
    pid = uuid4()
    conn = _FakeConn(
        [
            _FakeRow(
                id=pid, name="Blue Tumbler", description="500ml steel",
                price_text="RM 49", rrf_score=0.8, vector_rank=1, bm25_rank=2,
                vector_distance=None,
            )
        ]
    )
    hits = await search_products_hybrid(conn, uuid4(), "tumbler", [0.0] * 1024)  # type: ignore[arg-type]
    assert len(hits) == 1
    assert hits[0].id == pid
    assert hits[0].rrf_score == 0.8
    assert hits[0].vector_rank == 1
    assert hits[0].distance is None


@pytest.mark.asyncio
async def test_vector_only_synthesises_rrf_from_distance() -> None:
    conn = _FakeConn(
        [
            _FakeRow(
                id=uuid4(), name="X", description="d", price_text=None,
                distance=0.25,
            )
        ]
    )
    hits = await search_products_vector_only(conn, uuid4(), [0.0] * 1024)  # type: ignore[arg-type]
    assert hits[0].rrf_score == pytest.approx(0.75)
    assert hits[0].distance == 0.25
    assert hits[0].vector_rank is None


@pytest.mark.asyncio
async def test_fetch_images_groups_by_product_in_order() -> None:
    p1, p2 = uuid4(), uuid4()
    conn = _FakeConn(
        [
            _FakeRow(product_id=p1, storage_path="a.jpg"),
            _FakeRow(product_id=p1, storage_path="b.jpg"),
            _FakeRow(product_id=p2, storage_path="c.jpg"),
        ]
    )
    out = await fetch_images_for(conn, [p1, p2])  # type: ignore[arg-type]
    assert out[p1] == ["a.jpg", "b.jpg"]
    assert out[p2] == ["c.jpg"]


@pytest.mark.asyncio
async def test_fetch_images_empty_returns_empty() -> None:
    out = await fetch_images_for(_FakeConn([]), [])  # type: ignore[arg-type]
    assert out == {}
