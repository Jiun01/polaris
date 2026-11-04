"""Knowledge-sources service — reconciler redesign.

CRUD operations for the declarative ``knowledge_sources`` ledger.
All three public functions receive the request-scoped ``asyncpg.Connection``
already inside a transaction with ``SET LOCAL app.current_tenant_id`` applied
(the dep chain in ``deps.tenant`` handles this).

Capacity gate (``insert_source``) reuses the same functions as the
``ingestion_requests`` enqueue path so the two submission surfaces are
governed by the same cap arithmetic and 409 shape.

Size estimates per source type (belt-and-braces; real usage is measured
as ``SUM(octet_length(content))`` over ``knowledge_chunks`` after sync):
  * url  — fixed heuristic (one URL site ≈ a handful of pages)
  * file — generous per-file estimate (Docling can expand PDFs)
  * text — exact byte length of the submitted body

The worker (``polaris_service``, BYPASSRLS) updates ledger columns
(``last_synced_hash``, ``status``, ``chunk_count``, ``error``,
``last_synced_at``) and hard-deletes tombstoned rows after chunk cleanup
— those writes are out of scope here.

RLS note: ``knowledge_sources_self_select / self_insert / self_update``
policies in migration 118 enforce tenant scoping at the DB layer.
The ``WHERE tenant_id = $N`` clauses below are belt-and-braces that catch
a "forgot SET LOCAL" bug as a 404 / empty-list rather than a leak.
"""

from __future__ import annotations

import logging
from typing import Any, Final
from uuid import UUID

import asyncpg
from fastapi import HTTPException, status
from polaris_shared_types import KnowledgeSourceCreate, KnowledgeSourceResponse

from polaris_api.services.knowledge_ingestion import (
    _capacity_exceeded,  # noqa: PLC2701 — shared 409 shape
    estimate_current_usage_mb,
    get_effective_cap_mb,
)

logger = logging.getLogger(__name__)

# Per-source size heuristics (see knowledge_ingestion.py for rationale).
_URL_ESTIMATE_MB: Final[int] = 5
_FILE_ESTIMATE_MB: Final[int] = 10
_TEXT_ESTIMATE_BYTES_PER_MB: Final[int] = 1_048_576  # 1 MiB


def _estimate_source_size_mb(body: KnowledgeSourceCreate) -> int:
    """Rough size estimate matching knowledge_ingestion.estimate_request_size_mb.

    Returns minimum 1 MB so a submission against a tenant exactly at cap
    still 409s instead of silently succeeding.
    """
    match body.source_type:
        case "url":
            return _URL_ESTIMATE_MB
        case "file":
            return _FILE_ESTIMATE_MB
        case "text":
            if body.source_text is None:
                return 1
            mb = len(body.source_text.encode("utf-8")) // _TEXT_ESTIMATE_BYTES_PER_MB
            return max(mb, 1)


def _row_to_response(row: dict[str, Any]) -> KnowledgeSourceResponse:
    """Map an asyncpg row dict to the tenant-facing response model."""
    return KnowledgeSourceResponse(
        id=row["id"],
        tenant_id=row["tenant_id"],
        tenant_branch_id=row["tenant_branch_id"],
        source_type=row["source_type"],
        source_url=row["source_url"],
        storage_path=row["storage_path"],
        source_label=row["source_label"],
        status=row["status"],
        chunk_count=row["chunk_count"],
        last_synced_at=row["last_synced_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


async def insert_source(
    conn: asyncpg.Connection,
    tenant_id: UUID,
    body: KnowledgeSourceCreate,
) -> KnowledgeSourceResponse:
    """Capacity-gate, then INSERT one ``knowledge_sources`` row.

    The caller supplies the request-scoped ``conn`` already in a
    transaction — capacity check + INSERT are one atomic unit.

    Raises:
      * HTTPException(409) — tenant over KB cap (``kb_capacity_exceeded``).
      * HTTPException(500) — no quota row (provisioning bug).
    """
    cap = await get_effective_cap_mb(conn, tenant_id)
    used = await estimate_current_usage_mb(conn, tenant_id)
    estimated = _estimate_source_size_mb(body)
    would_be = used + estimated
    if would_be > cap:
        logger.info(
            "knowledge_sources_capacity_gate_reject tenant_id=%s used_mb=%d "
            "cap_mb=%d would_be_mb=%d source_type=%s",
            tenant_id, used, cap, would_be, body.source_type,
        )
        raise _capacity_exceeded(used, cap, would_be)

    row = await conn.fetchrow(
        """
        INSERT INTO knowledge_sources (
            tenant_id, tenant_branch_id, source_type, source_url,
            storage_path, source_text, source_label, desired_hash
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        RETURNING id, tenant_id, tenant_branch_id, source_type, source_url,
                  storage_path, source_label, status, chunk_count,
                  last_synced_at, created_at, updated_at
        """,
        tenant_id,
        body.tenant_branch_id,
        body.source_type,
        body.source_url,
        body.storage_path,
        body.source_text,
        body.source_label,
        body.desired_hash,
    )
    # A None here means the INSERT...RETURNING was swallowed by the RLS
    # WITH CHECK policy — tenant_id mismatch between JWT and SET LOCAL GUC.
    # Surface as 500 (server bug, not tenant input).
    assert row is not None, (
        "INSERT...RETURNING returned no row (RLS WITH CHECK mismatch?)"
    )
    return _row_to_response(dict(row))


async def list_sources(
    conn: asyncpg.Connection,
    tenant_id: UUID,
    limit: int = 200,
) -> list[KnowledgeSourceResponse]:
    """Return the tenant's active (non-deleted) knowledge sources, newest first.

    Excludes rows with ``deleted_at IS NOT NULL`` (soft-deleted; pending
    chunk cleanup by the reconciler worker).

    Hard limit 200 prevents accidental full-table scans; portal paginates
    in a later phase. Belt-and-braces ``WHERE tenant_id = $1`` guards
    against a "forgot SET LOCAL" scenario at the app level (RLS already
    strips other tenants' rows).
    """
    capped = max(1, min(limit, 200))
    rows = await conn.fetch(
        """
        SELECT id, tenant_id, tenant_branch_id, source_type, source_url,
               storage_path, source_label, status, chunk_count,
               last_synced_at, created_at, updated_at
          FROM knowledge_sources
         WHERE tenant_id = $1
           AND deleted_at IS NULL
         ORDER BY created_at DESC
         LIMIT $2
        """,
        tenant_id,
        capped,
    )
    return [_row_to_response(dict(r)) for r in rows]


async def soft_delete_source(
    conn: asyncpg.Connection,
    tenant_id: UUID,
    source_id: UUID,
) -> None:
    """Soft-delete a knowledge source by setting ``deleted_at = now()``.

    The reconciler worker picks up tombstoned rows on the next Sync, deletes
    their chunks, then hard-deletes the row via service-role BYPASSRLS.

    RLS ``knowledge_sources_self_update`` policy scopes the UPDATE to the
    calling tenant; the explicit ``tenant_id = $2`` is belt-and-braces.

    Raises:
      * HTTPException(404) — row not found or already hard-deleted.
        (Also raised for already-soft-deleted rows — idempotent callers
        should tolerate 404 on a second delete attempt.)
    """
    result = await conn.execute(
        """
        UPDATE knowledge_sources
           SET deleted_at = now(),
               updated_at = now()
         WHERE id = $1
           AND tenant_id = $2
           AND deleted_at IS NULL
        """,
        source_id,
        tenant_id,
    )
    # asyncpg returns "UPDATE N" as a string; N=0 means no matching live row.
    updated = int(result.split()[-1])
    if updated == 0:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="source_not_found",
        )
