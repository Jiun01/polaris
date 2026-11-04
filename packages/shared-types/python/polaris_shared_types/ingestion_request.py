"""Ingestion-queue Pydantic SSOT — Phase 3 PR-1.

Three shapes:

  * ``IngestionRequestSubmit`` — what the tenant POSTs to
    ``/api/tenant/knowledge/requests``. Per-request overrides for crawl,
    OCR, and ingest mode live here; chunk-shape settings DON'T
    (chunk_size + chunk_overlap are tenant-level per ADR-032 Amendment +
    Q-PHASE3-15, read by the desktop via
    ``GET /api/admin/tenants/{id}/chunking-config``).

  * ``IngestionRequestResponse`` — what the API returns. Carries the
    submitted-at timestamp, an ``eta`` (24h SLA per PRD §6.2), and a
    ``status_url`` the portal can poll. The desktop owns status
    transitions; the API just surfaces them.

  * ``ChunkingConfig`` — desktop-only response from
    ``GET /api/admin/tenants/{id}/chunking-config``. Read by the desktop
    at run time so per-tenant chunk overrides land without a desktop
    redeploy.

The ``source_type`` discriminator + per-shape validation matches the
``ingestion_requests_source_shape`` CHECK in migration 018: exactly one
of ``source_url`` / ``storage_paths`` / ``source_text`` must be set, and
the source_text body cap (100 KiB) mirrors the SQL.

Per ADR-057 the desktop is OUT of this monorepo — it imports this
package via the published shared-types schema, not Python directly.
"""

from __future__ import annotations

from datetime import datetime
from typing import Final, Literal
from uuid import UUID

from pydantic import BaseModel, Field, HttpUrl, model_validator

SourceType = Literal["url", "file", "text"]
IngestionStatus = Literal[
    "pending", "processing", "partial", "completed", "failed", "cancelled"
]
IngestionMode = Literal["add", "sync", "replace"]


# 100 KiB body cap mirrors migration 018's
# `length(source_text) <= 102400` CHECK so the API rejects oversized
# inline-text submissions at parse time rather than at INSERT time.
_SOURCE_TEXT_MAX = 102_400


# Chunk-config defaults per ADR-032 Amendment. Single source of truth so
# admin_ingestion.py + the desktop project (Polaris Ingestion System, out
# of monorepo per ADR-057) agree on what a "no override" tenant sees.
# Postgres-side default for tenants.chunk_size / chunk_overlap columns
# in migration 002 (512 / 80) MUST agree with these; the SQL default is
# applied at row-insert time and isn't auto-derived from this Python
# constant, so they're a maintained pair — update both or neither.
DEFAULT_CHUNK_SIZE: Final[int] = 512
DEFAULT_CHUNK_OVERLAP: Final[int] = 80
DEFAULT_CONTEXTUAL_ENRICHMENT: Final[bool] = True


class IngestionRequestSubmit(BaseModel):
    """Tenant-facing request body for ``POST /api/tenant/knowledge/requests``.

    Pydantic-side shape validation parallels the SQL CHECK so callers
    see a clean 422 instead of a 500 + integrity-error round-trip.
    Field bounds match the desktop's safe operating envelope (crawl
    depth ≤ 10, pages ≤ 2000, OCR languages ≤ 4) so a runaway request
    can't burn an arbitrary amount of desktop CPU before its own caps
    kick in.
    """

    source_type: SourceType
    source_url: HttpUrl | None = None
    storage_paths: list[str] | None = Field(default=None, max_length=20)
    source_text: str | None = Field(default=None, max_length=_SOURCE_TEXT_MAX)
    ingest_mode: IngestionMode = "sync"
    ocr_enabled: bool = False
    # Q-PHASE3-10: ['ch_sim','en'] is the EasyOCR default at GA. Tenants
    # can shrink the list (single-lang content) but not arbitrarily
    # expand it — EasyOCR loads each model into memory.
    ocr_languages: list[str] = Field(
        default_factory=lambda: ["ch_sim", "en"], max_length=4
    )
    crawl_max_depth: int | None = Field(default=None, ge=1, le=10)
    crawl_max_pages: int | None = Field(default=None, ge=1, le=2_000)
    contextual_enrichment: bool | None = None
    tenant_branch_id: UUID | None = None

    @model_validator(mode="after")
    def _exactly_one_source(self) -> IngestionRequestSubmit:
        """Mirror of migration 018's source-shape CHECK at parse time.

        Faster + clearer than letting the INSERT trip the constraint —
        the tenant sees a 422 with the field name instead of a 500.
        """
        match self.source_type:
            case "url":
                if self.source_url is None:
                    raise ValueError("source_url is required when source_type='url'")
                if self.storage_paths or self.source_text is not None:
                    raise ValueError(
                        "source_type='url' rejects storage_paths and source_text"
                    )
            case "file":
                if not self.storage_paths:
                    raise ValueError(
                        "storage_paths must be a non-empty list when "
                        "source_type='file'"
                    )
                if self.source_url is not None or self.source_text is not None:
                    raise ValueError(
                        "source_type='file' rejects source_url and source_text"
                    )
            case "text":
                if self.source_text is None or not self.source_text.strip():
                    raise ValueError(
                        "source_text must be non-empty when source_type='text'"
                    )
                if self.source_url is not None or self.storage_paths:
                    raise ValueError(
                        "source_type='text' rejects source_url and storage_paths"
                    )
        return self


class IngestionRequestResponse(BaseModel):
    """Tenant-facing response for POST/GET on ``/api/tenant/knowledge/requests``.

    ``eta`` = ``submitted_at + 24h`` per PRD §6.2 SLA. ``status_url`` is
    a relative path the portal can poll without hard-coding the API host.
    Counts (``chunks_added`` / ``_skipped`` / ``_deleted``) are NULL
    until the desktop processes the row.
    """

    id: UUID
    tenant_id: UUID
    tenant_branch_id: UUID | None
    source_type: SourceType
    ingest_mode: IngestionMode
    status: IngestionStatus
    submitted_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    chunks_added: int | None
    chunks_skipped: int | None
    chunks_deleted: int | None
    tokens_used: int | None
    error: dict[str, object] | None
    eta: datetime
    status_url: str


class ChunkingConfig(BaseModel):
    """Per-tenant chunk shape — read by the desktop at run time.

    Returned from ``GET /api/admin/tenants/{tenant_id}/chunking-config``.
    Service-role JWT only (tenants manage these values via the Phase 8
    portal, NOT this endpoint). Defaults come from ADR-032 Amendment
    (chunk_size=512, overlap=80); per-tenant overrides land in
    ``tenants.settings`` JSONB per Q-PHASE3-15.
    """

    chunk_size: int = Field(ge=64, le=4_096)
    chunk_overlap: int = Field(ge=0, le=1_024)
    contextual_enrichment_default: bool

    @model_validator(mode="after")
    def _overlap_under_size(self) -> ChunkingConfig:
        """Overlap must leave at least one new token per chunk.

        Without this, the chunker would loop emitting identical chunks
        — defending here in the SSOT means a typo in
        ``tenants.settings`` surfaces at API-response time instead of
        as a runaway desktop loop.
        """
        if self.chunk_overlap >= self.chunk_size:
            raise ValueError(
                "chunk_overlap must be < chunk_size "
                f"({self.chunk_overlap} >= {self.chunk_size})"
            )
        return self
