"""Tenant-facing knowledge ingestion routes — Phase 3 PR-1.

Mounted at ``/api/tenant/knowledge`` (per Q-PHASE3-06 prefix layering:
``/v1/webhooks/*`` + ``/api/tenant/*`` + ``/api/admin/*``).

All routes consume ``TenantContext`` + ``get_request_conn`` so:

  * the JWT is decoded once,
  * one asyncpg connection is checked out per request,
  * one transaction wraps the whole handler (SET LOCAL
    ``app.current_tenant_id`` persists, rollback-on-exception is
    automatic).

The capacity-gate live SUM, the INSERT, and the cancel UPDATE all run
on this same connection, so they participate in one atomic unit (a
mid-request crash leaves no half-written rows).

``POST /uploads/sign`` is a thin Supabase Storage signed-upload-URL
issuer with a pre-upload capacity check. The actual upload happens
client-side (tenant PUTs to the returned URL); the desktop later
fetches via the storage_path written into the subsequent
``POST /requests`` payload.
"""

from __future__ import annotations

import logging
import uuid
from typing import Annotated
from uuid import UUID

import asyncpg
import httpx
from fastapi import APIRouter, Depends, HTTPException, Response, status
from polaris_shared_types import (
    IngestionRequestResponse,
    IngestionRequestSubmit,
    KnowledgeSourceCreate,
    KnowledgeSourceResponse,
)
from pydantic import BaseModel, Field, field_validator

from polaris_api.config import get_settings
from polaris_api.deps.tenant import (
    TenantContext,
    get_request_conn,
    get_tenant_context,
)
from polaris_api.services.knowledge_ingestion import (
    cancel_request,
    enqueue_request,
    estimate_current_usage_mb,
    get_effective_cap_mb,
    get_request,
    list_requests,
)
from polaris_api.services.knowledge_sources import (
    insert_source,
    list_sources,
    soft_delete_source,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["tenant-knowledge"])

# Supabase Storage bucket for tenant-uploaded ingestion source files.
# Bucket creation + RLS lives outside Phase 3 schema (Supabase Storage
# uses its own object-level policy system); ops creates the bucket
# manually as part of the GA cutover.
_INGESTION_BUCKET = "polaris-ingestion-uploads"

# Signed-URL TTL per AC-INGESTION-003. 15 minutes is the Supabase
# Storage default for signed-upload URLs; long enough for a tenant to
# upload via the portal even on slow connections, short enough that a
# leaked URL has limited window of abuse.
_UPLOAD_URL_TTL_SECONDS = 15 * 60

# Hard ceiling on a single file upload. Larger uploads should be split
# by the tenant; the desktop's downstream chunker assumes individual
# files fit comfortably in memory.
_MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # 50 MiB

# Allowlist of MIME prefixes for tenant file uploads (PR-5 / review M5
# fix). Defence-in-depth in front of Supabase's bucket policy: an
# attacker passing `application/x-msdownload` 422s here instead of
# round-tripping to Storage and back. Match by prefix so a vendor-
# specific image MIME (e.g. `image/svg+xml`) lands without enumeration.
# The desktop's downstream ingestion can still refuse types it doesn't
# know how to process (EasyOCR / Docling will skip them); this gate is
# the FIRST line, not the only one.
_ALLOWED_CONTENT_TYPE_PREFIXES: tuple[str, ...] = (
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.",  # .docx, .xlsx, .pptx
    "application/vnd.oasis.opendocument.",             # .odt, .ods, .odp
    "text/",                                            # plain, markdown, csv, html
    "image/",                                           # png, jpeg, webp, svg+xml, etc.
)


class SignUploadRequest(BaseModel):
    """Pre-upload capacity check + bucket signing request."""

    file_size_bytes: int = Field(ge=1, le=_MAX_UPLOAD_BYTES)
    content_type: str = Field(min_length=1, max_length=200)

    @field_validator("content_type")
    @classmethod
    def _content_type_allowed(cls, value: str) -> str:
        normalised = value.split(";", 1)[0].strip().lower()
        if not any(
            normalised.startswith(prefix)
            for prefix in _ALLOWED_CONTENT_TYPE_PREFIXES
        ):
            raise ValueError(
                f"content_type {normalised!r} not in upload allowlist "
                "(PDF, Office, OpenDocument, text/*, image/* only)"
            )
        return normalised


class SignUploadResponse(BaseModel):
    """Tenant uploads via PUT to ``upload_url`` then POSTs the
    returned ``storage_path`` into ``POST /requests`` so the desktop
    can fetch the object via service-role on the next queue drain."""

    upload_url: str
    storage_path: str
    expires_in_seconds: int


@router.post(
    "/requests",
    response_model=IngestionRequestResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Submit a knowledge-ingestion request",
)
async def submit_request(
    body: IngestionRequestSubmit,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    conn: Annotated[asyncpg.Connection, Depends(get_request_conn)],
) -> IngestionRequestResponse:
    """Capacity-gate + enqueue. AC-INGESTION-001 / -002 / -004 / -009.

    .. deprecated::
        The imperative ``ingestion_requests`` queue is superseded by the
        declarative ``knowledge_sources`` ledger (reconciler redesign —
        see ``POST /sources``). This endpoint is retained for backward
        compatibility; new portal code should target ``/sources``.
    """
    return await enqueue_request(conn, ctx.tenant_id, body)


@router.get(
    "/requests",
    response_model=list[IngestionRequestResponse],
    summary="List my recent ingestion requests",
)
async def list_my_requests(
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    conn: Annotated[asyncpg.Connection, Depends(get_request_conn)],
    limit: int = 50,
) -> list[IngestionRequestResponse]:
    """.. deprecated:: Use ``GET /sources`` (reconciler model)."""
    return await list_requests(conn, ctx.tenant_id, limit=limit)


@router.get(
    "/requests/{request_id}",
    response_model=IngestionRequestResponse,
    summary="Show one ingestion request",
)
async def show_request(
    request_id: UUID,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    conn: Annotated[asyncpg.Connection, Depends(get_request_conn)],
) -> IngestionRequestResponse:
    """.. deprecated:: Use ``GET /sources`` (reconciler model)."""
    found = await get_request(conn, ctx.tenant_id, request_id)
    if found is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="request_not_found"
        )
    return found


@router.delete(
    "/requests/{request_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Cancel a pending ingestion request (AC-INGESTION-010)",
)
async def cancel_my_request(
    request_id: UUID,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    conn: Annotated[asyncpg.Connection, Depends(get_request_conn)],
) -> Response:
    """.. deprecated:: Use ``DELETE /sources/{id}`` (reconciler model)."""
    await cancel_request(conn, ctx.tenant_id, request_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# /sources — declarative knowledge-source ledger (reconciler redesign)
# ---------------------------------------------------------------------------
# These routes replace the imperative /requests queue. The portal writes
# desired-state rows here; the local reconciler worker diffs desired_hash
# vs last_synced_hash and syncs knowledge_chunks on each Sync run.
# See: ../docs/internal-ops/PRPs/plans/knowledge-ingestion-reconciler-redesign.plan.md
# ---------------------------------------------------------------------------


@router.post(
    "/sources",
    response_model=KnowledgeSourceResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Declare a knowledge source (reconciler desired-state ledger)",
)
async def create_source(
    body: KnowledgeSourceCreate,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    conn: Annotated[asyncpg.Connection, Depends(get_request_conn)],
) -> KnowledgeSourceResponse:
    """Capacity-gate + INSERT one ``knowledge_sources`` row with status='pending'.

    The row represents *desired state*. The local reconciler worker picks it
    up on the next Sync, fetches/chunks/embeds the source, and updates the
    ledger columns (``status``, ``chunk_count``, ``last_synced_hash``).

    ``desired_hash`` must be caller-supplied:
      * url  → SHA-256(url string)
      * file → SHA-256(file bytes, computed browser-side before the sign step)
      * text → SHA-256(source_text body)
    """
    return await insert_source(conn, ctx.tenant_id, body)


@router.get(
    "/sources",
    response_model=list[KnowledgeSourceResponse],
    summary="List active knowledge sources for the calling tenant",
)
async def list_my_sources(
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    conn: Annotated[asyncpg.Connection, Depends(get_request_conn)],
    limit: int = 200,
) -> list[KnowledgeSourceResponse]:
    """Return active (non-deleted) knowledge sources newest-first.

    Soft-deleted rows (``deleted_at IS NOT NULL``) are excluded — they are
    pending chunk cleanup by the reconciler and no longer shown in the portal.
    """
    return await list_sources(conn, ctx.tenant_id, limit=limit)


@router.delete(
    "/sources/{source_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Soft-delete a knowledge source",
)
async def delete_source(
    source_id: UUID,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    conn: Annotated[asyncpg.Connection, Depends(get_request_conn)],
) -> Response:
    """Soft-delete: sets ``deleted_at = now()`` on the source row.

    The source disappears from ``GET /sources`` immediately. The reconciler
    worker picks up the tombstone on the next Sync, deletes the source's
    ``knowledge_chunks``, then hard-deletes this row via service-role
    BYPASSRLS.

    Returns 404 if the source does not exist or is already deleted.
    """
    await soft_delete_source(conn, ctx.tenant_id, source_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


class CapacityResponse(BaseModel):
    """Capacity bar payload — PRD §6.2 portal widget consumes this directly."""

    used_mb: int = Field(ge=0)
    cap_mb: int = Field(ge=0)
    available_mb: int = Field(ge=0)


@router.get(
    "/capacity",
    response_model=CapacityResponse,
    summary="Live KB capacity for the calling tenant (portal capacity bar)",
)
async def get_capacity(
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    conn: Annotated[asyncpg.Connection, Depends(get_request_conn)],
) -> CapacityResponse:
    """Wraps the same SUM + cap functions the intake gate uses.

    Phase 8 portal calls this on the Knowledge submission screen to
    render `{used_mb} MB / {cap_mb} MB` plus a hint when the bar is
    nearly full. ``available_mb`` is clamped at 0 (an over-cap tenant
    via Enterprise-override downgrade still sees 0, not a negative).
    """
    cap_mb = await get_effective_cap_mb(conn, ctx.tenant_id)
    used_mb = await estimate_current_usage_mb(conn, ctx.tenant_id)
    return CapacityResponse(
        used_mb=used_mb,
        cap_mb=cap_mb,
        available_mb=max(0, cap_mb - used_mb),
    )


@router.post(
    "/uploads/sign",
    response_model=SignUploadResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Issue a 15-min Supabase Storage signed upload URL",
)
async def sign_upload(
    body: SignUploadRequest,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    conn: Annotated[asyncpg.Connection, Depends(get_request_conn)],
) -> SignUploadResponse:
    """Pre-upload capacity check, then mint a Supabase Storage upload URL.

    AC-INGESTION-003: the tenant must not be able to upload past their
    KB cap. We add the requested file size to the live SUM and refuse
    if it would push them over. The desktop later validates again at
    chunk-write time as belt-and-braces.

    The storage_path is namespaced under the tenant_id so the desktop's
    service-role fetch can verify the path matches the request's
    tenant before downloading (defence against a tenant submitting a
    request that references another tenant's storage path).
    """
    cap_mb = await get_effective_cap_mb(conn, ctx.tenant_id)
    used_mb = await estimate_current_usage_mb(conn, ctx.tenant_id)
    incoming_mb = max(1, body.file_size_bytes // (1024 * 1024))
    if used_mb + incoming_mb > cap_mb:
        logger.info(
            "uploads_sign_capacity_reject tenant_id=%s used_mb=%d cap_mb=%d "
            "incoming_mb=%d",
            ctx.tenant_id, used_mb, cap_mb, incoming_mb,
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "kb_capacity_exceeded",
                "current_mb": used_mb,
                "tier_cap_mb": cap_mb,
                "would_be_mb": used_mb + incoming_mb,
                "hint": "Upgrade plan or delete unused sources",
            },
        )

    settings = get_settings()
    if not settings.supabase_url or not settings.supabase_service_key:
        # Local dev / unconfigured environments: the capacity check still
        # runs and tells the tenant a real number; only the signing leg
        # is unavailable. 503 (not 500) so portal can render a clear
        # "storage temporarily unavailable" instead of a generic crash.
        logger.warning(
            "uploads_sign called but supabase_url / service_key not "
            "configured; returning 503"
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="storage_unconfigured",
        )

    storage_path = f"{ctx.tenant_id}/{uuid.uuid4()}"
    url = (
        f"{settings.supabase_url.rstrip('/')}/storage/v1/object/upload/sign/"
        f"{_INGESTION_BUCKET}/{storage_path}"
    )
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                url,
                headers={
                    "Authorization": f"Bearer {settings.supabase_service_key}",
                    "Content-Type": "application/json",
                },
                json={"expiresIn": _UPLOAD_URL_TTL_SECONDS},
            )
    except httpx.HTTPError as exc:
        logger.warning(
            "uploads_sign supabase storage call failed: %s", type(exc).__name__
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="storage_unavailable",
        ) from exc

    if resp.status_code >= 400:
        logger.warning(
            "uploads_sign supabase storage returned %d", resp.status_code
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="storage_unavailable",
        )

    payload = resp.json()
    upload_url = payload.get("url") or payload.get("signedURL") or ""
    if not upload_url:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="storage_unavailable",
        )
    return SignUploadResponse(
        upload_url=upload_url,
        storage_path=storage_path,
        expires_in_seconds=_UPLOAD_URL_TTL_SECONDS,
    )
