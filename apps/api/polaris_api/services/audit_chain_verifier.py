"""Audit hash-chain verifier service -- Phase 9 PR-5.

Wraps services/audit.py::verify_chain; persists results to
audit_chain_verifications (migration 054).

Called by two surfaces:
  - scheduled/audit_chain_verify.py (nightly 04:00 MYT APScheduler job)
  - POST /api/admin/audit/verify (on-demand admin button)

Both call run_verification(). The function:
  1. Fetches all distinct tenant IDs from audit_logs.
  2. Walks each tenant's chain with verify_chain().
  3. Writes ONE summary row to audit_chain_verifications per run
     (not one row per tenant -- the admin viewer shows a single
     "system-wide" status; the broken_at_id points to the first
     broken row found across any tenant).
  4. Returns a summary dict for the HTTP response.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID, uuid4

import asyncpg

from polaris_api.services.audit import verify_chain

logger = logging.getLogger(__name__)


async def run_verification(
    triggered_by: Literal["scheduled", "on_demand"],
    triggered_by_admin_id: UUID | None,
    pool: asyncpg.Pool,
) -> dict[str, Any]:
    """Walk all tenants' audit chains, write audit_chain_verifications row.

    Returns a summary dict:
      {
        id: UUID,
        status: "intact" | "broken" | "errored",
        rows_scanned: int,
        duration_ms: int,
        broken_at_id: UUID | None,
        checked_at: str (ISO UTC),
        triggered_by: str,
      }
    """
    start_ms = time.monotonic_ns() // 1_000_000
    verification_id = uuid4()
    checked_at = datetime.now(UTC)

    try:
        async with pool.acquire() as conn:
            # Collect all tenant IDs present in audit_logs.
            tenant_rows = await conn.fetch(
                "SELECT DISTINCT tenant_id FROM audit_logs ORDER BY tenant_id"
            )
            tenant_ids: list[UUID] = [r["tenant_id"] for r in tenant_rows]

            rows_scanned = 0
            broken_at_id: UUID | None = None
            broken_at_created_at: datetime | None = None
            broken_prev_hash: bytes | None = None
            broken_actual_hash: bytes | None = None

            # Walk each tenant's chain. Stop at first broken row found.
            for tenant_id in tenant_ids:
                # Count rows for this tenant so we can accumulate rows_scanned.
                count = await conn.fetchval(
                    "SELECT COUNT(*) FROM audit_logs WHERE tenant_id = $1",
                    tenant_id,
                )
                rows_scanned += int(count or 0)

                ok, broken_id = await verify_chain(conn, tenant_id)
                if not ok and broken_id is not None and broken_at_id is None:
                    # Capture detail about the broken row.
                    broken_at_id = broken_id
                    broken_row = await conn.fetchrow(
                        "SELECT created_at, prev_hash, row_hash "
                        "FROM audit_logs WHERE id = $1",
                        broken_id,
                    )
                    if broken_row is not None:
                        broken_at_created_at = broken_row["created_at"]
                        broken_prev_hash = (
                            bytes(broken_row["prev_hash"])
                            if broken_row["prev_hash"] is not None
                            else None
                        )
                        broken_actual_hash = (
                            bytes(broken_row["row_hash"])
                            if broken_row["row_hash"] is not None
                            else None
                        )

            status: str = "broken" if broken_at_id is not None else "intact"
            duration_ms = int((time.monotonic_ns() // 1_000_000) - start_ms)

            # Persist result.
            await conn.execute(
                """
                INSERT INTO audit_chain_verifications (
                    id, checked_at, status, rows_scanned, duration_ms,
                    broken_at_id, broken_at_created_at,
                    broken_prev_hash, broken_actual_hash,
                    triggered_by, triggered_by_admin_id
                ) VALUES (
                    $1, $2, $3, $4, $5,
                    $6, $7,
                    $8, $9,
                    $10, $11
                )
                """,
                verification_id,
                checked_at,
                status,
                rows_scanned,
                duration_ms,
                broken_at_id,
                broken_at_created_at,
                broken_prev_hash,
                broken_actual_hash,
                triggered_by,
                triggered_by_admin_id,
            )

    except Exception:
        logger.exception(
            "audit_chain_verifier: unexpected error during verification run"
        )
        # Best-effort: write an errored row so the admin banner shows amber.
        duration_ms = int((time.monotonic_ns() // 1_000_000) - start_ms)
        status = "errored"
        rows_scanned = 0
        broken_at_id = None
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO audit_chain_verifications (
                        id, checked_at, status, rows_scanned, duration_ms,
                        triggered_by, triggered_by_admin_id,
                        notes
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                    """,
                    verification_id,
                    checked_at,
                    status,
                    rows_scanned,
                    duration_ms,
                    triggered_by,
                    triggered_by_admin_id,
                    "Verifier raised an exception; see API logs for details.",
                )
        except Exception:
            logger.exception(
                "audit_chain_verifier: also failed to write errored row"
            )

    return {
        "id": verification_id,
        "status": status,
        "rows_scanned": rows_scanned,
        "duration_ms": duration_ms,
        "broken_at_id": broken_at_id,
        "checked_at": checked_at.isoformat(),
        "triggered_by": triggered_by,
    }
