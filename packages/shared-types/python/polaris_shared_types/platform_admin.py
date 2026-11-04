"""Platform admin auth subject — Pydantic SSOT (Phase 9 PR-1).

The platform_admin is the operator of admin.getpolaris.my — Polaris's
sole sales-led onboarding path per ADR-012. At GA there is one
platform_admin (the operator). Magic-link auth only per ADR-030 Amendment
2026-05-20; defence-in-depth via Caddy basic-auth, 10-minute link
expiry, IP-bind callback, and 1-day session lifetime (all out-of-DB
controls). No MFA columns at GA.

Backing table: ``platform_admins`` (migration 054).

Anchors:
  - ADR-030 (2026-05-15): original mandatory-MFA decision (superseded
    for platform_admin only)
  - ADR-030 Amendment 2026-05-20: drops platform_admin MFA at GA with
    4 compensating controls; reversal trigger = second platform_admin
    joining (new amendment + new migration)
  - Q-PHASE9-09: separate table (not a flag on tenant_users) keeps auth
    subjects orthogonal and RLS-reasoning clean
"""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

_FORBID = ConfigDict(extra="forbid")


class PlatformAdminRole(StrEnum):
    PLATFORM_ADMIN = "platform_admin"
    # Reserved for post-GA splits (no migration today):
    #   PLATFORM_SUPPORT  — read-mostly scope, no destructive ops
    #   PLATFORM_BILLING  — invoice + plan-change scope only


class PlatformAdmin(BaseModel):
    """Read shape returned by the admin API to the admin frontend.

    The frontend never receives more than this — there are no tokens or
    secrets on a platform_admin row. NextAuth holds the session JWT
    independently.
    """

    model_config = _FORBID

    id: UUID
    email: str = Field(..., min_length=3, max_length=254)
    display_name: str = Field(..., min_length=1, max_length=120)
    role: PlatformAdminRole
    active: bool = Field(
        ...,
        description=(
            "Soft-deactivate (FALSE) on departure so audit FKs survive. "
            "INSERT sets TRUE; UPDATE flips to FALSE."
        ),
    )
    last_login_at: str | None = Field(
        None, description="RFC3339 UTC; NULL before first login."
    )
    last_login_ip: str | None = Field(
        None,
        description=(
            "IP captured on last successful magic-link consumption — used "
            "as the bound IP for the IP-bind callback per ADR-030 "
            "Am.2026-05-20."
        ),
    )
    created_at: str
    updated_at: str


__all__ = [
    "PlatformAdmin",
    "PlatformAdminRole",
]
