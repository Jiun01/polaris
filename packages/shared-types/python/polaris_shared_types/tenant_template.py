"""Tenant WhatsApp template — Pydantic SSOT (Phase 6 PR-1).

Mirrors Meta's message-template wire format closely so that
``services/meta_whatsapp.submit_template`` can pass ``components`` through
to the Graph API with minimal transformation. The discriminated union on
``TemplateComponent.type`` (HEADER/BODY/FOOTER/BUTTONS) catches client
typos at the API boundary instead of letting them propagate to Meta's
``error_data.details`` blob (which is opaque enough that we'd surface a
useless error to the tenant).

Status enum + category enum are intentionally typed as ``Literal[...]``
strings so the database CHECK constraint in migration 041 and this
Pydantic shape stay textually aligned — drift between the two is the
class of bug that took us a Phase 4 incident to catch (ADR-031 Amendment
added 'gpt-4o-mini-fallback' to the literal but missed the column CHECK).

ConfigDict(extra='forbid') is set on every shape so an unknown field in
the POST body fails with 422 rather than being silently dropped — Phase 8
portal drift detection.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

_FORBID = ConfigDict(extra="forbid")

TemplateStatus = Literal["pending", "approved", "rejected", "paused", "disabled"]
"""Lifecycle status mirroring Meta's vocabulary.

Aligned with the CHECK constraint in migration 041:
  CHECK (status IN ('pending','approved','rejected','paused','disabled'))
"""

TemplateCategory = Literal["AUTHENTICATION", "MARKETING", "UTILITY"]
"""Meta template category. Uppercase to match Meta's wire enum exactly
so we can pass it through without a translation layer."""


class TemplateButton(BaseModel):
    """One button inside a BUTTONS component.

    Quick-reply buttons carry only ``text``. URL + phone-number buttons
    carry ``url`` / ``phone_number`` respectively. Meta enforces that
    each component holds at most 3 buttons; we don't repeat that here
    because the BUTTONS-component validator below applies the cap.
    """

    model_config = _FORBID

    type: Literal["QUICK_REPLY", "URL", "PHONE_NUMBER"]
    text: str = Field(min_length=1, max_length=25)
    url: str | None = None
    phone_number: str | None = None


class TemplateComponent(BaseModel):
    """One component of a Meta message template.

    Meta's wire shape is loosely typed (``components[]`` mixes HEADER
    with TEXT format, BODY with text, FOOTER with text, BUTTONS with
    nested buttons). We keep it loose here too — a tighter discriminated
    union on ``type`` would be cleaner but the cost is heavy when most
    templates only carry a BODY component anyway.
    """

    model_config = _FORBID

    type: Literal["HEADER", "BODY", "FOOTER", "BUTTONS"]
    format: Literal["TEXT", "IMAGE", "VIDEO", "DOCUMENT"] | None = None
    text: str | None = Field(default=None, max_length=1024)
    example: dict[str, object] | None = None
    buttons: list[TemplateButton] | None = Field(default=None, max_length=3)


class TenantTemplateSubmit(BaseModel):
    """Inbound POST /api/tenant/templates body.

    ``name`` matches Meta's accepted pattern (lowercase + underscores).
    ``language`` is a Meta locale code (en_US, ms_MY, zh_CN). We don't
    enum-restrict it because Meta's allowed set evolves; bad locales
    surface as 400s from Meta which we wrap into MetaTemplateError.
    """

    model_config = _FORBID

    name: str = Field(pattern=r"^[a-z0-9_]{1,512}$")
    language: str = Field(min_length=2, max_length=10)
    category: TemplateCategory
    components: list[TemplateComponent] = Field(min_length=1)


class TenantTemplateDTO(BaseModel):
    """Outbound GET /api/tenant/templates row.

    Includes every column the portal lifecycle UI needs:
    submission timestamps, current status, the meta_template_id (so the
    portal can deep-link into Meta's Business Manager for templates that
    were rejected or paused), and the rejection_reason text Meta returned.
    """

    model_config = _FORBID

    id: UUID
    tenant_id: UUID
    tenant_channel_id: UUID
    name: str
    language: str
    category: TemplateCategory
    status: TemplateStatus
    meta_template_id: str | None
    components: list[TemplateComponent]
    rejection_reason: str | None
    last_status_change_at: datetime
    submitted_at: datetime
    created_at: datetime
    updated_at: datetime


__all__ = [
    "TemplateButton",
    "TemplateCategory",
    "TemplateComponent",
    "TemplateStatus",
    "TenantTemplateDTO",
    "TenantTemplateSubmit",
]
