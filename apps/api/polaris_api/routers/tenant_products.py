"""Tenant-facing product catalog CRUD — Phase 15 PR-2.

Mounted at ``/api/tenant/products`` per the standard prefix layering.

Product endpoints (full REST CRUD):

  * ``GET    /``                         — list products, newest first.
  * ``GET    /{id}``                     — single product detail + gallery.
  * ``POST   /``                         — create (embeds name+description).
  * ``PATCH  /{id}``                     — full-shape update (re-embeds).
  * ``DELETE /{id}``                     — hard delete (cascades images).

Image endpoints (gallery management; storage_path comes from the PR-3
signed-upload flow):

  * ``POST   /{id}/images``              — attach an uploaded image.
  * ``DELETE /{id}/images/{image_id}``   — detach (also remove the blob in PR-3).
  * ``PATCH  /{id}/images/reorder``      — set sort_order from an id list.

Universal across tiers per ADR-061 (mirrors ADR-042 promotions posture).
Optional ``tenant_branch_id`` scopes a product to one location (NULL =
tenant-wide). Optional ``sku`` is unique per tenant when present.

RLS on ``tenant_products`` / ``tenant_product_images`` (migration 089)
scopes everything to the calling tenant; the router does NOT re-add a
redundant ``tenant_id`` filter so policy regressions surface in tests.

Embed-on-write: ``name + '\\n' + description`` is embedded synchronously via
``services.embed.embed_text`` and stored in ``tenant_products.embedding``
(``$n::vector`` bind). Embedding is fail-soft (zero vector on provider
failure) — the product still saves; vector retrieval simply contributes
0.0 until a later edit re-embeds. ``content_tsv`` (BM25 leg) is a generated
column, always populated.

Error envelope codes:
  * ``PRODUCT_NOT_FOUND``        — id missing or RLS-stripped.
  * ``PRODUCT_SKU_CONFLICT``     — (tenant_id, sku) partial-unique violation.
  * ``PRODUCT_IMAGE_NOT_FOUND``  — image id missing / not on this product.
  * ``PRODUCT_IMAGE_REORDER_MISMATCH`` — reorder id set != the product's images.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated
from uuid import UUID, uuid4

import asyncpg
import httpx
from fastapi import APIRouter, Depends, HTTPException, UploadFile, status
from fastapi import File as FileParam
from polaris_shared_types import ProductImage, TenantProduct, TenantProductUpsert
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from polaris_api.config import get_settings
from polaris_api.deps.tenant import (
    TenantContext,
    get_request_conn,
    get_tenant_context,
)
from polaris_api.services.embed import embed_text, to_pgvector_literal
from polaris_api.services.products.storage import (
    ALLOWED_IMAGE_CONTENT_TYPES,
    DEFAULT_MAX_UPLOAD_BYTES,
    ImageValidationError,
    delete_object,
    process_image,
    signed_read_url,
    upload_image,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["tenant-products"])

_FORBID = ConfigDict(extra="forbid")


# --------------------------------------------------------------- request bodies


class ProductImageReorder(BaseModel):
    """Body for reordering a product's gallery: image ids in display order."""

    model_config = _FORBID

    image_ids: list[UUID] = Field(min_length=1)


# --------------------------------------------------------------- row -> DTO


def _image_row_to_dto(row: asyncpg.Record) -> ProductImage:
    return ProductImage(
        id=row["id"],
        product_id=row["product_id"],
        storage_path=row["storage_path"],
        sort_order=row["sort_order"],
        width=row["width"],
        height=row["height"],
        bytes=row["bytes"],
        content_type=row["content_type"],
        created_at=row["created_at"],
        # url is minted at read time in PR-3/PR-6 (signed read URL); None here.
        url=None,
    )


def _product_row_to_dto(
    row: asyncpg.Record, images: list[ProductImage]
) -> TenantProduct:
    return TenantProduct(
        id=row["id"],
        tenant_id=row["tenant_id"],
        tenant_branch_id=row["tenant_branch_id"],
        sku=row["sku"],
        name=row["name"],
        description=row["description"],
        price_text=row["price_text"],
        is_active=row["is_active"],
        images=images,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _embedding_input(name: str, description: str) -> str:
    """Text embedded into the product vector. name + description only —
    price/sku are not semantic, so they stay out of the vector."""
    return f"{name}\n{description}"


async def _images_for(
    conn: asyncpg.Connection, product_ids: list[UUID]
) -> dict[UUID, list[ProductImage]]:
    """Fetch gallery images for the given products, grouped by product_id and
    ordered by sort_order then created_at. One query for the whole page."""
    if not product_ids:
        return {}
    rows = await conn.fetch(
        """
        SELECT id, product_id, storage_path, sort_order, width, height,
               bytes, content_type, created_at
        FROM tenant_product_images
        WHERE product_id = ANY($1::uuid[])
        ORDER BY product_id, sort_order, created_at
        """,
        product_ids,
    )
    grouped: dict[UUID, list[ProductImage]] = {pid: [] for pid in product_ids}
    for r in rows:
        grouped[r["product_id"]].append(_image_row_to_dto(r))
    return grouped


async def _sign_image_urls(images: list[ProductImage]) -> None:
    """Best-effort: populate each image's signed read URL for portal display.

    Leaves ``url=None`` when storage is unconfigured or a mint fails (the portal
    renders a placeholder). Mints concurrently to bound read latency.
    """
    if not images:
        return
    settings = get_settings()
    if not settings.supabase_url or not settings.supabase_service_key:
        return

    async def _one(img: ProductImage) -> None:
        try:
            img.url = await signed_read_url(img.storage_path, settings)
        except Exception:  # noqa: BLE001 — a missing thumbnail is non-critical
            img.url = None

    await asyncio.gather(*[_one(i) for i in images])


async def _delete_blobs(storage_paths: list[str]) -> None:
    """Best-effort delete of bucket objects after their DB rows are gone.

    The DB row deletion is the source of truth; a leftover blob is a storage
    leak, not a correctness bug, so a failure here logs and continues (a sweep
    can reap stragglers). Runs concurrently. (Q-PHASE15-05.)
    """
    if not storage_paths:
        return
    settings = get_settings()
    if not settings.supabase_url or not settings.supabase_service_key:
        return

    async def _one(path: str) -> None:
        try:
            await delete_object(path, settings)
        except Exception:  # noqa: BLE001 — blob cleanup is best-effort
            logger.warning("product image blob delete failed for path=%s", path)

    await asyncio.gather(*[_one(p) for p in storage_paths])


# --------------------------------------------------------------- product CRUD


@router.get(
    "/",
    response_model=list[TenantProduct],
    summary="List the tenant's products, newest first",
)
async def list_products(
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    conn: Annotated[asyncpg.Connection, Depends(get_request_conn)],
) -> list[TenantProduct]:
    rows = await conn.fetch(
        """
        SELECT id, tenant_id, tenant_branch_id, sku, name, description,
               price_text, is_active, created_at, updated_at
        FROM tenant_products
        ORDER BY created_at DESC
        """
    )
    images = await _images_for(conn, [r["id"] for r in rows])
    dtos = [_product_row_to_dto(r, images.get(r["id"], [])) for r in rows]
    await _sign_image_urls([img for d in dtos for img in d.images])
    return dtos


@router.get(
    "/{product_id}",
    response_model=TenantProduct,
    responses={404: {"description": "Product not visible to caller"}},
    summary="Fetch one product with its image gallery",
)
async def get_product(
    product_id: UUID,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    conn: Annotated[asyncpg.Connection, Depends(get_request_conn)],
) -> TenantProduct:
    row = await conn.fetchrow(
        """
        SELECT id, tenant_id, tenant_branch_id, sku, name, description,
               price_text, is_active, created_at, updated_at
        FROM tenant_products
        WHERE id = $1
        """,
        product_id,
    )
    if row is None:
        raise _not_found(product_id)
    images = await _images_for(conn, [product_id])
    dto = _product_row_to_dto(row, images.get(product_id, []))
    await _sign_image_urls(dto.images)
    return dto


@router.post(
    "/",
    response_model=TenantProduct,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new product (embeds name + description)",
)
async def create_product(
    body: TenantProductUpsert,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    conn: Annotated[asyncpg.Connection, Depends(get_request_conn)],
) -> TenantProduct:
    vec = to_pgvector_literal(
        await embed_text(_embedding_input(body.name, body.description), tenant_id=ctx.tenant_id)
    )
    try:
        row = await conn.fetchrow(
            """
            INSERT INTO tenant_products
                (tenant_id, tenant_branch_id, sku, name, description,
                 price_text, is_active, embedding)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8::vector)
            RETURNING id, tenant_id, tenant_branch_id, sku, name, description,
                      price_text, is_active, created_at, updated_at
            """,
            ctx.tenant_id,
            body.tenant_branch_id,
            body.sku,
            body.name,
            body.description,
            body.price_text,
            body.is_active,
            vec,
        )
    except asyncpg.UniqueViolationError as exc:
        raise _sku_conflict(body.sku) from exc
    assert row is not None
    logger.info(
        "product.create tenant=%s id=%s name=%r", ctx.tenant_id, row["id"], body.name
    )
    return _product_row_to_dto(row, [])


@router.patch(
    "/{product_id}",
    response_model=TenantProduct,
    summary="Full-shape update of an existing product (re-embeds)",
)
async def update_product(
    product_id: UUID,
    body: TenantProductUpsert,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    conn: Annotated[asyncpg.Connection, Depends(get_request_conn)],
) -> TenantProduct:
    """Full-shape replacement (the portal form carries every field). Always
    re-embeds: name/description may have changed and the vector must track."""
    vec = to_pgvector_literal(
        await embed_text(_embedding_input(body.name, body.description), tenant_id=ctx.tenant_id)
    )
    try:
        row = await conn.fetchrow(
            """
            UPDATE tenant_products
            SET tenant_branch_id = $1,
                sku              = $2,
                name             = $3,
                description      = $4,
                price_text       = $5,
                is_active        = $6,
                embedding        = $7::vector,
                updated_at       = now()
            WHERE id = $8
            RETURNING id, tenant_id, tenant_branch_id, sku, name, description,
                      price_text, is_active, created_at, updated_at
            """,
            body.tenant_branch_id,
            body.sku,
            body.name,
            body.description,
            body.price_text,
            body.is_active,
            vec,
            product_id,
        )
    except asyncpg.UniqueViolationError as exc:
        raise _sku_conflict(body.sku) from exc
    if row is None:
        raise _not_found(product_id)
    images = await _images_for(conn, [product_id])
    dto = _product_row_to_dto(row, images.get(product_id, []))
    await _sign_image_urls(dto.images)
    return dto


@router.delete(
    "/{product_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a product (cascades its images)",
)
async def delete_product(
    product_id: UUID,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    conn: Annotated[asyncpg.Connection, Depends(get_request_conn)],
) -> None:
    """Hard delete. tenant_product_images rows cascade via FK; the blobs in
    Supabase storage are reaped by the PR-3 detach path / a sweep."""
    # Capture image blob keys BEFORE the cascade removes the rows, so we can
    # reap the bucket objects (the FK cascade only drops DB rows). Q-PHASE15-05.
    img_rows = await conn.fetch(
        "SELECT storage_path FROM tenant_product_images WHERE product_id = $1",
        product_id,
    )
    result = await conn.execute(
        "DELETE FROM tenant_products WHERE id = $1", product_id
    )
    if result.endswith(" 0"):
        raise _not_found(product_id)
    await _delete_blobs([r["storage_path"] for r in img_rows])
    logger.info("product.delete tenant=%s id=%s", ctx.tenant_id, product_id)
    return None


# --------------------------------------------------------------- image gallery


@router.post(
    "/{product_id}/images",
    response_model=ProductImage,
    status_code=status.HTTP_201_CREATED,
    summary="Upload + attach an image to a product (downscaled server-side)",
)
async def upload_product_image(
    product_id: UUID,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    conn: Annotated[asyncpg.Connection, Depends(get_request_conn)],
    file: Annotated[UploadFile, FileParam(...)],
) -> ProductImage:
    """Multipart upload: validate → downscale (≤1600px, JPEG q80) → capacity-gate
    → store in the private bucket → record the row. Images pass through the API
    (not client-direct signed upload) so we can downscale + magic-byte validate
    server-side (ADR-061 §6)."""
    # 1. Product must be visible to the caller (RLS) before we accept bytes.
    exists = await conn.fetchval(
        "SELECT 1 FROM tenant_products WHERE id = $1", product_id
    )
    if exists is None:
        raise _not_found(product_id)

    # 2. Content-type allowlist (cheap gate before reading the body).
    if file.content_type not in ALLOWED_IMAGE_CONTENT_TYPES:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail={
                "code": "PRODUCT_IMAGE_UNSUPPORTED_TYPE",
                "message": (
                    f"content_type {file.content_type!r} not allowed; "
                    "use JPEG, PNG, or WebP"
                ),
            },
        )

    # 3. Size cap — read one byte past the limit to detect oversize.
    raw = await file.read(DEFAULT_MAX_UPLOAD_BYTES + 1)
    if len(raw) > DEFAULT_MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail={
                "code": "PRODUCT_IMAGE_TOO_LARGE",
                "max_bytes": DEFAULT_MAX_UPLOAD_BYTES,
            },
        )

    # 4. Validate + downscale off the event loop (Pillow is CPU-bound).
    try:
        processed = await run_in_threadpool(process_image, raw)
    except ImageValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "PRODUCT_IMAGE_INVALID",
                "message": "uploaded file is not a valid image",
            },
        ) from exc

    # 5. Capacity gate: tenant's stored image bytes + this one vs tier cap.
    used_bytes = int(
        await conn.fetchval(
            "SELECT COALESCE(SUM(bytes), 0) FROM tenant_product_images"
        )
        or 0
    )
    cap_mb = await conn.fetchval(
        "SELECT product_images_total_mb FROM tenant_quotas"
    )
    cap_bytes = int(cap_mb or 0) * 1024 * 1024
    if cap_bytes and used_bytes + len(processed.data) > cap_bytes:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "PRODUCT_IMAGE_CAPACITY_EXCEEDED",
                "used_bytes": used_bytes,
                "cap_mb": int(cap_mb or 0),
            },
        )

    # 6. Upload to the private bucket BEFORE the DB row (storage failure leaves
    #    no orphan row).
    settings = get_settings()
    if not settings.supabase_url or not settings.supabase_service_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="storage_unconfigured",
        )
    object_key = f"{ctx.tenant_id}/{uuid4()}.jpg"
    try:
        await upload_image(
            object_key, processed.data, processed.content_type, settings
        )
    except httpx.HTTPError as exc:
        logger.warning(
            "product.image.upload storage failed: %s", type(exc).__name__
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="storage_unavailable",
        ) from exc

    # 7. Append to the gallery at the next sort_order.
    next_order = int(
        await conn.fetchval(
            "SELECT COALESCE(MAX(sort_order) + 1, 0) FROM tenant_product_images "
            "WHERE product_id = $1",
            product_id,
        )
        or 0
    )
    row = await conn.fetchrow(
        """
        INSERT INTO tenant_product_images
            (tenant_id, product_id, storage_path, sort_order,
             width, height, bytes, content_type)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        RETURNING id, product_id, storage_path, sort_order, width, height,
                  bytes, content_type, created_at
        """,
        ctx.tenant_id,
        product_id,
        object_key,
        next_order,
        processed.width,
        processed.height,
        len(processed.data),
        processed.content_type,
    )
    assert row is not None
    logger.info(
        "product.image.upload tenant=%s product=%s image=%s bytes=%d",
        ctx.tenant_id, product_id, row["id"], len(processed.data),
    )
    dto = _image_row_to_dto(row)
    await _sign_image_urls([dto])
    return dto


@router.delete(
    "/{product_id}/images/{image_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Detach an image from a product",
)
async def detach_image(
    product_id: UUID,
    image_id: UUID,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    conn: Annotated[asyncpg.Connection, Depends(get_request_conn)],
) -> None:
    deleted = await conn.fetchrow(
        "DELETE FROM tenant_product_images WHERE id = $1 AND product_id = $2 "
        "RETURNING storage_path",
        image_id,
        product_id,
    )
    if deleted is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "PRODUCT_IMAGE_NOT_FOUND",
                "message": f"image {image_id} not found on product {product_id}",
            },
        )
    await _delete_blobs([deleted["storage_path"]])
    logger.info(
        "product.image.detach tenant=%s product=%s image=%s",
        ctx.tenant_id, product_id, image_id,
    )
    return None


@router.patch(
    "/{product_id}/images/reorder",
    response_model=list[ProductImage],
    summary="Reorder a product's gallery from an ordered id list",
)
async def reorder_images(
    product_id: UUID,
    body: ProductImageReorder,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    conn: Annotated[asyncpg.Connection, Depends(get_request_conn)],
) -> list[ProductImage]:
    """The id list must be exactly the product's current image set (a
    permutation) — partial reorders are rejected so sort_order stays a clean
    0..N-1 sequence."""
    current = await conn.fetch(
        "SELECT id FROM tenant_product_images WHERE product_id = $1",
        product_id,
    )
    if not current:
        raise _not_found(product_id)
    current_ids = {r["id"] for r in current}
    if set(body.image_ids) != current_ids:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "PRODUCT_IMAGE_REORDER_MISMATCH",
                "message": "image_ids must be exactly the product's current images",
            },
        )
    # Apply the new ordering in one statement via VALUES(ord, id) join.
    pairs = [(idx, img_id) for idx, img_id in enumerate(body.image_ids)]
    await conn.executemany(
        "UPDATE tenant_product_images SET sort_order = $1 WHERE id = $2 AND product_id = $3",
        [(ordr, img_id, product_id) for ordr, img_id in pairs],
    )
    images = await _images_for(conn, [product_id])
    logger.info(
        "product.image.reorder tenant=%s product=%s n=%d",
        ctx.tenant_id, product_id, len(pairs),
    )
    return images.get(product_id, [])


# --------------------------------------------------------------- error helpers


def _not_found(product_id: UUID) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={
            "code": "PRODUCT_NOT_FOUND",
            "message": f"product {product_id} not found",
        },
    )


def _sku_conflict(sku: str | None) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "code": "PRODUCT_SKU_CONFLICT",
            "message": f"sku {sku!r} already exists for this tenant",
        },
    )
