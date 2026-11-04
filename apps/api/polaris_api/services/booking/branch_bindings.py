"""Branch -> EA provider/service provisioning write-back (v2.1).

The branch-centric booking flow (``services/booking/conversational.py``)
routes a booking to ``tenant_branches.ea_provider_id`` /
``ea_default_service_id``. Those are opaque EA-internal integer ids, so
they must NEVER be a hand-typed operator/tenant field — drift would
silently misroute bookings to the wrong calendar. Instead the system sets
them automatically by reading them back from EA at provisioning time.

This module is the single write path shared by:

  * the production sync (``sync_tenant_branch_ea_bindings``), invoked
    automatically during EA provisioning (the manual admin sync endpoint
    was removed 2026-07-02 — dead-code audit); and
  * the E2E seed (``seed_e2e_ea_providers.py``), which resolves its known
    branch slug and calls ``set_branch_ea_binding_by_slug``.

Matching rule (provider -> branch), conservative to avoid misrouting:
  * exactly one branch + >=1 provider -> unambiguous bind;
  * otherwise match by normalised slug/name; only a single unambiguous
    hit binds. Anything ambiguous is REPORTED, never guessed.
``ea_default_service_id`` is the provider's first service id (validated
against the tenant's service list).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import asyncpg

from polaris_api.services.booking.client import (
    EABookingClient,
    get_ea_booking_client,
)

logger = logging.getLogger(__name__)


class EAProvisioningError(Exception):
    """Raised when a write-back targets a branch that does not exist."""


def _norm(value: str | None) -> str:
    """Lowercase alphanumeric-only key for slug/name matching."""
    if not value:
        return ""
    return "".join(ch for ch in value.lower() if ch.isalnum())


# -------------------------------------------------------------------------
# Low-level writers (idempotent; first-class columns only)
# -------------------------------------------------------------------------


async def set_branch_ea_binding(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    branch_id: UUID,
    ea_provider_id: int,
    ea_default_service_id: int | None,
) -> bool:
    """Write EA ids onto a branch by id. Idempotent — returns True only
    when a value actually changed (the UPDATE is a no-op otherwise)."""
    row = await conn.fetchrow(
        """
        UPDATE tenant_branches
           SET ea_provider_id        = $3,
               ea_default_service_id = $4,
               updated_at            = now()
         WHERE id = $1 AND tenant_id = $2
           AND (ea_provider_id        IS DISTINCT FROM $3
                OR ea_default_service_id IS DISTINCT FROM $4)
        RETURNING id
        """,
        branch_id,
        tenant_id,
        ea_provider_id,
        ea_default_service_id,
    )
    return row is not None


async def set_branch_ea_binding_by_slug(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    branch_slug: str,
    ea_provider_id: int,
    ea_default_service_id: int | None,
) -> UUID:
    """Write EA ids onto a branch by slug (used by the E2E seed, which
    knows its branch slugs). Raises ``EAProvisioningError`` if no branch
    matches — the seed must create the branch first."""
    row = await conn.fetchrow(
        """
        UPDATE tenant_branches
           SET ea_provider_id        = $3,
               ea_default_service_id = $4,
               updated_at            = now()
         WHERE tenant_id = $1 AND slug = $2
        RETURNING id
        """,
        tenant_id,
        branch_slug,
        ea_provider_id,
        ea_default_service_id,
    )
    if row is None:
        raise EAProvisioningError(
            f"no tenant_branches row for tenant_id={tenant_id} slug={branch_slug!r}"
        )
    branch_id: UUID = row["id"]
    return branch_id


# -------------------------------------------------------------------------
# Reconciliation sync (operator-triggered; no typed ids)
# -------------------------------------------------------------------------


@dataclass
class SyncReport:
    """Outcome of a provisioning sync for one tenant."""

    tenant_id: UUID
    bound: list[dict[str, Any]] = field(default_factory=list)          # newly written / changed
    unchanged: list[dict[str, Any]] = field(default_factory=list)      # already correct
    unmatched_providers: list[dict[str, Any]] = field(default_factory=list)
    unmatched_branches: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "tenant_id": str(self.tenant_id),
            "bound": self.bound,
            "unchanged": self.unchanged,
            "unmatched_providers": self.unmatched_providers,
            "unmatched_branches": self.unmatched_branches,
        }


def _provider_service_id(provider: dict[str, Any], valid_service_ids: set[int]) -> int | None:
    """First of the provider's services that is a valid tenant service id."""
    for sid in provider.get("services") or []:
        try:
            sid_int = int(sid)
        except (TypeError, ValueError):
            continue
        if sid_int in valid_service_ids:
            return sid_int
    return None


def _match_provider_to_branch(
    provider: dict[str, Any], branches: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """Return the single branch matching this provider by normalised
    slug/name, or None if zero or ambiguous (>1)."""
    email_local = (provider.get("email") or "").split("@", 1)[0]
    full_name = f"{provider.get('firstName', '')}{provider.get('lastName', '')}"
    provider_keys = {_norm(email_local), _norm(full_name)}
    provider_keys.discard("")
    hits = [
        b
        for b in branches
        if {_norm(b.get("slug")), _norm(b.get("name"))} & provider_keys
    ]
    return hits[0] if len(hits) == 1 else None


async def sync_tenant_branch_ea_bindings(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    ea_client: EABookingClient | None = None,
) -> SyncReport:
    """Reconcile a tenant's EA providers -> branch ea_provider/service ids.

    Reads EA ``list_providers`` + ``list_services`` (tenant-scoped JWT),
    matches providers to branches, and writes the first-class columns
    idempotently. ``conn`` is supplied by the caller (admin endpoint uses
    the service pool); every query is explicitly tenant-scoped.
    """
    client = ea_client or get_ea_booking_client()
    report = SyncReport(tenant_id=tenant_id)

    branch_rows = await conn.fetch(
        """
        SELECT id, slug, name, ea_provider_id, ea_default_service_id
          FROM tenant_branches
         WHERE tenant_id = $1
         ORDER BY created_at
        """,
        tenant_id,
    )
    branches = [dict(r) for r in branch_rows]
    providers = await client.list_providers(tenant_id=tenant_id)
    services = await client.list_services(tenant_id=tenant_id)
    valid_service_ids = {
        int(s["id"]) for s in services if isinstance(s.get("id"), int)
    }

    matched_branch_ids: set[UUID] = set()

    # Build (branch, provider) pairs.
    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    if len(branches) == 1 and providers:
        if len(providers) > 1:
            logger.warning(
                "ea sync: tenant=%s has 1 branch but %d providers; binding the first",
                tenant_id,
                len(providers),
            )
        pairs.append((branches[0], providers[0]))
        for extra in providers[1:]:
            report.unmatched_providers.append(
                {"id": extra.get("id"), "email": extra.get("email")}
            )
    else:
        for provider in providers:
            branch = _match_provider_to_branch(provider, branches)
            if branch is None:
                report.unmatched_providers.append(
                    {"id": provider.get("id"), "email": provider.get("email")}
                )
            else:
                pairs.append((branch, provider))

    for branch, provider in pairs:
        try:
            ea_provider_id = int(provider["id"])
        except (KeyError, TypeError, ValueError):
            report.unmatched_providers.append({"id": provider.get("id")})
            continue
        ea_service_id = _provider_service_id(provider, valid_service_ids)
        matched_branch_ids.add(branch["id"])
        entry = {
            "branch_id": str(branch["id"]),
            "branch_name": branch.get("name"),
            "ea_provider_id": ea_provider_id,
            "ea_default_service_id": ea_service_id,
        }
        if (
            branch.get("ea_provider_id") == ea_provider_id
            and branch.get("ea_default_service_id") == ea_service_id
        ):
            report.unchanged.append(entry)
            continue
        changed = await set_branch_ea_binding(
            conn,
            tenant_id=tenant_id,
            branch_id=branch["id"],
            ea_provider_id=ea_provider_id,
            ea_default_service_id=ea_service_id,
        )
        (report.bound if changed else report.unchanged).append(entry)

    for branch in branches:
        if branch["id"] not in matched_branch_ids:
            report.unmatched_branches.append(
                {"branch_id": str(branch["id"]), "branch_name": branch.get("name")}
            )

    logger.info(
        "ea sync tenant=%s bound=%d unchanged=%d unmatched_providers=%d "
        "unmatched_branches=%d",
        tenant_id,
        len(report.bound),
        len(report.unchanged),
        len(report.unmatched_providers),
        len(report.unmatched_branches),
    )
    return report
