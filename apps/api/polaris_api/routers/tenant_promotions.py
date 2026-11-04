"""Tenant-facing promotion CRUD — Phase 8 PR-9.

Mounted at ``/api/tenant/promotions`` per the standard prefix layering.

Five endpoints (full REST CRUD):

  * ``GET    /``       — list active + scheduled promotions, newest first.
  * ``GET    /{id}``   — single promotion detail.
  * ``POST   /``       — create new promotion.
  * ``PATCH  /{id}``   — partial update (any subset of fields).
  * ``DELETE /{id}``   — hard delete (no soft-delete — operators
                         consistently asked for hard removal in beta).

Universal across tiers per ADR-042; Starter / Pro / Ultra all see and
edit promotions identically. Optional ``tenant_branch_id`` lets a tenant
scope a promo to one location; NULL = applies tenant-wide.

RLS on ``tenant_promotions`` (migration 053) scopes everything to the
calling tenant; the router does NOT re-add a redundant ``tenant_id``
filter so policy regressions surface in tests.

Error envelope codes:
  * ``PROMOTION_NOT_FOUND``   — id missing or RLS-stripped.
  * ``PROMOTION_INVALID_RANGE`` — valid_until <= valid_from (mirrors
    the SQL CHECK + Pydantic model_validator).
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, status
from polaris_shared_types import TenantPromotion, TenantPromotionUpsert
from pydantic import BaseModel, ConfigDict, Field, model_validator

from polaris_api.deps.tenant import (
    TenantContext,
    get_request_conn,
    get_tenant_context,
)

logger = logging.getLogger(__name__)


class TenantPromotionPatch(BaseModel):
    """Partial-update body for ``PATCH /{id}`` (QA finding L8).

    Every field is optional; only fields actually present in the request are
    applied (``model_dump(exclude_unset=True)``). Omitted fields keep their
    current value; an explicit ``null`` on a nullable field (``terms`` /
    ``tenant_branch_id``) clears it. Field constraints mirror
    ``TenantPromotionUpsert``; the ``valid_until > valid_from`` rule is checked
    in the handler against the MERGED result (either date may be omitted).
    """

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, min_length=1, max_length=2000)
    discount_text: str | None = Field(default=None, min_length=1, max_length=100)
    terms: str | None = Field(default=None, max_length=4000)
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    is_active: bool | None = None

    @model_validator(mode="after")
    def _reject_null_on_required(self) -> TenantPromotionPatch:
        """Reject an EXPLICIT ``null`` on a NOT-NULL column (QA finding L8).

        The optional-typing that enables partial PATCH means Pydantic's
        ``min_length`` no longer rejects ``None`` for ``title`` / ``description``
        / ``discount_text`` — so ``{"title": null}`` would pass validation, merge
        to ``NULL``, and hit a Postgres NotNullViolation → an unhandled 500.
        Distinguish "omitted" (keep existing) from "explicit null" via
        ``model_fields_set`` and 422 the latter. ``terms`` / ``tenant_branch_id``
        are nullable columns, so a null there is a legitimate clear.
        """
        for field in ("title", "description", "discount_text", "valid_from",
                      "valid_until", "is_active"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} cannot be set to null")
        return self
    tenant_branch_id: UUID | None = None

router = APIRouter(tags=["tenant-promotions"])


def _row_to_dto(row: asyncpg.Record) -> TenantPromotion:
    return TenantPromotion(
        id=row["id"],
        tenant_id=row["tenant_id"],
        tenant_branch_id=row["tenant_branch_id"],
        title=row["title"],
        description=row["description"],
        discount_text=row["discount_text"],
        terms=row["terms"],
        valid_from=row["valid_from"],
        valid_until=row["valid_until"],
        is_active=row["is_active"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


@router.get(
    "/",
    response_model=list[TenantPromotion],
    summary="List the tenant's promotions, newest first",
)
async def list_promotions(
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    conn: Annotated[asyncpg.Connection, Depends(get_request_conn)],
) -> list[TenantPromotion]:
    rows = await conn.fetch(
        """
        SELECT id, tenant_id, tenant_branch_id, title, description,
               discount_text, terms, valid_from, valid_until, is_active,
               created_at, updated_at
        FROM tenant_promotions
        ORDER BY created_at DESC
        """
    )
    return [_row_to_dto(r) for r in rows]


@router.get(
    "/{promotion_id}",
    response_model=TenantPromotion,
    responses={404: {"description": "Promotion not visible to caller"}},
    summary="Fetch one promotion",
)
async def get_promotion(
    promotion_id: UUID,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    conn: Annotated[asyncpg.Connection, Depends(get_request_conn)],
) -> TenantPromotion:
    row = await conn.fetchrow(
        """
        SELECT id, tenant_id, tenant_branch_id, title, description,
               discount_text, terms, valid_from, valid_until, is_active,
               created_at, updated_at
        FROM tenant_promotions
        WHERE id = $1
        """,
        promotion_id,
    )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "PROMOTION_NOT_FOUND",
                "message": f"promotion {promotion_id} not found",
            },
        )
    return _row_to_dto(row)


@router.post(
    "/",
    response_model=TenantPromotion,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new promotion",
)
async def create_promotion(
    body: TenantPromotionUpsert,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    conn: Annotated[asyncpg.Connection, Depends(get_request_conn)],
) -> TenantPromotion:
    row = await conn.fetchrow(
        """
        INSERT INTO tenant_promotions
            (tenant_id, tenant_branch_id, title, description, discount_text,
             terms, valid_from, valid_until, is_active)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
        RETURNING id, tenant_id, tenant_branch_id, title, description,
                  discount_text, terms, valid_from, valid_until, is_active,
                  created_at, updated_at
        """,
        ctx.tenant_id,
        body.tenant_branch_id,
        body.title,
        body.description,
        body.discount_text,
        body.terms,
        body.valid_from,
        body.valid_until,
        body.is_active,
    )
    assert row is not None
    logger.info(
        "promotion.create tenant=%s id=%s title=%r",
        ctx.tenant_id, row["id"], body.title,
    )
    return _row_to_dto(row)


@router.patch(
    "/{promotion_id}",
    response_model=TenantPromotion,
    summary="Partial update of an existing promotion",
)
async def update_promotion(
    promotion_id: UUID,
    body: TenantPromotionPatch,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    conn: Annotated[asyncpg.Connection, Depends(get_request_conn)],
) -> TenantPromotion:
    """Partial update — apply only the fields present in the request, keeping
    the rest as-is (QA finding L8). The full-body form keeps working; a caller
    sending ``{"title": "…"}`` alone no longer 422s on the omitted fields."""
    provided = body.model_dump(exclude_unset=True)

    existing = await conn.fetchrow(
        """
        SELECT id, tenant_id, tenant_branch_id, title, description,
               discount_text, terms, valid_from, valid_until, is_active,
               created_at, updated_at
          FROM tenant_promotions
         WHERE id = $1
        """,
        promotion_id,
    )
    if existing is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "PROMOTION_NOT_FOUND",
                "message": f"promotion {promotion_id} not found",
            },
        )

    # Merge the provided subset over the current row, then validate the date
    # range on the RESULT (either date may have been omitted). Mirrors the
    # TenantPromotionUpsert model_validator + the SQL CHECK.
    merged = {
        field: provided.get(field, existing[field])
        for field in (
            "tenant_branch_id", "title", "description", "discount_text",
            "terms", "valid_from", "valid_until", "is_active",
        )
    }
    if merged["valid_until"] <= merged["valid_from"]:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "PROMOTION_INVALID_RANGE",
                "message": "valid_until must be strictly after valid_from",
            },
        )

    row = await conn.fetchrow(
        """
        UPDATE tenant_promotions
        SET tenant_branch_id = $1,
            title            = $2,
            description      = $3,
            discount_text    = $4,
            terms            = $5,
            valid_from       = $6,
            valid_until      = $7,
            is_active        = $8,
            updated_at       = now()
        WHERE id = $9
        RETURNING id, tenant_id, tenant_branch_id, title, description,
                  discount_text, terms, valid_from, valid_until, is_active,
                  created_at, updated_at
        """,
        merged["tenant_branch_id"],
        merged["title"],
        merged["description"],
        merged["discount_text"],
        merged["terms"],
        merged["valid_from"],
        merged["valid_until"],
        merged["is_active"],
        promotion_id,
    )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "PROMOTION_NOT_FOUND",
                "message": f"promotion {promotion_id} not found",
            },
        )
    return _row_to_dto(row)


@router.delete(
    "/{promotion_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a promotion",
)
async def delete_promotion(
    promotion_id: UUID,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    conn: Annotated[asyncpg.Connection, Depends(get_request_conn)],
) -> None:
    """Hard delete. Beta operators consistently preferred this to soft-
    delete for promotions — promo state is transient + history isn't
    referenced by other tables (no FK from broadcasts / audience)."""
    result = await conn.execute(
        "DELETE FROM tenant_promotions WHERE id = $1", promotion_id,
    )
    # asyncpg returns 'DELETE n' on success; n=0 means RLS-stripped or absent.
    if result.endswith(" 0"):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "PROMOTION_NOT_FOUND",
                "message": f"promotion {promotion_id} not found",
            },
        )
    logger.info("promotion.delete tenant=%s id=%s", ctx.tenant_id, promotion_id)
    # 204 No Content — FastAPI returns empty body automatically.
    return None
