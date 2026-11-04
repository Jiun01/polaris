"""Search-RPC result shape — Phase 3 PR-2.

``KnowledgeChunkHit`` is the row shape returned by both search RPCs:

  * ``match_knowledge_chunks_hybrid`` populates all four ranking
    fields (``rrf_score``, ``vector_rank``, ``bm25_rank``,
    ``distance=None``) — RRF score is the merged ranking signal.
  * ``match_knowledge_chunks`` populates only ``distance`` (cosine
    distance, lower = more similar); the service wrapper synthesises
    ``rrf_score = 1.0 - distance`` so Phase 4 RAG can sort either
    result set by the same field.

Phase 4 imports this shape via packages/shared-types; the desktop
project (Polaris Ingestion System, out of monorepo per ADR-057)
writes ``knowledge_chunks`` rows but does NOT consume this shape —
search is a server-side concern.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, Field


class KnowledgeChunkHit(BaseModel):
    """One row from ``match_knowledge_chunks`` / ``_hybrid``.

    Per-field semantics:
      * ``id`` — knowledge_chunks.id (UUID).
      * ``content`` — full chunk text (bounded by chunker; ADR-032
        default 512 tokens, per-tenant override via tenants.settings).
      * ``source_name`` — display label (URL, filename, etc.) for
        citation rendering in Phase 8 portal.
      * ``rrf_score`` — primary sort key, higher = better. Always
        populated.
      * ``vector_rank`` / ``bm25_rank`` — 1-based ranks within the
        candidate pool for each leg; ``None`` when the chunk only
        matched the OTHER leg (FULL OUTER JOIN in the hybrid RPC).
      * ``distance`` — populated by the vector-only fallback; ``None``
        from the hybrid RPC.
    """

    id: UUID
    content: str
    source_name: str
    rrf_score: float
    vector_rank: int | None = Field(default=None, ge=1)
    bm25_rank: int | None = Field(default=None, ge=1)
    distance: float | None = Field(default=None, ge=0.0, le=2.0)
