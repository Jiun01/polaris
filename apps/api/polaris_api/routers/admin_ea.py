"""Admin routes — tenant EA (Easy!Appointments) account management (v2.1).

Mounted at /api/admin/tenants.

Endpoints: create/delete a tenant's EA account. Branch↔EA-provider binding
reconciliation runs automatically at provisioning time via
``services/booking/branch_bindings.py::sync_tenant_branch_ea_bindings``; the
manual ``POST /{tenant_id}/ea-providers/sync`` trigger that once lived here
was removed 2026-07-02 (dead-code audit — zero UI callers).

Design contract (ADR: branch-centric EA routing):
  * No typed ids. The operator only picks the tenant; the system reads the
    opaque EA integer ids back from EA. Drift via hand-typed ids would
    silently misroute bookings to the wrong calendar.
  * platform_admin only; audited.
"""

from __future__ import annotations

import logging
from typing import Annotated
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, EmailStr, Field

from polaris_api.routers.admin_tenants import require_platform_admin
from polaris_api.services.admin import get_admin_service_conn
from polaris_api.services.audit import record as audit_record
from polaris_api.services.booking.account_provisioning import (
    create_ea_account,
    deprovision_ea_account,
    split_display_name,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["admin-ea"])

_FORBID = ConfigDict(extra="forbid")


class _EaAccountCreateRequest(BaseModel):
    model_config = _FORBID

    # The reset-link recipient (EA admin login). The wizard passes the EA email;
    # the tenant-detail button passes the tenant's primary user email.
    email: EmailStr
    # Optional operator-chosen EA admin login username. Blank/None → the EA side
    # derives ``admin-<slug>``. Pattern mirrors the EA-side validation (EA re-checks
    # + enforces global uniqueness, returning 409 username_taken on collision).
    username: str | None = Field(default=None, pattern=r"^[A-Za-z0-9._-]{3,50}$")


class _EaAccountResponse(BaseModel):
    model_config = _FORBID

    provisioned: bool
    failure_reason: str | None = None
    # Final EA admin login username (operator-chosen or derived admin-<slug>) so the
    # admin UI can show it in the success toast. None on failure.
    username: str | None = None


@router.post("/{tenant_id}/ea-account", response_model=_EaAccountResponse)
async def create_tenant_ea_account(
    tenant_id: UUID,
    payload: _EaAccountCreateRequest,
    conn: Annotated[asyncpg.Connection, Depends(get_admin_service_conn)],
    admin_id: Annotated[UUID, Depends(require_platform_admin)],
) -> _EaAccountResponse:
    """Provision an EA admin + default provider/service for a tenant and email
    the admin a password-reset link. 409 if one is already active."""
    row = await conn.fetchrow(
        """
        SELECT slug, display_name, settings->'ea_account'->>'status' AS ea_status
          FROM tenants WHERE id = $1 LIMIT 1
        """,
        tenant_id,
    )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="tenant_not_found"
        )
    if row["ea_status"] == "active":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="ea_account_already_active"
        )

    first_name, last_name = split_display_name(row["display_name"])
    result = await create_ea_account(
        conn,
        tenant_id=tenant_id,
        slug=row["slug"],
        email=str(payload.email),
        first_name=first_name,
        last_name=last_name,
        username=(payload.username or ""),
        # Seed the EA company_name from the tenant's real display name (the
        # first/last split above is only for the admin user record). Link +
        # timezone fall back to EA-side defaults.
        company_name=(row["display_name"] or ""),
    )

    await audit_record(
        conn,
        tenant_id=tenant_id,
        actor=admin_id,
        action=(
            "tenant.ea_account_created"
            if result.provisioned
            else "tenant.ea_account_creation_failed"
        ),
        after={
            "provisioned": result.provisioned,
            "failure_reason": result.failure_reason,
            "ea_email": str(payload.email),
            "ea_username": result.username,
        },
    )
    return _EaAccountResponse(
        provisioned=result.provisioned,
        failure_reason=result.failure_reason,
        username=result.username,
    )


@router.delete("/{tenant_id}/ea-account", response_model=_EaAccountResponse)
async def delete_tenant_ea_account(
    tenant_id: UUID,
    conn: Annotated[asyncpg.Connection, Depends(get_admin_service_conn)],
    admin_id: Annotated[UUID, Depends(require_platform_admin)],
) -> _EaAccountResponse:
    """Deprovision a tenant's EA account (operator-triggered)."""
    existing = await conn.fetchrow(
        "SELECT id FROM tenants WHERE id = $1 LIMIT 1", tenant_id
    )
    if existing is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="tenant_not_found"
        )

    result = await deprovision_ea_account(conn, tenant_id=tenant_id)

    await audit_record(
        conn,
        tenant_id=tenant_id,
        actor=admin_id,
        action=(
            "tenant.ea_account_deprovisioned"
            if not result.failure_reason
            else "tenant.ea_account_deprovision_failed"
        ),
        after={"failure_reason": result.failure_reason},
    )
    return _EaAccountResponse(
        provisioned=False, failure_reason=result.failure_reason
    )
