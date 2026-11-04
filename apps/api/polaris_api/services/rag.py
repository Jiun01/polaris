"""RAG service — Phase 4 PR-2.

Tier-aware wrapper around ``services/knowledge_search.py``. Adds two
concerns that don't belong in the SQL-layer module:

  1. **Cohere rerank tier gate** per ADR-026 Amendment 5th. Ultra +
     Enterprise tenants get hybrid-25 → Cohere rerank → top-5 with
     relevance ≥ 0.3. Starter + Pro skip Cohere entirely. Per-tenant
     ``tenants.cohere_rerank_enabled`` overrides the tier default
     (TRUE forces rerank, FALSE forces off, NULL = tier default).

  2. **Empty-result vector fallback**. The hybrid RPC handles
     PostgresError-class failures internally (BM25 parser errors fall
     to vector-only inside ``knowledge_search.search_hybrid``). But
     when hybrid runs cleanly and just returns zero rows (no BM25
     match AND no vector hit above the SQL-side threshold), this
     module re-queries vector-only — defending against the case where
     a typo in the user message scrambles the BM25 path but the
     vector path would have hit. Logged as ``rag.fallback_to_vector_only``.

The Cohere client is lazy + placeholder-key-guarded. Calling rerank
with a ``placeholder-*`` key raises ``CohereDisabledError`` BEFORE the
SDK opens a socket — Phase 4 dogfood runs Starter-tier paths only
(per plan decision-lock 2), so the placeholder-key state is the
expected default until the first Ultra+ tenant signs up.

This module is the only place ``cohere`` is imported in the Polaris
codebase. Future changes to rerank model / threshold / top_n shape
land here.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from functools import lru_cache
from typing import Final, Literal
from uuid import UUID

import asyncpg
import cohere
import httpx
import redis.asyncio as redis_async
from cohere.core import ApiError as CohereApiError
from polaris_shared_types import KnowledgeChunkHit

from polaris_api import metrics
from polaris_api.config import get_settings
from polaris_api.services import knowledge_search

logger = logging.getLogger(__name__)

# ADR-026 Amendment 5th: Ultra/Enterprise default to Cohere rerank.
# Starter/Pro never call Cohere unless cohere_rerank_enabled=TRUE
# override flips them on.
_RERANK_DEFAULT_TIERS: Final[frozenset[str]] = frozenset({"ultra", "enterprise"})

# ADR-032 Amendment 2026-05-15: top_k=5 base. Ultra+ fetch top_k=25
# from hybrid → Cohere rerank → take top 5 with score >= 0.3.
_RERANK_FETCH_K: Final[int] = 25
_RERANK_THRESHOLD: Final[float] = 0.3

# Multilingual Cohere model (EN + Bahasa Malaysia + Simplified Chinese
# per ADR-031 language split). cohere-python 5.x exposes this via
# `AsyncClient.rerank(model=...)`. Per deep-dive §1.2 doc lookup
# 2026-05-18: rerank-v3.5 is the current multilingual default;
# rerank-v4.0-pro adds quality at higher cost but isn't needed at
# Phase 4 dogfood scale.
_COHERE_MODEL: Final[str] = "rerank-v3.5"

# Redis TTL on the per-tenant tier cache. Plan-tier changes happen via
# Stripe webhook (Phase 9). audit Batch B (B5): lengthened 60 -> 300s — a
# 5-minute plan-change lag is an accepted tradeoff for the reduced per-
# message tenant reads (no invalidation hooks built); a much longer cache
# would surface as "we paid for Ultra but Cohere still off" complaints.
_TIER_CACHE_TTL_SECONDS: Final[int] = 300
_TIER_CACHE_PREFIX: Final[str] = "tier:"

PlanTier = Literal["starter", "pro", "ultra", "enterprise"]


class CohereDisabledError(RuntimeError):
    """Raised when ``services/rag.py`` tries to rerank but the Cohere
    client is unavailable (placeholder key, missing env, etc.).

    Surfacing as a typed exception (not a bare ValueError) means tests
    can assert this specific guard fires, and the pipeline orchestrator
    (PR-4) can downgrade-to-no-rerank instead of failing the whole
    reply if it ever leaks through to a real request.
    """


@dataclass(slots=True)
class TenantTierInfo:
    """Snapshot of one tenant's rerank-relevant settings.

    JSON-serialized into Redis under ``tier:{tenant_id}`` for 300s (audit
    Batch B / B5). Plain dataclass (not Pydantic) so the Redis serialize path is just
    ``json.dumps(asdict(info))`` — no pydantic_core overhead in the
    hot RAG path.
    """
    plan_tier: PlanTier
    cohere_rerank_enabled: bool | None

    def to_json(self) -> str:
        return json.dumps({
            "plan_tier": self.plan_tier,
            "cohere_rerank_enabled": self.cohere_rerank_enabled,
        })

    @classmethod
    def from_json(cls, payload: str) -> TenantTierInfo:
        data = json.loads(payload)
        return cls(
            plan_tier=data["plan_tier"],
            cohere_rerank_enabled=data["cohere_rerank_enabled"],
        )


# ----------------------------------------------------------------------
# Cohere client factory — lazy, placeholder-guarded, test-monkeypatchable
# ----------------------------------------------------------------------


@lru_cache(maxsize=1)
def _get_cohere_client() -> cohere.AsyncClient | None:
    """Lazy Cohere async client; returns None on placeholder key.

    Returning None (rather than raising) lets the call site decide:
    ``_cohere_rerank`` raises ``CohereDisabledError`` on None, while a
    future health-check endpoint could surface "rerank disabled" as a
    benign status rather than a hard fault. Tests monkeypatch this
    factory after calling ``_reset_clients_for_testing``.
    """
    settings = get_settings()
    key = settings.cohere_api_key
    if not key or key.startswith("placeholder-"):
        return None
    return cohere.AsyncClient(api_key=key)


def _reset_clients_for_testing() -> None:
    """Clear the Cohere client cache. Used by tests before monkeypatch.

    Mirrors ``services/llm.py::_reset_clients_for_testing`` shape +
    defensive against a fixture having already swapped the factory
    for a plain function (no ``cache_clear`` attr).
    """
    clear = getattr(_get_cohere_client, "cache_clear", None)
    if clear is not None:
        clear()


# ----------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------


async def search_hybrid(
    *,
    conn: asyncpg.Connection,
    tenant_id: UUID,
    query_text: str,
    query_embedding: list[float],
    tenant_branch_id: UUID | None = None,
    top_k: int = 5,
    redis: redis_async.Redis | None = None,
    tier_info: TenantTierInfo | None = None,
) -> list[KnowledgeChunkHit]:
    """Tier-aware hybrid RAG search with optional Cohere rerank.

    Assumes the caller has already set ``app.current_tenant_id`` GUC
    on the connection (i.e. invoked under a TenantContext Depends).
    RLS strips other tenants' rows even if a tenant_id arg were
    spoofed.

    Tier matrix per ADR-026 Amendment 5th + ADR-032 Amendment 2026-05-15:

    | plan_tier   | cohere_rerank_enabled | Behavior                       |
    |-------------|-----------------------|--------------------------------|
    | starter/pro | NULL or FALSE         | hybrid-5 (no rerank)           |
    | starter/pro | TRUE (override)       | hybrid-25 → rerank-5           |
    | ultra/ent.  | NULL or TRUE          | hybrid-25 → rerank-5           |
    | ultra/ent.  | FALSE (override)      | hybrid-5 (no rerank)           |

    Empty-result fallback: if hybrid returns 0 rows AND the tenant has
    any knowledge_chunks at all, re-queries via ``search_vector_only``.
    Defensive against a BM25 path scrambling the result on typo-heavy
    queries that the vector path would have caught.

    ``redis`` (optional) enables the 300s per-tenant tier cache (audit Batch
    B / B5 — lengthened from 60s). PR-3+ will always pass it; standalone
    tests skip it.

    ``tier_info`` (audit Batch B / B1): when the caller already knows the
    tenant's tier (e.g. from the consolidated tenant snapshot the classic
    pipeline path loads once per message), pass it here to skip BOTH the
    Redis tier-cache lookup and the `tenants` SELECT this function would
    otherwise issue. Agent/tool callers (``RAGAdapter``) intentionally do
    NOT pass this — they keep the Redis-cached tier lookup since they don't
    already have a snapshot in hand.
    """
    resolved_tier_info = tier_info or await _get_tenant_tier(conn, tenant_id, redis=redis)
    use_rerank = _should_rerank(resolved_tier_info)

    if use_rerank:
        candidates = await knowledge_search.search_hybrid(
            conn, tenant_id, query_text, query_embedding,
            tenant_branch_id=tenant_branch_id, top_k=_RERANK_FETCH_K,
        )
        try:
            reranked = await _cohere_rerank(
                query=query_text, chunks=candidates,
                top_n=top_k, threshold=_RERANK_THRESHOLD,
            )
        except CohereDisabledError:
            # The tier gate said "rerank", but the Cohere client is
            # unavailable (key unset / still a placeholder). Degrade to the
            # plain hybrid-top_k result instead of failing the whole reply —
            # this is the downgrade-to-no-rerank the module docstring promises.
            # An Ultra/Enterprise tenant (or a cohere_rerank_enabled=TRUE
            # override) must NOT lose every bot reply just because no Cohere
            # key is provisioned yet. Logged so the missing key is visible.
            logger.warning(
                "rag.rerank_disabled_degrade tenant_id=%s — COHERE_API_KEY "
                "unset/placeholder; serving un-reranked hybrid-%d",
                tenant_id, top_k,
            )
            return candidates[:top_k]
        except (CohereApiError, httpx.TransportError) as exc:
            # Real network/API failure (timeout, 5xx, connection reset) — NOT
            # a config/disabled state. Same degrade as CohereDisabledError:
            # an Ultra/Enterprise tenant must not lose the reply because
            # Cohere had a blip. Distinct metric + log line so this is
            # distinguishable from the "never configured" case on the
            # cost/reliability dashboard.
            logger.warning(
                "rag.rerank_network_degrade tenant_id=%s err=%s — Cohere "
                "network/API failure; serving un-reranked hybrid-%d",
                tenant_id, type(exc).__name__, top_k,
            )
            metrics.record_cohere_rerank_degrade(
                tenant_id=tenant_id, reason=type(exc).__name__
            )
            return candidates[:top_k]
        # One billable Cohere call per successful rerank — record it for the
        # cost dashboard. Guarded on candidates because _cohere_rerank short-
        # circuits (no API call) on an empty candidate set; recorded after the
        # call so a CohereDisabledError isn't counted.
        if candidates:
            metrics.record_cohere_rerank(tenant_id=tenant_id)
        # Empty-result fallback: if every candidate scored below the rerank
        # threshold, ``reranked`` is empty even though the tenant has matching
        # chunks. Serve the un-reranked hybrid top_k rather than returning
        # nothing — mirrors the CohereDisabledError degrade above. Without
        # this, an Ultra/Enterprise tenant gets a context-free reply on any
        # query whose candidates all rank low (e.g. a cross-language question).
        if not reranked and candidates:
            logger.info(
                "rag.rerank_all_below_threshold tenant_id=%s — every candidate "
                "scored below %.2f; serving un-reranked hybrid-%d",
                tenant_id, _RERANK_THRESHOLD, top_k,
            )
            return candidates[:top_k]
        return reranked

    chunks = await knowledge_search.search_hybrid(
        conn, tenant_id, query_text, query_embedding,
        tenant_branch_id=tenant_branch_id, top_k=top_k,
    )

    if not chunks and await _tenant_has_any_chunks(conn, tenant_id):
        logger.info(
            "rag.fallback_to_vector_only tenant_id=%s query_len=%d",
            tenant_id, len(query_text),
        )
        chunks = await knowledge_search.search_vector_only(
            conn, tenant_id, query_embedding,
            tenant_branch_id=tenant_branch_id, top_k=top_k,
        )

    return chunks


# ----------------------------------------------------------------------
# Internal helpers
# ----------------------------------------------------------------------


def _should_rerank(tier: TenantTierInfo) -> bool:
    """Per-tenant override beats tier default.

    Tristate logic on ``cohere_rerank_enabled``:
      - TRUE  → always rerank (Starter with override gets paid quality)
      - FALSE → never rerank (Ultra with override saves cost; a test-
                tenant the operator wants to keep cheap)
      - NULL  → tier default (Ultra/Enterprise → rerank; else off)
    """
    if tier.cohere_rerank_enabled is True:
        return True
    if tier.cohere_rerank_enabled is False:
        return False
    return tier.plan_tier in _RERANK_DEFAULT_TIERS


async def _get_tenant_tier(
    conn: asyncpg.Connection,
    tenant_id: UUID,
    *,
    redis: redis_async.Redis | None,
) -> TenantTierInfo:
    """Read tenant tier info, with optional 300s Redis cache (audit Batch B / B5).

    Cache miss → SELECT from tenants (RLS-protected, so a forgotten
    SET LOCAL on the connection would surface as a zero-row read,
    not a leak). Defensive: an unknown tenant row defaults to
    Starter + no override — keeps the surface clean if a malformed
    tenant_id reaches this path (e.g. via a deleted demo tenant
    whose conversation is being replayed).
    """
    cache_key = f"{_TIER_CACHE_PREFIX}{tenant_id}"

    if redis is not None:
        cached = await redis.get(cache_key)
        if cached is not None:
            payload = cached.decode("utf-8") if isinstance(cached, bytes) else cached
            return TenantTierInfo.from_json(payload)

    row = await conn.fetchrow(
        "SELECT plan_tier, cohere_rerank_enabled FROM tenants WHERE id = $1",
        tenant_id,
    )
    if row is None:
        info = TenantTierInfo(plan_tier="starter", cohere_rerank_enabled=None)
    else:
        info = TenantTierInfo(
            plan_tier=row["plan_tier"],
            cohere_rerank_enabled=row["cohere_rerank_enabled"],
        )

    if redis is not None:
        await redis.set(cache_key, info.to_json(), ex=_TIER_CACHE_TTL_SECONDS)
    return info


async def _tenant_has_any_chunks(
    conn: asyncpg.Connection,
    tenant_id: UUID,
) -> bool:
    """EXISTS check on knowledge_chunks for the empty-result fallback.

    Cheaper than COUNT(*) on a large table — Postgres can stop at the
    first match. RLS-protected; an empty result here means the tenant
    genuinely has no chunks, in which case there's nothing to fall
    back to.
    """
    row = await conn.fetchrow(
        "SELECT EXISTS (SELECT 1 FROM knowledge_chunks WHERE tenant_id = $1) AS has",
        tenant_id,
    )
    return bool(row["has"]) if row else False


async def _cohere_rerank(
    *,
    query: str,
    chunks: list[KnowledgeChunkHit],
    top_n: int,
    threshold: float,
) -> list[KnowledgeChunkHit]:
    """Run Cohere rerank on candidate chunks; return survivors above threshold.

    Raises ``CohereDisabledError`` if the client is unavailable —
    placeholder key, missing env, etc. The tier-gate logic in
    ``search_hybrid`` is supposed to prevent this from being called
    in those states; the raise here is a defense-in-depth guard so a
    test seed or config typo surfaces loudly instead of silently
    leaking a request with bad credentials.
    """
    if not chunks:
        return []
    client = _get_cohere_client()
    if client is None:
        raise CohereDisabledError(
            "Cohere rerank requested but COHERE_API_KEY is unset or "
            "still a placeholder. Either provision a real key or "
            "ensure this tenant's plan_tier/override doesn't trigger "
            "rerank (Starter/Pro default to off per ADR-026 Amendment "
            "5th)."
        )
    documents = [{"text": c.content} for c in chunks]
    response = await client.rerank(
        query=query,
        documents=documents,
        model=_COHERE_MODEL,
        top_n=top_n,
        return_documents=False,
    )
    survivors: list[KnowledgeChunkHit] = []
    for result in response.results:
        if result.relevance_score >= threshold:
            survivors.append(chunks[result.index])
    return survivors[:top_n]
