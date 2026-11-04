"""Tenant product catalog DTOs — Pydantic SSOT (Phase 15 PR-1).

Four surfaces, all ``ConfigDict(extra='forbid')`` so a future column on
``tenant_products`` / ``tenant_product_images`` fails the contract loudly
instead of silently dropping from the portal payload:

  * ``ProductImage`` — one row in `tenant_product_images` (migration 089).
    ``url`` is NOT a stored column — the API populates it with a short-TTL
    signed read URL minted at response time (None on write-side bodies).
  * ``TenantProduct`` — one row in `tenant_products`, with its ordered
    image gallery. The ``embedding`` / ``content_tsv`` columns are internal
    (RAG only) and deliberately absent from the DTO.
  * ``TenantProductUpsert`` — body shape for create + update. Universal
    across tiers per ADR-061 (mirrors ADR-042 promotions posture).
  * ``ProductHit`` — row shape returned by the search RPCs
    (``match_products_hybrid`` / ``match_products``, migration 090),
    mirroring ``KnowledgeChunkHit``.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

_FORBID = ConfigDict(extra="forbid")


class ProductImage(BaseModel):
    """Row shape for `tenant_product_images`."""

    model_config = _FORBID

    id: UUID
    product_id: UUID
    storage_path: str = Field(min_length=1, max_length=512)
    sort_order: int = Field(ge=0)
    width: int | None = Field(default=None, ge=0)
    height: int | None = Field(default=None, ge=0)
    bytes: int | None = Field(default=None, ge=0)
    content_type: str | None = Field(default=None, min_length=1, max_length=100)
    created_at: datetime
    # Populated by the API at read time with a short-TTL signed read URL
    # from the private `polaris-product-images` bucket. Never stored.
    url: str | None = None


class TenantProduct(BaseModel):
    """Row shape for `tenant_products` plus its ordered image gallery."""

    model_config = _FORBID

    id: UUID
    tenant_id: UUID
    tenant_branch_id: UUID | None
    sku: str | None = Field(default=None, min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=4000)
    price_text: str | None = Field(default=None, min_length=1, max_length=100)
    is_active: bool
    images: list[ProductImage] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class TenantProductUpsert(BaseModel):
    """Body shape for create + update.

    ``tenant_branch_id`` may be explicitly set to None to clear the branch
    scope on update. ``is_active`` defaults to True so a freshly created
    product is immediately eligible for the bot's lookup; pausing requires
    an explicit toggle off. Images are managed via the dedicated
    attach/detach/reorder endpoints (PR-2), not this body.
    """

    model_config = _FORBID

    name: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=4000)
    sku: str | None = Field(default=None, min_length=1, max_length=64)
    price_text: str | None = Field(default=None, min_length=1, max_length=100)
    is_active: bool = True
    tenant_branch_id: UUID | None = None


class ProductHit(BaseModel):
    """One row from ``match_products`` / ``match_products_hybrid``.

    Per-field semantics mirror ``KnowledgeChunkHit``:
      * ``rrf_score`` — primary sort key, higher = better. Always populated.
      * ``vector_rank`` / ``bm25_rank`` — 1-based ranks within each leg's
        candidate pool; ``None`` when the product only matched the other
        leg (FULL OUTER JOIN in the hybrid RPC).
      * ``distance`` — populated by the vector-only fallback; ``None`` from
        the hybrid RPC.
    """

    model_config = _FORBID

    id: UUID
    name: str
    description: str
    price_text: str | None = None
    rrf_score: float
    vector_rank: int | None = Field(default=None, ge=1)
    bm25_rank: int | None = Field(default=None, ge=1)
    distance: float | None = Field(default=None, ge=0.0, le=2.0)
