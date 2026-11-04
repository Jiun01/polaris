"""Knowledge-ingestion service — Phase 3 PR-1.

Capacity gate + enqueue + cancel for ``ingestion_requests``. All three
operations run under the request-scoped ``polaris_anon`` connection
opened by ``deps.tenant.get_tenant_context`` (Q-PHASE3-09 Option A —
the SET LOCAL ``app.current_tenant_id`` GUC is already set, and both
``tenant_quotas`` + ``tenant_billing`` have ``self_select`` RLS
policies keyed on that GUC, so the LEFT JOIN in
``get_effective_cap_mb`` returns the tenant's own row without
needing the BYPASSRLS service pool).

Capacity is computed live per Q-PHASE3-08 + Q-PHASE3-17:
``SUM(octet_length(content))`` over ``knowledge_chunks`` rather than a
denormalised counter. Cheap at GA volume (~50 ms for typical tenant);
revisit if a Phase 10 perf gate shows pain.

The desktop (Polaris Ingestion System — out of monorepo per ADR-057)
writes ``knowledge_chunks`` via service-role pool; this module only
reads chunks for the live SUM and only writes / cancels
``ingestion_requests``.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any, Final
from uuid import UUID

import asyncpg
from fastapi import HTTPException, status
from polaris_shared_types import IngestionRequestResponse, IngestionRequestSubmit

logger = logging.getLogger(__name__)

# 24h tenant-facing SLA per PRD §6.2. Used to compute the ``eta`` field
# returned in IngestionRequestResponse so portal can show the operator
# "expect chunks queryable by ..." text without re-deriving the policy.
_SLA_HOURS: Final[int] = 24

# Per-source heuristics for the pre-enqueue size estimate. Real size is
# known only after the desktop fetches + chunks + embeds; this estimate
# exists to reject "obviously too big" submissions at intake time so the
# tenant gets an immediate 409 instead of a failed run hours later.
#
# Heuristics intentionally generous: under-estimating an over-cap
# request is the safe-failure direction (the desktop's final write
# would still see capacity exceeded and abort). Over-estimating a
# borderline request denies a tenant in advance, which is the
# wrong-failure direction. Tune via Phase 10 measurement, not theory.
_URL_ESTIMATE_MB: Final[int] = 5      # one URL ≈ a handful of pages
_FILE_ESTIMATE_MB_PER_PATH: Final[int] = 10
_TEXT_ESTIMATE_BYTES_PER_MB: Final[int] = 1_048_576  # 1 MiB


async def estimate_current_usage_mb(
    conn: asyncpg.Connection, tenant_id: UUID
) -> int:
    """Live SUM of ``octet_length(content)`` over ``knowledge_chunks``.

    Q-PHASE3-17 lock: no ``current_used_mb`` column on
    ``tenant_quotas``; the answer is computed on read. The query is
    RLS-gated by the ``app.current_tenant_id`` GUC set by
    ``get_tenant_context``, so the tenant_id arg is belt-and-braces
    (the RLS policy on ``knowledge_chunks`` already strips other
    tenants' rows).

    Returns rounded-down megabytes. A tenant with zero chunks returns 0
    (COALESCE handles the empty-aggregate case).
    """
    row = await conn.fetchrow(
        """
        SELECT COALESCE(SUM(octet_length(content)), 0)::bigint / 1048576 AS used_mb
          FROM knowledge_chunks
         WHERE tenant_id = $1
        """,
        tenant_id,
    )
    # asyncpg returns Decimal or int depending on the cast path; coerce.
    return int(row["used_mb"]) if row is not None else 0


async def get_effective_cap_mb(
    conn: asyncpg.Connection, tenant_id: UUID
) -> int:
    """Effective KB cap = ``tenant_billing.kb_mb_cap_override`` if set,
    otherwise ``tenant_quotas.knowledge_chunks_total_mb``.

    Per ADR-032 Amendment: Enterprise SOWs may override the tier
    default. The LEFT JOIN handles tenants without a billing row
    (Phase 0/1 fixture tenants — fall back to quotas).

    Raises HTTPException(500) if the tenant has no quota row at all —
    that indicates a provisioning bug (every tenant should be seeded
    via Phase 6 sales-led flow with a quotas row), not a tenant error.
    """
    row = await conn.fetchrow(
        """
        SELECT COALESCE(b.kb_mb_cap_override, q.knowledge_chunks_total_mb)
                 AS effective_cap_mb
          FROM tenant_quotas q
     LEFT JOIN tenant_billing b ON b.tenant_id = q.tenant_id
         WHERE q.tenant_id = $1
        """,
        tenant_id,
    )
    if row is None:
        # No quotas row = provisioning bug, not tenant error. 500 + log
        # so ops sees it; tenant sees a generic "try again" via FastAPI's
        # default exception handler (no internal detail leak).
        logger.error(
            "tenant_quotas row missing for tenant_id=%s (provisioning bug)",
            tenant_id,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="quota_unavailable",
        )
    return int(row["effective_cap_mb"])


def estimate_request_size_mb(body: IngestionRequestSubmit) -> int:
    """Rough size estimate per source type.

    URL: fixed estimate per crawl (depth/pages already capped at
    Pydantic layer). File: per-path estimate (caller has already passed
    the per-file storage check in /uploads/sign). Text: actual byte
    length of source_text. Returns minimum 1 MB so trivial submissions
    against a tenant at exactly the cap still 409 rather than silently
    succeeding.
    """
    match body.source_type:
        case "url":
            return _URL_ESTIMATE_MB
        case "file":
            count = len(body.storage_paths) if body.storage_paths else 1
            return count * _FILE_ESTIMATE_MB_PER_PATH
        case "text":
            if body.source_text is None:
                return 1
            mb = len(body.source_text.encode("utf-8")) // _TEXT_ESTIMATE_BYTES_PER_MB
            return max(mb, 1)


def _capacity_exceeded(used: int, cap: int, would_be: int) -> HTTPException:
    """Shape the 409 per PRD §6.2 / AC-INGESTION-002.

    Detail payload is structured so the portal can render the
    capacity-bar widget directly from the response (current / cap /
    would-be in MB + a hint for the tenant).
    """
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "error": "kb_capacity_exceeded",
            "current_mb": used,
            "tier_cap_mb": cap,
            "would_be_mb": would_be,
            "hint": "Upgrade plan or delete unused sources",
        },
    )


async def enqueue_request(
    conn: asyncpg.Connection,
    tenant_id: UUID,
    body: IngestionRequestSubmit,
) -> IngestionRequestResponse:
    """Capacity-gate, then INSERT one ``ingestion_requests`` row.

    The caller (router) supplies the request-scoped ``conn`` already in
    a transaction with ``SET LOCAL app.current_tenant_id`` applied —
    same connection FastAPI's Depends chain holds open per request, so
    everything here participates in one atomic unit. On 409 the
    transaction rolls back and no row is written.

    Known limitation — capacity-gate TOCTOU (review M1):
    The SELECT-then-INSERT runs under Postgres's default READ COMMITTED
    isolation, so two concurrent submissions from the same tenant can
    both read ``used = 99``, both compute ``would_be = 104 <= 100``,
    both INSERT. Acceptable at GA single-tenant-trickle volume; revisit
    when traffic grows by either (a) ``SET TRANSACTION ISOLATION LEVEL
    SERIALIZABLE`` on this path, (b) a per-tenant ``pg_advisory_xact_
    lock`` (same pattern as ``services/audit.py``), or (c) a CHECK
    trigger that re-validates capacity at INSERT time.
    """
    cap = await get_effective_cap_mb(conn, tenant_id)
    used = await estimate_current_usage_mb(conn, tenant_id)
    estimated = estimate_request_size_mb(body)
    would_be = used + estimated
    if would_be > cap:
        # Log on the server side for ops dashboards but keep the tenant
        # response generic. PRD §6.2 / AC-INGESTION-002.
        logger.info(
            "capacity_gate_reject tenant_id=%s used_mb=%d cap_mb=%d "
            "would_be_mb=%d source_type=%s",
            tenant_id, used, cap, would_be, body.source_type,
        )
        raise _capacity_exceeded(used, cap, would_be)

    row = await conn.fetchrow(
        """
        INSERT INTO ingestion_requests (
            tenant_id, tenant_branch_id, source_type, source_url,
            storage_paths, source_text, ingest_mode, ocr_enabled,
            ocr_languages, crawl_max_depth, crawl_max_pages,
            contextual_enrichment
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
        RETURNING id, submitted_at, status
        """,
        tenant_id,
        body.tenant_branch_id,
        body.source_type,
        str(body.source_url) if body.source_url is not None else None,
        body.storage_paths,
        body.source_text,
        body.ingest_mode,
        body.ocr_enabled,
        body.ocr_languages,
        body.crawl_max_depth,
        body.crawl_max_pages,
        body.contextual_enrichment,
    )
    # asyncpg's INSERT...RETURNING always returns a row for a successful
    # write; a None here would indicate an INSERT silently dropped by RLS
    # (the WITH CHECK policy failed). Surface as 500 — that's a server
    # bug (tenant_id mismatch between JWT and SET LOCAL), not tenant input.
    assert row is not None, "INSERT...RETURNING returned no row (RLS WITH CHECK mismatch?)"

    submitted_at = row["submitted_at"]
    return IngestionRequestResponse(
        id=row["id"],
        tenant_id=tenant_id,
        tenant_branch_id=body.tenant_branch_id,
        source_type=body.source_type,
        ingest_mode=body.ingest_mode,
        status=row["status"],
        submitted_at=submitted_at,
        started_at=None,
        completed_at=None,
        chunks_added=None,
        chunks_skipped=None,
        chunks_deleted=None,
        tokens_used=None,
        error=None,
        eta=submitted_at + timedelta(hours=_SLA_HOURS),
        status_url=f"/api/tenant/knowledge/requests/{row['id']}",
    )


async def list_requests(
    conn: asyncpg.Connection,
    tenant_id: UUID,
    limit: int = 50,
) -> list[IngestionRequestResponse]:
    """Tenant's recent ingestion requests, newest first.

    RLS strips other tenants' rows so the WHERE clause is belt-and-
    braces (catches "forgot SET LOCAL" as a zero-row result rather
    than a leak). Hard limit of 200 prevents accidental
    full-table scans; the portal's pagination ships in Phase 8.
    """
    capped_limit = max(1, min(limit, 200))
    rows = await conn.fetch(
        """
        SELECT id, tenant_branch_id, source_type, ingest_mode, status,
               submitted_at, started_at, completed_at, chunks_added,
               chunks_skipped, chunks_deleted, tokens_used, error
          FROM ingestion_requests
         WHERE tenant_id = $1
         ORDER BY submitted_at DESC
         LIMIT $2
        """,
        tenant_id,
        capped_limit,
    )
    return [_row_to_response(tenant_id, dict(r)) for r in rows]


async def get_request(
    conn: asyncpg.Connection,
    tenant_id: UUID,
    request_id: UUID,
) -> IngestionRequestResponse | None:
    row = await conn.fetchrow(
        """
        SELECT id, tenant_branch_id, source_type, ingest_mode, status,
               submitted_at, started_at, completed_at, chunks_added,
               chunks_skipped, chunks_deleted, tokens_used, error
          FROM ingestion_requests
         WHERE tenant_id = $1 AND id = $2
        """,
        tenant_id,
        request_id,
    )
    if row is None:
        return None
    return _row_to_response(tenant_id, dict(row))


async def cancel_request(
    conn: asyncpg.Connection,
    tenant_id: UUID,
    request_id: UUID,
) -> None:
    """UPDATE the row to ``cancelled`` if and only if status='pending'.

    AC-INGESTION-010: in-flight requests (status='processing') cannot
    be cancelled — the desktop owns the run at that point and a
    cancel-mid-run would orphan partially-written chunks.

    Returns silently on success (the router translates to 204). Raises:
      * 404 if the row doesn't exist or belongs to another tenant
        (RLS-gated SELECT returns 0 rows in both cases).
      * 409 if the row exists but is not pending.
    """
    # Look up the row first so we can distinguish 404 vs 409 in one
    # round-trip — UPDATE...RETURNING would return 0 rows for both
    # cases. RLS strips other tenants' rows so the tenant_id WHERE is
    # belt-and-braces (matches the policy USING clause).
    existing = await conn.fetchrow(
        "SELECT status FROM ingestion_requests WHERE id = $1 AND tenant_id = $2",
        request_id,
        tenant_id,
    )
    if existing is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="request_not_found"
        )
    if existing["status"] != "pending":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "not_cancellable",
                "current_status": existing["status"],
            },
        )
    await conn.execute(
        """
        UPDATE ingestion_requests
           SET status = 'cancelled'
         WHERE id = $1 AND tenant_id = $2 AND status = 'pending'
        """,
        request_id,
        tenant_id,
    )


def _row_to_response(
    tenant_id: UUID, row: dict[str, Any]
) -> IngestionRequestResponse:
    """Shape an asyncpg row dict into the tenant-facing response.

    PR-5 (review L1 + L2): ``row`` is typed ``dict[str, Any]`` (asyncpg
    Records type-erase per-column) so Pydantic does the runtime coercion
    without per-field ``# type: ignore[arg-type]``. The runtime check
    on ``submitted_at`` raises ``TypeError`` instead of ``assert`` so
    a Python launched with ``-O`` (asserts stripped) still surfaces a
    column-shape drift loudly.
    """
    submitted_at = row["submitted_at"]
    if not isinstance(submitted_at, datetime):
        raise TypeError(
            f"submitted_at column drift: expected datetime, got {type(submitted_at).__name__}"
        )
    eta = submitted_at + timedelta(hours=_SLA_HOURS)
    if eta.tzinfo is None:
        eta = eta.replace(tzinfo=UTC)
    return IngestionRequestResponse(
        id=row["id"],
        tenant_id=tenant_id,
        tenant_branch_id=row["tenant_branch_id"],
        source_type=row["source_type"],
        ingest_mode=row["ingest_mode"],
        status=row["status"],
        submitted_at=submitted_at,
        started_at=row["started_at"],
        completed_at=row["completed_at"],
        chunks_added=row["chunks_added"],
        chunks_skipped=row["chunks_skipped"],
        chunks_deleted=row["chunks_deleted"],
        tokens_used=row["tokens_used"],
        error=row["error"],
        eta=eta,
        status_url=f"/api/tenant/knowledge/requests/{row['id']}",
    )
