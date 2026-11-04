"""EA account provisioning — create/deprovision a tenant's Easy!Appointments
admin login + default provider + default service, and record the outcome on
``tenants.settings.ea_account``.

Mirrors the ``portal_invite`` contract: these functions NEVER raise into the
request path. EA being unreachable must not fail tenant creation — the caller
audits the returned ``EaProvisionResult`` either way and the operator can retry
from the tenant-detail page.

The actual create-admin-and-email-reset work happens server-side in the EA fork
(``POST /api/v1/internal/tenants/provision``); here we just invoke it over HMAC
via ``EABookingClient`` and persist the returned ids.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import asyncpg

from polaris_api.services.booking.client import (
    EABookingClient,
    EABookingError,
    get_ea_booking_client,
)
from polaris_api.services.booking.settings_sync import sync_ea_calendar

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EaProvisionResult:
    """Outcome of an EA provisioning / deprovisioning attempt.

    ``provisioned`` is True only when a provision call succeeded. The created
    ids are persisted to ``tenants.settings.ea_account`` (the source of truth),
    so they are not carried here — callers only need success + reason.
    """

    provisioned: bool
    failure_reason: str | None = None
    # The final EA admin login username (operator-chosen or derived admin-<slug>),
    # echoed back so the admin UI can show it. None on failure / older EA images.
    username: str | None = None


def split_display_name(display_name: str | None) -> tuple[str, str]:
    """Split a display name into (first, last) with owner-y defaults.

    Shared by the create-tenant wizard wiring and the tenant-detail provision
    endpoint so the derived EA admin name can't drift between the two paths.
    """
    parts = (display_name or "").strip().split(None, 1)
    if not parts:
        return ("Calendar", "Owner")
    if len(parts) == 1:
        return (parts[0], "Owner")
    return (parts[0], parts[1])


async def create_ea_account(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    slug: str,
    email: str,
    first_name: str,
    last_name: str,
    username: str = "",
    company_name: str = "",
    company_link: str = "",
    timezone: str = "",
    ea_client: EABookingClient | None = None,
) -> EaProvisionResult:
    """Provision the tenant's EA account; persist ids; never raise.

    On success writes ``tenants.settings.ea_account`` (merged, so other
    settings keys are untouched) and returns ``provisioned=True``. On any
    EA error returns ``provisioned=False`` with a ``failure_reason``.

    ``company_name`` / ``company_link`` / ``timezone`` (optional) seed the
    new EA tenant's company settings; empty values fall back to EA-side
    defaults. Forwarded verbatim to ``provision_admin``.
    """
    client = ea_client or get_ea_booking_client()
    try:
        body = await client.provision_admin(
            tenant_id=tenant_id,
            slug=slug,
            email=email,
            first_name=first_name,
            last_name=last_name,
            username=username,
            company_name=company_name,
            company_link=company_link,
            timezone=timezone,
        )
    except EABookingError as exc:
        # Prefer EA's machine code (e.g. "username_taken"/"invalid_username") over
        # the generic exception class name, so the operator gets an actionable reason.
        reason = getattr(exc, "ea_code", None) or type(exc).__name__
        logger.warning(
            "create_ea_account: EA provision failed tenant=%s reason=%s msg=%s",
            tenant_id,
            reason,
            exc,
        )
        return EaProvisionResult(provisioned=False, failure_reason=reason)

    ea_account: dict[str, Any] = {
        "admin_user_id": body.get("admin_user_id"),
        "provider_id": body.get("provider_id"),
        "service_id": body.get("service_id"),
        "username": body.get("username"),
        "email": email,
        "status": "active",
        "provisioned_at": datetime.now(UTC).isoformat(),
    }
    # Replace the whole block on (re)provision so a stale ``deprovisioned_at``
    # from a prior teardown can't survive alongside the new ``provisioned_at``.
    await _write_ea_account(
        conn, tenant_id=tenant_id, ea_account=ea_account, merge=False
    )

    # Register the slug→tenant routing row in EA so the public booking page
    # ``{base_url}/book/{slug}`` resolves (EA's ``Booking::slug`` 404s when the
    # slug is absent from ``ea_polaris_tenants``). Provision seeds the EA account
    # but NOT this row, so without it the bot's booking link is a dead 404.
    # Best-effort — a sync failure must NOT fail provisioning; re-provision
    # backfills it (idempotent upsert EA-side).
    try:
        await client.sync_tenant(tenant_id=tenant_id, slug=slug, status="active")
    except Exception as exc:  # noqa: BLE001 — never block provisioning on a routing miss
        logger.warning(
            "create_ea_account: EA tenant slug-sync failed tenant=%s slug=%s: %s",
            tenant_id,
            slug,
            exc,
        )

    # Initial mirror: pull EA's freshly-seeded settings into the bot's
    # booking_config + ea_calendar so the bot enforces EA's policy from day one
    # (provision seeds EA defaults; this reflects them into Polaris). Best-effort
    # — a sync failure must NOT fail provisioning; the scheduled tick catches up.
    try:
        await sync_ea_calendar(conn, tenant_id=tenant_id, ea_client=client)
    except Exception as exc:  # noqa: BLE001 — never block provisioning on a mirror miss
        logger.warning(
            "create_ea_account: initial EA sync failed tenant=%s: %s",
            tenant_id,
            exc,
        )

    return EaProvisionResult(provisioned=True, username=body.get("username"))


async def deprovision_ea_account(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    ea_client: EABookingClient | None = None,
    write_settings: bool = True,
) -> EaProvisionResult:
    """Remove the tenant's EA account; mark settings; never raise.

    Best-effort: a failure is logged and reflected in the result, but the
    caller (hard-delete) proceeds regardless. ``write_settings=False`` skips
    the ``settings.ea_account`` marker — pointless when the caller is about to
    purge the tenant row (hard-delete).
    """
    client = ea_client or get_ea_booking_client()
    try:
        await client.deprovision(tenant_id=tenant_id)
    except EABookingError as exc:
        reason = type(exc).__name__
        logger.warning(
            "deprovision_ea_account: EA deprovision failed tenant=%s reason=%s",
            tenant_id,
            reason,
        )
        return EaProvisionResult(provisioned=False, failure_reason=reason)

    if not write_settings:
        return EaProvisionResult(provisioned=False)

    # Mark the (possibly already-gone) tenant row's settings — merged so the
    # ids stay for the audit trail. Best-effort: a write failure here must NOT
    # propagate, because the caller (hard_delete) treats this whole call as
    # fire-and-forget and a raise would roll back the purge transaction.
    try:
        await _write_ea_account(
            conn,
            tenant_id=tenant_id,
            ea_account={
                "status": "deprovisioned",
                "deprovisioned_at": datetime.now(UTC).isoformat(),
            },
            merge=True,
        )
    except Exception as exc:  # noqa: BLE001 — never block the caller
        logger.warning(
            "deprovision_ea_account: settings write failed tenant=%s: %s",
            tenant_id,
            exc,
        )
    return EaProvisionResult(provisioned=False)


async def _write_ea_account(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    ea_account: dict[str, Any],
    merge: bool,
) -> None:
    """Write ``tenants.settings.ea_account`` atomically, preserving other keys.

    ``merge=True`` keeps existing ``ea_account`` fields (e.g. ids on
    deprovision); ``merge=False`` replaces the whole block (provision, so a
    stale ``deprovisioned_at`` can't linger). The dict is bound directly — the
    connection's JSONB codec serialises once (json.dumps here would
    double-encode, breaking ``settings->>'...'`` reads).
    """
    if merge:
        await conn.execute(
            """
            UPDATE tenants
               SET settings = settings || jsonb_build_object(
                     'ea_account',
                     COALESCE(settings->'ea_account', '{}'::jsonb) || $2::jsonb
                   )
             WHERE id = $1
            """,
            tenant_id,
            ea_account,
        )
    else:
        await conn.execute(
            """
            UPDATE tenants
               SET settings = settings
                   || jsonb_build_object('ea_account', $2::jsonb)
             WHERE id = $1
            """,
            tenant_id,
            ea_account,
        )
