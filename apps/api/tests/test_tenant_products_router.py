"""Phase 15 PR-2 — tenant_products router unit tests.

Full CRUD + embed-on-write + RLS isolation coverage is DB-gated (needs
``POLARIS_DATABASE_URL`` + an applied migration 089/090) and lands with the
integration fixtures. This file gives in-process coverage of the pure-Python
helpers + request-body contracts so collection works in CI without a DB —
mirrors ``test_tenant_dashboard.py``.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from polaris_api.routers.tenant_products import (
    ProductImageReorder,
    _embedding_input,
    _image_row_to_dto,
    _product_row_to_dto,
)
from polaris_api.services.embed import to_pgvector_literal
from pydantic import ValidationError


class _FakeRow:
    """Minimal asyncpg.Record stand-in for unit tests."""

    def __init__(self, **kw: object) -> None:
        self._d = dict(kw)

    def __getitem__(self, key: str) -> object:
        return self._d[key]


# ---------------------------------------------------------------- vector literal


def test_to_pgvector_literal_formats_list() -> None:
    assert to_pgvector_literal([0.1, 0.2, 0.3]) == "[0.1,0.2,0.3]"


def test_to_pgvector_literal_empty() -> None:
    assert to_pgvector_literal([]) == "[]"


def test_to_pgvector_literal_coerces_non_finite() -> None:
    """NaN / ±Inf from a misbehaving provider must become 0.0 so the
    ``::vector`` cast never rejects the literal."""
    out = to_pgvector_literal([float("nan"), float("inf"), float("-inf"), 1.5])
    assert out == "[0.0,0.0,0.0,1.5]"
    # And it parses back to all-finite floats.
    parsed = [float(x) for x in out.strip("[]").split(",")]
    assert all(math.isfinite(v) for v in parsed)


# ---------------------------------------------------------------- embed input


def test_embedding_input_combines_name_and_description() -> None:
    assert _embedding_input("Blue Tumbler", "500ml steel") == "Blue Tumbler\n500ml steel"


# ---------------------------------------------------------------- row -> DTO


def test_image_row_to_dto_maps_fields_and_nulls_url() -> None:
    pid = uuid4()
    iid = uuid4()
    now = datetime.now(UTC)
    row = _FakeRow(
        id=iid, product_id=pid, storage_path="t/abc.jpg", sort_order=2,
        width=800, height=600, bytes=12345, content_type="image/jpeg",
        created_at=now,
    )
    dto = _image_row_to_dto(row)  # type: ignore[arg-type]
    assert dto.id == iid
    assert dto.product_id == pid
    assert dto.storage_path == "t/abc.jpg"
    assert dto.sort_order == 2
    assert dto.bytes == 12345
    assert dto.content_type == "image/jpeg"
    # url is minted at read time (PR-3/PR-6), never stored.
    assert dto.url is None


def test_product_row_to_dto_includes_images() -> None:
    pid = uuid4()
    tid = uuid4()
    now = datetime.now(UTC)
    img = _image_row_to_dto(
        _FakeRow(
            id=uuid4(), product_id=pid, storage_path="t/x.jpg", sort_order=0,
            width=None, height=None, bytes=None, content_type=None, created_at=now,
        )  # type: ignore[arg-type]
    )
    row = _FakeRow(
        id=pid, tenant_id=tid, tenant_branch_id=None, sku="SKU-1",
        name="Blue Tumbler", description="500ml steel", price_text="RM 49.90",
        is_active=True, created_at=now, updated_at=now,
    )
    dto = _product_row_to_dto(row, [img])  # type: ignore[arg-type]
    assert dto.id == pid
    assert dto.sku == "SKU-1"
    assert dto.price_text == "RM 49.90"
    assert len(dto.images) == 1
    assert dto.images[0].storage_path == "t/x.jpg"


# ---------------------------------------------------------------- request bodies


def test_reorder_body_requires_nonempty() -> None:
    with pytest.raises(ValidationError):
        ProductImageReorder(image_ids=[])


def test_reorder_body_accepts_uuid_list() -> None:
    ids = [uuid4(), uuid4()]
    body = ProductImageReorder(image_ids=ids)
    assert body.image_ids == ids
