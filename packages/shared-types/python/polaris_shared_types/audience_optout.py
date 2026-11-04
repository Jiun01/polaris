"""Audience opt-out + CSV import Pydantic SSOT (Phase 6 PR-1).

Two surfaces live here:

  1. **Opt-out viewer** — GA-blocking PDPA compliance per
     02_SCOPE_FEATURES_LIMITATIONS.md §6. Surfaces at
     ``GET /api/tenant/audience/opted-out`` (portal JWT) and the lone
     ``GET /api/v1/audience/opted-out`` GA exception (portal-JWT-auth at
     GA per ADR-035 Amendment evening; tenant API keys are v3.0).

  2. **CSV importer** — ``POST /api/tenant/audience/import`` accepts a
     multipart CSV upload; the importer (``services/audience/importer``)
     normalises ``phone`` to E.164 via libphonenumber against the
     tenant's ``default_country_code`` and accumulates per-line errors
     instead of first-failure-abort. The shape here is the parsed-row
     contract; raw CSV cells are converted into ``CSVImportRow``
     instances inside the importer.

``OptOutReason`` is the same set the audience_members.opted_out_reason
column CHECK enforces. Drift between this Literal and the column CHECK
is the bug class that the model_used incident taught us to watch for.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

_FORBID = ConfigDict(extra="forbid")

OptOutReason = Literal["stop_keyword", "manual", "dsar_delete"]
"""Why a member is opted out.

  - ``stop_keyword``  — STOP-keyword handler (ADR-010): customer texted
                        the localised "STOP BOT REPLY" phrase on inbound.
  - ``manual``        — tenant_admin marked them opted out via portal /
                        admin tooling.
  - ``dsar_delete``   — DSAR delete cascade (Phase 9) zeroed contact
                        information and flipped them out.

Aligned with audience_members.opted_out_reason CHECK in migration 009.
"""


# ---------------------------------------------------------------------
# Opt-out viewer
# ---------------------------------------------------------------------


class OptOutViewerRow(BaseModel):
    """One row in the opt-out viewer.

    Carries the three PDPA-required columns (opted_out_at,
    opted_out_reason, opted_out_at_channel) plus enough identifying
    info that a human agent can confirm "yes this is the right person
    to NOT re-engage". phone_number_e164 is intentionally kept — under
    PDPA, an opt-out list that hides the contact identifier is useless
    for downstream agents checking before manual outreach.
    """

    model_config = _FORBID

    id: UUID
    channel: str
    channel_user_id: str
    channel_user_name: str | None
    phone_number_e164: str | None
    locale: str | None
    opted_out_at: datetime
    opted_out_reason: OptOutReason
    opted_out_at_channel: str | None


class OptOutViewerPage(BaseModel):
    """Paginated opt-out viewer response.

    Cursor-based pagination (opaque base64) so a large tenant's opt-out
    list paginates stably even as new rows arrive. ``total_count`` is
    a SELECT COUNT(*) under the same RLS-scoped WHERE — cheap because
    the audience_members.opted_out_at partial index makes it an
    index-only scan.
    """

    model_config = _FORBID

    rows: list[OptOutViewerRow]
    next_cursor: str | None
    total_count: int


# ---------------------------------------------------------------------
# CSV import
# ---------------------------------------------------------------------


class CSVImportRow(BaseModel):
    """Parsed row from the audience CSV importer.

    ``phone`` is the raw value as it appeared in the CSV cell; the
    importer normalises to E.164 via ``phonenumbers.format_number`` and
    rejects unparseable rows with ``INVALID_PHONE`` accumulated into
    the result.

    ``tags`` accepts a semicolon-separated string in the CSV cell
    (matches v1's importer for backward-compat) and the importer
    splits it before constructing this model. ``metadata`` accepts a
    JSON-string cell which the importer parses; bad JSON surfaces as
    ``MALFORMED_METADATA_JSON``.

    ``consent_at`` is optional; if absent the importer writes
    opted_in_at=now() AND emits an audit row with
    outcome='consent_not_provided' for compliance review (per 04g §9
    gotchas). Don't fail the import — fail at PDPA review.
    """

    model_config = _FORBID

    phone: str
    name: str | None = None
    channel: Literal["whatsapp", "messenger"]
    branch: str | None = None
    locale: str | None = None
    tags: list[str] = Field(default_factory=list, max_length=20)
    consent_at: datetime | None = None
    metadata: dict[str, object] = Field(default_factory=dict)


class CSVImportErrorRow(BaseModel):
    """One error from the importer's per-line error accumulator."""

    model_config = _FORBID

    # row_number=0 is the documented sentinel for a "global file error"
    # (e.g. UTF-8 decode failure where no per-row position exists). All
    # real per-row errors carry the 1-based row index from the CSV.
    row_number: int = Field(ge=0)
    raw: dict[str, object]
    error_code: Literal[
        "INVALID_PHONE",
        "INVALID_CHANNEL",
        "INVALID_LOCALE",
        "CONSENT_REQUIRED_MISSING",
        "DUPLICATE_IN_FILE",
        "BRANCH_NOT_FOUND",
        "MALFORMED_TAGS",
        "MALFORMED_METADATA_JSON",
    ]
    message: str


class CSVImportResult(BaseModel):
    """Aggregate result of a CSV import attempt.

    ``imported_count`` covers both new inserts and updates to existing
    active rows (re-import of an already-opted-in contact merges
    locale + tags). ``skipped_count`` covers rows that were valid but
    no-op'd (e.g. duplicate-in-file, or already-opted-out contacts the
    importer chose not to re-activate). ``error_rows`` carries the
    per-line accumulator — every row that didn't apply is here, with
    the row_number from the source CSV preserved for tenant debugging.
    """

    model_config = _FORBID

    imported_count: int = Field(ge=0)
    skipped_count: int = Field(ge=0)
    error_rows: list[CSVImportErrorRow] = Field(default_factory=list)


__all__ = [
    "CSVImportErrorRow",
    "CSVImportResult",
    "CSVImportRow",
    "OptOutReason",
    "OptOutViewerPage",
    "OptOutViewerRow",
]
