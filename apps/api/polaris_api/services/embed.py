"""Query embedding — Phase 4 PR-6.

Calls OpenRouter's embeddings endpoint to produce a 1024-dim vector
for inbound user messages. Matches the desktop ingestion app's
embedding model + dimensions exactly (per
`Polaris Ingestion System V2/polaris_ingest/app/utils/embeddings.py`):

  - Model: ``qwen/qwen3-embedding-8b`` (default per ADR-059 — overridable
    via ``EMBEDDING_MODEL`` env)
  - Dimensions: ``1024`` via MRL truncation (matches
    ``knowledge_chunks.embedding VECTOR(1024)`` from migration 020)

Resolves Q-PHASE4-05 (deep-dive §11): run the same Qwen3 embedding
model as the desktop ingest, served via OpenRouter rather than
self-hosting it. Same key (``OPENROUTER_API_KEY``) we already use for
the LLM-fallback path — no new credential, no new dependency.

Failure handling: if OpenRouter is unreachable / returns an error /
``OPENROUTER_API_KEY`` is placeholder, returns a zero vector (logged
as warning). The vector contributes 0.0 to the RRF score on the
vector half of hybrid search — BM25 still drives ranking. Phase 9
will replace the warning with a circuit breaker once we have OTel
metrics to drive it.

Test isolation: ``_reset_client_for_testing`` clears the lru_cache
so tests can monkeypatch the factory after env tweaks.
"""

from __future__ import annotations

import logging
import math
from functools import lru_cache
from typing import TYPE_CHECKING, Final

import openai

from polaris_api import metrics
from polaris_api.config import get_settings

if TYPE_CHECKING:
    from uuid import UUID

logger = logging.getLogger(__name__)

# Dim must match migration 020's VECTOR(1024) — drift would surface
# as an asyncpg adapter error on the first search_hybrid call.
_EMBEDDING_DIM: Final[int] = 1024

# OpenRouter-compatible OpenAI client. Same pattern as services/llm.py
# fallback path; OpenRouter speaks the OpenAI chat-completions AND
# embeddings APIs.
_OPENROUTER_BASE_URL: Final[str] = "https://openrouter.ai/api/v1"

# Models that support MRL truncation via the ``dimensions`` parameter.
# Sending ``dimensions=`` to other models causes an OpenAI API error.
# Mirrors the desktop's MRL_SUPPORTED_MODELS list.
_MRL_SUPPORTED_MODELS: Final[frozenset[str]] = frozenset({
    "openai/text-embedding-3-small",
    "openai/text-embedding-3-large",
    "google/gemini-embedding-001",
    "qwen/qwen3-embedding-8b",
    "qwen/qwen3-embedding-4b",
    "qwen/qwen3-embedding-0.6b",
})


@lru_cache(maxsize=1)
def _get_embed_client() -> openai.AsyncOpenAI | None:
    """Lazy OpenAI client pointed at OpenRouter. Returns None on placeholder key.

    Mirrors ``services/llm._get_openrouter_client`` but kept separate
    so the embed path can fail-soft (zero vector + warning) while the
    LLM fallback path fails-hard (LLMConfigError).
    """
    settings = get_settings()
    key = settings.openrouter_api_key
    if not key or key.startswith("placeholder-"):
        return None
    return openai.AsyncOpenAI(api_key=key, base_url=_OPENROUTER_BASE_URL)


def _reset_client_for_testing() -> None:
    """Clear the cached client. Used by tests before monkeypatching."""
    clear = getattr(_get_embed_client, "cache_clear", None)
    if clear is not None:
        clear()


async def embed_query(text: str, *, tenant_id: UUID | None = None) -> list[float]:
    """Embed ``text`` for query-side hybrid search.

    Returns a 1024-dim list. On any failure (placeholder key, network,
    API error, wrong dim from a misconfigured model), logs a warning
    and returns a zero vector so the inbound pipeline still produces
    a reply via the BM25 half of hybrid search.

    ``tenant_id`` (optional) enables per-tenant cost telemetry: when
    provided, the OpenRouter embedding token count from a successful call
    is emitted on ``polaris_openrouter_embedding_tokens`` for the admin
    cost dashboard. Callers without a tenant context omit it.
    """
    if not text or not text.strip():
        return [0.0] * _EMBEDDING_DIM

    client = _get_embed_client()
    if client is None:
        _log_placeholder_warning_once()
        return [0.0] * _EMBEDDING_DIM

    settings = get_settings()
    model = settings.embedding_model
    kwargs: dict[str, object] = {"model": model, "input": text}
    if model in _MRL_SUPPORTED_MODELS:
        kwargs["dimensions"] = _EMBEDDING_DIM

    try:
        response = await client.embeddings.create(**kwargs)  # type: ignore[arg-type]
    except openai.APIError as exc:
        # Per code-review L-4: openai.APIConnectionError and
        # openai.APITimeoutError are both subclasses of openai.APIError,
        # so the single catch covers all transient failure modes.
        logger.warning(
            "embed_query: %s — returning zero vector",
            type(exc).__name__,
        )
        return [0.0] * _EMBEDDING_DIM

    # The call succeeded (and billed) — record tokens regardless of how the
    # downstream dim/empty checks resolve.
    if tenant_id is not None:
        tokens = int(getattr(getattr(response, "usage", None), "total_tokens", 0) or 0)
        metrics.record_openrouter_embedding(tenant_id=tenant_id, tokens=tokens)

    if not response.data:
        logger.warning("embed_query: empty response.data — returning zero vector")
        return [0.0] * _EMBEDDING_DIM

    embedding = list(response.data[0].embedding)
    if len(embedding) != _EMBEDDING_DIM:
        logger.error(
            "embed_query: dim mismatch (got %d, expected %d for model=%s). "
            "Falling back to zero vector — check EMBEDDING_MODEL is MRL-capable "
            "OR matches knowledge_chunks VECTOR(%d).",
            len(embedding), _EMBEDDING_DIM, model, _EMBEDDING_DIM,
        )
        return [0.0] * _EMBEDDING_DIM

    return embedding


async def embed_text(text: str, *, tenant_id: UUID | None = None) -> list[float]:
    """Embed arbitrary text (e.g. a product's ``name`` + ``description``) into
    the same 1024-dim space as :func:`embed_query`.

    Products are the *documents* searched by ``match_products_hybrid`` and
    inbound questions are the *queries*; both sides must share one vector
    space, so product text is embedded through the identical no-instruction
    path (Phase 15 PR-2). Fail-soft identical to ``embed_query`` (zero vector
    on provider failure — vector leg contributes 0.0, BM25 still ranks).
    ``tenant_id`` threads through for embedding cost telemetry.
    """
    return await embed_query(text, tenant_id=tenant_id)


def to_pgvector_literal(embedding: list[float]) -> str:
    """Format an embedding as a pgvector ``'[x,y,z]'`` string literal for a
    ``$n::vector`` bind.

    asyncpg does not adapt Python lists to the pgvector ``vector`` type
    without ``pgvector.asyncpg.register_vector(conn)`` at every pool acquire;
    the string literal + ``::vector`` cast at the call site keeps the pool
    registration-free (matches ``services/knowledge_search._vec_literal``).
    Non-finite values (NaN/±Inf) from a misbehaving provider are coerced to
    0.0 so the cast never rejects the literal.
    """
    return (
        "["
        + ",".join(
            repr(f if math.isfinite(f := float(v)) else 0.0) for v in embedding
        )
        + "]"
    )


# One-shot placeholder-key warning so the log doesn't spam under burst.
# Per code-review M-2: lru_cache replaces the prior module-level mutable
# bool. lru_cache is thread-safe under the GIL + naturally idempotent
# (second call returns the cached None without re-logging). Tests
# call `reset_warning_for_testing()` to re-arm.


@lru_cache(maxsize=1)
def _log_placeholder_warning_once() -> None:
    logger.warning(
        "embed_query: OPENROUTER_API_KEY placeholder — returning zero "
        "vector. RAG vector side contributes 0.0; BM25 still drives "
        "ranking. Set a real key in `.env` for full hybrid search."
    )


def reset_warning_for_testing() -> None:
    """Re-arm the one-shot warning so test ordering stays deterministic."""
    clear = getattr(_log_placeholder_warning_once, "cache_clear", None)
    if clear is not None:
        clear()
