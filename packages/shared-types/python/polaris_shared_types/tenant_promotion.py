"""Tenant promotion + branch DTOs — Pydantic SSOT (Phase 8 PR-9).

Two surfaces:

  * ``TenantPromotion`` — one row in `tenant_promotions` (migration 053).
    Universal across tiers per ADR-042 — every plan tier sees + edits
    promotions identically. Optional ``tenant_branch_id`` so a promo can
    be scoped to one location (NULL = applies tenant-wide).

  * ``TenantBranch`` — one row in `tenant_branches`. Pro+ tier-gated
    UI-side per ADR-041 Amendment; backend RLS is uniform.

Both shapes use ``ConfigDict(extra='forbid')`` so a future column on
either table fails the shared-types contract loudly instead of silently
disappearing from the portal payload.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

_FORBID = ConfigDict(extra="forbid")


class TenantPromotion(BaseModel):
    """Row shape for `tenant_promotions`."""

    model_config = _FORBID

    id: UUID
    tenant_id: UUID
    tenant_branch_id: UUID | None
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=2000)
    discount_text: str = Field(min_length=1, max_length=100)
    terms: str | None = Field(default=None, max_length=4000)
    valid_from: datetime
    valid_until: datetime
    is_active: bool
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def _valid_range(self) -> TenantPromotion:
        if self.valid_until <= self.valid_from:
            raise ValueError("valid_until must be strictly after valid_from")
        return self


class TenantPromotionUpsert(BaseModel):
    """Body shape for create + update. ``tenant_branch_id`` may be
    explicitly set to None to clear the branch scope on update.

    ``is_active`` defaults to True so a tenant creating a new promo
    sees it immediately on the bot's lookup; pausing requires an
    explicit toggle off.
    """

    model_config = _FORBID

    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=2000)
    discount_text: str = Field(min_length=1, max_length=100)
    terms: str | None = Field(default=None, max_length=4000)
    valid_from: datetime
    valid_until: datetime
    is_active: bool = True
    tenant_branch_id: UUID | None = None

    @model_validator(mode="after")
    def _valid_range(self) -> TenantPromotionUpsert:
        if self.valid_until <= self.valid_from:
            raise ValueError("valid_until must be strictly after valid_from")
        return self


class TenantBranch(BaseModel):
    """Row shape for `tenant_branches`.

    Beyond ``name``, the operational columns are surfaced so the portal can edit
    them: per-branch ``business_hours`` (same shape as ``tenants.business_hours``,
    NULL = inherit tenant default), handoff-message overrides (ADR-019), and the
    EA provider/service routing (migration 080). ``slug`` is read-only.
    """

    model_config = _FORBID

    id: UUID
    tenant_id: UUID
    name: str = Field(min_length=1, max_length=100)
    slug: str | None = None
    business_hours: dict[str, Any] | None = None
    handoff_message_in_hours: str | None = Field(default=None, max_length=2000)
    handoff_message_after_hours: str | None = Field(default=None, max_length=2000)
    ea_provider_id: int | None = Field(default=None, ge=1)
    ea_default_service_id: int | None = Field(default=None, ge=1)
    created_at: datetime


class TenantBranchUpsert(BaseModel):
    """Body shape for create + update.

    ``name`` is required. The operational fields are optional; on UPDATE a
    null/omitted field leaves the stored value unchanged (COALESCE semantics),
    so editing hours never clears the provisioning-written ``ea_*`` ids. ``slug``
    is read-only (auto-managed at provisioning) and not settable here.
    """

    model_config = _FORBID

    name: str = Field(min_length=1, max_length=100)
    business_hours: dict[str, Any] | None = None
    handoff_message_in_hours: str | None = Field(default=None, max_length=2000)
    handoff_message_after_hours: str | None = Field(default=None, max_length=2000)
    ea_provider_id: int | None = Field(default=None, ge=1)
    ea_default_service_id: int | None = Field(default=None, ge=1)
