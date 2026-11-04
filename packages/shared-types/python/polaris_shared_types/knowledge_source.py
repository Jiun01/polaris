"""Knowledge-source Pydantic SSOT — reconciler redesign.

Two shapes:

  * ``KnowledgeSourceCreate`` — what the tenant POSTs to
    ``/api/tenant/knowledge/sources``. Declares a desired knowledge source;
    the reconciler worker reads this ledger and syncs chunks accordingly.

  * ``KnowledgeSourceResponse`` — what the API returns for GET + POST.
    Exposes status, chunk_count, and last_synced_at so the portal can
    render a live reconciliation state column per source row.

The ``source_type`` discriminator + per-shape validator mirrors the
``knowledge_sources_source_shape`` CHECK in migration 118: exactly one
of ``source_url`` / ``storage_path`` / ``source_text`` must be set, and
the source_text body cap (100 KiB) mirrors the SQL.

The ``desired_hash`` field carries the caller-computed hash of the
source's *input*:
  * url:  SHA-256(url string)
  * file: SHA-256(file bytes) — computed browser-side before upload
  * text: SHA-256(source_text body)

The reconciler compares ``desired_hash`` vs ``last_synced_hash`` to
classify each source as add / changed / unchanged — skipping the crawl /
embed / write cycle for unchanged sources.

``desired_hash`` and ``source_text`` body are intentionally omitted from
``KnowledgeSourceResponse`` — they are internal reconciler state and
potentially large; the portal only needs the label, status, and counts.

Per ADR-057 the desktop worker is OUT of this monorepo — it reads this
package's schema from the shared-types JSON Schema export.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

# 100 KiB body cap mirrors migration 118's
# `length(source_text) <= 102400` CHECK and the ingestion_requests CHECK
# in 018 so the API rejects oversized inline-text at parse time.
_SOURCE_TEXT_MAX: int = 102_400

KnowledgeSourceStatus = Literal["pending", "syncing", "synced", "failed"]
KnowledgeSourceType = Literal["url", "file", "text"]


class KnowledgeSourceCreate(BaseModel):
    """Tenant-facing request body for ``POST /api/tenant/knowledge/sources``.

    Pydantic-side shape validation parallels the SQL CHECK in migration 118
    so callers see a clean 422 instead of a 500 + integrity-error round-trip.

    ``desired_hash`` is caller-supplied:
      * url  → SHA-256(url string)  — stable as long as the URL is unchanged
      * file → SHA-256(file bytes)  — computed browser-side before the sign step
      * text → SHA-256(source_text) — computed from the body text

    The reconciler uses this to skip unchanged sources on every Sync run.
    """

    source_type: KnowledgeSourceType
    source_url: str | None = None
    storage_path: str | None = None
    source_text: str | None = Field(default=None, max_length=_SOURCE_TEXT_MAX)
    source_label: str = Field(min_length=1, max_length=500)
    desired_hash: str = Field(min_length=1, max_length=128)
    tenant_branch_id: UUID | None = None

    @model_validator(mode="after")
    def _exactly_one_source(self) -> KnowledgeSourceCreate:
        """Mirror of migration 118's source-shape CHECK at parse time.

        Raises ValueError (→ 422) on mismatch so callers get the field
        name rather than a 500 from a DB integrity-constraint violation.
        """
        match self.source_type:
            case "url":
                if self.source_url is None:
                    raise ValueError(
                        "source_url is required when source_type='url'"
                    )
                if self.storage_path is not None or self.source_text is not None:
                    raise ValueError(
                        "source_type='url' rejects storage_path and source_text"
                    )
            case "file":
                if self.storage_path is None:
                    raise ValueError(
                        "storage_path is required when source_type='file'"
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
                if self.source_url is not None or self.storage_path is not None:
                    raise ValueError(
                        "source_type='text' rejects source_url and storage_path"
                    )
        return self


class KnowledgeSourceResponse(BaseModel):
    """Tenant-facing response for POST/GET on ``/api/tenant/knowledge/sources``.

    Intentionally lean: ``desired_hash`` and ``source_text`` body are
    omitted — they are reconciler-internal and potentially large.
    ``source_url`` and ``storage_path`` are included so the portal can
    display the source location. ``status`` + ``chunk_count`` +
    ``last_synced_at`` power the reconciliation state column.
    """

    id: UUID
    tenant_id: UUID
    tenant_branch_id: UUID | None
    source_type: KnowledgeSourceType
    source_url: str | None
    storage_path: str | None
    source_label: str
    status: KnowledgeSourceStatus
    chunk_count: int
    last_synced_at: datetime | None
    created_at: datetime
    updated_at: datetime
