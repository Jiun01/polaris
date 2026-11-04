"""Agent tool-context ports + adapters (ADR-063).

The agent's tool-calling loop depends on a small hexagonal port surface —
``Ports`` container + a handful of Protocol-typed adapters — inherited from
the Phase 5 flow runtime (``flow_runtime/ports.py`` + ``adapters.py``), which
is now retired (audit Batch D). This module is the sole remaining owner of
that plumbing: the concrete adapters wire the ports to the service layer
(``services/llm.py``, ``services/rag.py``, ``services/products/search.py``,
``services/chatwoot/handoff.py``, ``services/conversation.py``); test fakes
implement the same protocols without touching Anthropic / Cohere / Chatwoot.

Trimmed from the original flow-runtime ports/adapters: ``MetaPort``,
``DBPort``, ``AuditPort`` and their adapters (``MetaAdapter``,
``NullMetaAdapter``, ``DBAdapter``, ``AuditAdapter``) are NOT carried over —
grep-confirmed zero agent-tool consumers of ``ports.meta`` / ``ports.db`` /
``ports.audit``. Those three ports existed only for flow-runtime's own
``flow_runs`` bookkeeping + WhatsApp template dispatch, which no longer
exists.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol, TypeVar
from uuid import UUID

import asyncpg
import redis.asyncio as redis_async
from polaris_shared_types.knowledge_chunk import KnowledgeChunkHit
from polaris_shared_types.llm_response import IntentTier, ResponseEnvelope
from polaris_shared_types.tenant_product import ProductHit

from polaris_api.services import conversation as conversation_service
from polaris_api.services import embed, llm, rag
from polaris_api.services.meta_whatsapp import MetaWhatsAppClient
from polaris_api.services.products import search as products_search
from polaris_api.services.promotions import lookup as promotions_lookup
from polaris_api.services.promotions.lookup import ActivePromotion

logger = logging.getLogger(__name__)

_PortT = TypeVar("_PortT")


# ---------------------------------------------------------------------------
# Port protocols
# ---------------------------------------------------------------------------


class LLMPort(Protocol):
    """Tier-aware LLM access. Wires to ``services/llm.py``.

    ``generate`` mirrors ``llm.generate_response``. The caller hands the
    adapter everything the LLM service needs to make one call; the adapter
    chooses Haiku vs Sonnet vs OpenRouter fallback based on ``intent``
    (tier-routing happens inside the service, not here — keeps model
    selection in one place).
    """

    async def classify(self, *, text: str, tenant_id: UUID) -> IntentTier: ...

    async def select_product_skus(
        self,
        *,
        user_message: str,
        candidates: list[dict[str, str]],
    ) -> list[str]:
        """Structured decision (Phase 15): of the retrieved product candidates
        (``[{"id","name","description"}]``), which ids' photos to show. Empty =
        send no images. Fail-safe to empty on any error."""
        ...

    async def generate(
        self,
        *,
        tenant_id: UUID,
        user_message: str,
        rag_context: str,
        rag_chunks: list[KnowledgeChunkHit],
        intent: IntentTier,
        conversation_history: list[dict[str, str]],
    ) -> ResponseEnvelope: ...


class RAGPort(Protocol):
    """Hybrid retrieval with optional Cohere rerank (tier-gated inside
    the adapter; the port just returns the final ranked list)."""

    async def search(
        self,
        *,
        query: str,
        tenant_id: UUID,
        top_k: int = 5,
    ) -> list[KnowledgeChunkHit]: ...


class ProductsPort(Protocol):
    """Product catalog retrieval for the bot (Phase 15).

    ``search_products`` returns ranked ``ProductHit``s (hybrid + vector
    fallback, ``is_active``-only). ``list_images`` returns ordered storage
    paths for the SKUs the LLM chose to show; the reply path turns those
    into short-TTL signed URLs for the per-channel image send.
    """

    async def search_products(
        self,
        *,
        query: str,
        tenant_id: UUID,
        top_k: int = 5,
    ) -> list[ProductHit]: ...

    async def list_images(
        self,
        *,
        product_ids: list[UUID],
    ) -> dict[UUID, list[str]]: ...


class PromotionsPort(Protocol):
    """Active-promotions retrieval for the bot (mirrors the classic path's
    ``services/promotions/lookup.fetch_active_promotions``).

    Returns the tenant's currently-valid promotions (``is_active`` AND now in
    ``[valid_from, valid_until]``). Branch scoping (NULL-safe: tenant-wide promos
    always match) is applied inside the adapter from the conversation's branch,
    so the tool doesn't need to know it."""

    async def fetch_active(self, *, tenant_id: UUID) -> list[ActivePromotion]: ...


class ChatwootPort(Protocol):
    """Minimal Chatwoot handoff surface — write a label + private
    note and ack.

    Returns True when Chatwoot acknowledges the write (becomes the
    ``confirmed`` outcome). Returns False on rejection (``declined`` —
    NOT an error). Raises only on transport failure.
    """

    async def handoff(
        self,
        *,
        conversation_id: UUID,
        team_id: str,
        private_note: str,
        tenant_id: UUID,
        customer_name: str | None = None,
        last_reply: str | None = None,
    ) -> bool: ...


class ConversationPort(Protocol):
    """Read conversation history for the current run's conversation.

    Surfaces the windowed-history read the reply path uses
    (``services/conversation.load_history`` — 20 messages OR 8k tokens per
    ADR-031).

    Returns the chronological list of ``{"role", "content"}`` dicts in the
    format the Anthropic SDK's ``messages`` parameter expects. The current
    inbound message is NOT included — the caller passes it separately.

    On conversation_id=None, the implementation returns an empty list (no
    conversation = no history).
    """

    async def load_history(
        self,
        *,
        tenant_id: UUID,
        conversation_id: UUID | None,
        max_messages: int = 20,
        max_tokens: int = 8000,
    ) -> list[dict[str, str]]: ...


class Ports:
    """Container threaded through agent tools. Holds one instance per port;
    tools pick whichever ports they actually need.

    A class (not a NamedTuple / dataclass) so adding a new port doesn't
    require updating every test fixture's constructor call — tests pass
    only the ports they care about as keyword args.
    """

    def __init__(
        self,
        *,
        llm: LLMPort | None = None,
        rag: RAGPort | None = None,
        products: ProductsPort | None = None,
        promotions: PromotionsPort | None = None,
        chatwoot: ChatwootPort | None = None,
        conversation: ConversationPort | None = None,
    ) -> None:
        self._llm = llm
        self._rag = rag
        self._products = products
        self._promotions = promotions
        self._chatwoot = chatwoot
        self._conversation = conversation

    @staticmethod
    def _require(value: _PortT | None, name: str) -> _PortT:
        """Raise if a port is missing — better to fail fast at the first
        tool that needs an unwired port than to default to a silent no-op
        that produces a spurious result."""
        if value is None:
            raise RuntimeError(f"{name}Port not wired on this Ports instance")
        return value

    @property
    def llm(self) -> LLMPort:
        return self._require(self._llm, "LLM")

    @property
    def rag(self) -> RAGPort:
        return self._require(self._rag, "RAG")

    @property
    def products(self) -> ProductsPort:
        return self._require(self._products, "Products")

    @property
    def promotions(self) -> PromotionsPort:
        return self._require(self._promotions, "Promotions")

    @property
    def chatwoot(self) -> ChatwootPort:
        return self._require(self._chatwoot, "Chatwoot")

    @property
    def conversation(self) -> ConversationPort:
        return self._require(self._conversation, "Conversation")


# ---------------------------------------------------------------------------
# Adapters
# ---------------------------------------------------------------------------


async def _embed_cached(
    query: str, tenant_id: UUID, cache: dict[str, list[float]] | None
) -> list[float]:
    """Embed ``query``, memoized in the shared per-turn ``embed_cache``.

    One implementation for the identical lookup/populate block
    ``RAGAdapter.search`` and ``ProductsAdapter.search_products`` both need
    (audit Batch B / B3 — see those adapters' docstrings for cache lifetime).
    """
    if cache is not None and query in cache:
        return cache[query]
    query_embedding = await embed.embed_query(query, tenant_id=tenant_id)
    if cache is not None:
        cache[query] = query_embedding
    return query_embedding


class LLMAdapter:
    """Wraps ``services/llm.py``. No per-request state — the service
    handles its own client + cache."""

    async def classify(self, *, text: str, tenant_id: UUID) -> IntentTier:
        # tenant_id threads through for per-tenant cost telemetry on the
        # classifier's Haiku call (intent_tier="classify").
        return await llm.classify_intent(text, tenant_id=tenant_id)

    async def select_product_skus(
        self,
        *,
        user_message: str,
        candidates: list[dict[str, str]],
    ) -> list[str]:
        return await llm.select_product_skus(user_message, candidates)

    async def generate(
        self,
        *,
        tenant_id: UUID,
        user_message: str,
        rag_context: str,
        rag_chunks: list[KnowledgeChunkHit],
        intent: IntentTier,
        conversation_history: list[dict[str, str]],
    ) -> ResponseEnvelope:
        return await llm.generate_response(
            tenant_id=tenant_id,
            rag_context=rag_context,
            conversation_history=conversation_history,
            user_message=user_message,
            intent=intent,
            chunks=rag_chunks or None,
        )


class RAGAdapter:
    """Wraps ``services/rag.search_hybrid``. Carries the asyncpg
    connection + redis pool because RAG needs both (RLS-scoped DB
    access + per-tenant tier cache).

    ``embed_cache`` (audit Batch B / B3): an optional dict shared with
    ``ProductsAdapter``, memoizing ``embed_query`` results per exact query
    string for the lifetime of ONE turn (adapters are built fresh per turn by
    ``build_ports``). Also memoizes ``search_hybrid`` results per
    ``(query, top_k)`` so a repeated ``search_knowledge`` tool call in the
    same tool loop skips embed + hybrid SQL + Cohere rerank entirely.
    """

    def __init__(
        self,
        *,
        conn: asyncpg.Connection,
        redis: redis_async.Redis | None = None,
        embed_cache: dict[str, list[float]] | None = None,
    ) -> None:
        self._conn = conn
        self._redis = redis
        self._embed_cache = embed_cache
        self._search_cache: dict[tuple[str, int], list[KnowledgeChunkHit]] = {}

    async def search(
        self,
        *,
        query: str,
        tenant_id: UUID,
        top_k: int = 5,
    ) -> list[KnowledgeChunkHit]:
        cache_key = (query, top_k)
        if cache_key in self._search_cache:
            return self._search_cache[cache_key]

        # Embedding generation happens here so the adapter can return the
        # chunks directly (compute embedding once, then search).
        query_embedding = await _embed_cached(query, tenant_id, self._embed_cache)

        hits = await rag.search_hybrid(
            conn=self._conn,
            tenant_id=tenant_id,
            query_text=query,
            query_embedding=query_embedding,
            top_k=top_k,
            redis=self._redis,
        )
        self._search_cache[cache_key] = hits
        return hits


class ProductsAdapter:
    """Wraps ``services/products/search.py``. Holds the per-request asyncpg
    connection (RLS-scoped, GUC already set by the pipeline) — same posture as
    ``RAGAdapter``. Embedding generation happens here so the caller gets
    ranked ``ProductHit``s back directly.

    ``embed_cache`` (audit Batch B / B3): same shared per-turn memo dict as
    ``RAGAdapter`` — a repeated query string (either the same tool called
    twice, or RAG + Products searching the same text) reuses the embedding
    instead of re-calling ``embed_query``.
    """

    def __init__(
        self,
        *,
        conn: asyncpg.Connection,
        embed_cache: dict[str, list[float]] | None = None,
        tenant_branch_id: UUID | None = None,
    ) -> None:
        self._conn = conn
        self._embed_cache = embed_cache
        # Branch of the current conversation (NULL-safe filter: tenant-wide
        # products always match; branch-bound products only for their branch).
        self._tenant_branch_id = tenant_branch_id

    async def search_products(
        self,
        *,
        query: str,
        tenant_id: UUID,
        top_k: int = 5,
    ) -> list[ProductHit]:
        query_embedding = await _embed_cached(query, tenant_id, self._embed_cache)
        return await products_search.search_products_hybrid(
            conn=self._conn,
            tenant_id=tenant_id,
            query_text=query,
            query_embedding=query_embedding,
            tenant_branch_id=self._tenant_branch_id,
            top_k=top_k,
        )

    async def list_images(
        self,
        *,
        product_ids: list[UUID],
    ) -> dict[UUID, list[str]]:
        return await products_search.fetch_images_for(self._conn, product_ids)


class PromotionsAdapter:
    """Wraps ``services/promotions/lookup.fetch_active_promotions``. Holds the
    per-request RLS-scoped connection + the conversation's branch, so branch-bound
    promotions only surface for their branch (NULL-safe: tenant-wide promos always
    match). Same connection posture as ``ProductsAdapter``."""

    def __init__(
        self,
        *,
        conn: asyncpg.Connection,
        tenant_branch_id: UUID | None = None,
    ) -> None:
        self._conn = conn
        self._tenant_branch_id = tenant_branch_id

    async def fetch_active(self, *, tenant_id: UUID) -> list[ActivePromotion]:
        return await promotions_lookup.fetch_active_promotions(
            self._conn,
            tenant_id,
            tenant_branch_id=self._tenant_branch_id,
        )


class ChatwootAdapter:
    """ChatwootPort impl — delegates to ``services/chatwoot/handoff.py``.

    Holds the per-request asyncpg connection + singleton HTTP clients and
    forwards the four port arguments plus enriched context to
    ``initiate_handoff``.

    Backward-compat construction: when ``conn`` / ``chatwoot_client`` /
    ``meta_client`` are all None (the no-args form used by tests), the
    adapter retains the declined-fallback behaviour.
    """

    def __init__(
        self,
        *,
        conn: asyncpg.Connection | None = None,
        chatwoot_client: Any = None,  # ChatwootClient (typed Any to avoid import cycle in fakes)
        meta_client: MetaWhatsAppClient | None = None,
    ) -> None:
        self._conn = conn
        self._chatwoot = chatwoot_client
        self._meta = meta_client

    async def handoff(
        self,
        *,
        conversation_id: UUID,
        team_id: str,
        private_note: str,
        tenant_id: UUID,
        customer_name: str | None = None,
        last_reply: str | None = None,
    ) -> bool:
        if self._conn is None or self._chatwoot is None or self._meta is None:
            # Test mode — no real outbound. The port contract treats
            # ``False`` as the ``declined`` outcome (NOT an error per
            # ADR-051).
            logger.debug(
                "ChatwootAdapter declined handoff (deps not wired): "
                "tenant_id=%s conversation_id=%s",
                tenant_id,
                conversation_id,
            )
            return False

        # Import here to avoid a circular dependency: agent/ports.py ->
        # chatwoot.handoff -> chatwoot.client (which is fine) but
        # chatwoot.handoff also reads services/secrets via a local import.
        # Keeping the top-level agent/ports.py imports thin.
        from datetime import UTC, datetime

        from polaris_api.services.chatwoot.handoff import initiate_handoff

        result = await initiate_handoff(
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            team_id_str=team_id,
            private_note=private_note,
            # The customer-facing handoff message (handoff_message_in/after_hours,
            # branch or tenant) is rendered inside initiate_handoff and may embed
            # {customer_name} / {last_reply} placeholders, so thread the tool
            # context values through for substitution parity with classic-mode
            # handoff. intent_tier stays None — the agent runtime has no intent
            # tier (that's a classic-pipeline concept).
            last_reply=last_reply,
            intent_tier=None,
            customer_name=customer_name,
            requested_at=datetime.now(UTC),
            conn=self._conn,
            chatwoot=self._chatwoot,
            meta_client=self._meta,
        )
        # ADR-051: only ``confirmed`` maps to True. Both ``declined``
        # (transient) and ``errored`` (structural) map to False; the
        # distinction lives in the audit trail (HandoffResult.reason +
        # handoff.initiated event row).
        return result.outcome == "confirmed"


class ConversationAdapter:
    """Wraps ``services/conversation.load_history``.

    Holds an asyncpg.Connection + redis pool injected at construction —
    same posture as ``RAGAdapter``. The caller has already set the
    ``app.current_tenant_id`` GUC on the connection so RLS double-guards
    the read.

    Returns history in the Anthropic SDK ``messages`` parameter shape
    (chronological list of ``{"role", "content"}`` dicts). The last item
    is the JUST-saved user turn, which the caller strips before calling
    the LLM (mirrors ``pipeline.py:253``).
    """

    def __init__(
        self,
        *,
        conn: asyncpg.Connection,
        redis: redis_async.Redis | None = None,
    ) -> None:
        self._conn = conn
        self._redis = redis

    async def load_history(
        self,
        *,
        tenant_id: UUID,
        conversation_id: UUID | None,
        max_messages: int = 20,
        max_tokens: int = 8000,
    ) -> list[dict[str, str]]:
        # No conversation; nothing to load.
        if conversation_id is None:
            return []
        window = await conversation_service.load_history(
            conn=self._conn,
            redis=self._redis,
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            max_messages=max_messages,
            max_tokens=max_tokens,
        )
        # Drop the just-saved user turn (it's the current message,
        # threaded separately into llm.generate_response via
        # ``user_message=`` by the caller). Matches pipeline.py:253
        # behaviour.
        history_messages = window.messages[:-1] if window.messages else []
        return [{"role": m.role, "content": m.content} for m in history_messages]


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


async def build_ports(
    *,
    conn: asyncpg.Connection,
    redis: redis_async.Redis,
    meta_client: MetaWhatsAppClient,
    tenant_branch_id: UUID | None = None,
) -> Ports:
    """Construct the Ports container for the request. Split out so
    tests can monkeypatch with fakes.

    ``meta_client`` is accepted (not used to build a MetaPort — that port
    was retired with flow_runtime) so callers don't need special-casing;
    it's forwarded to ``ChatwootAdapter`` for the handoff's Meta-side send.

    audit Batch B (B3): builds ONE per-turn embed-memo dict shared by both
    ``RAGAdapter`` and ``ProductsAdapter`` — a repeated query string across
    either adapter (or a repeated ``search_knowledge`` tool call) reuses the
    cached embedding instead of re-calling ``embed.embed_query``. The dict
    lives on this call's local scope only (NOT module level) — ``build_ports``
    runs fresh per inbound turn, so the cache's lifetime is exactly one
    message.
    """
    from polaris_api.services.chatwoot.client import get_chatwoot_client

    embed_cache: dict[str, list[float]] = {}

    return Ports(
        llm=LLMAdapter(),
        rag=RAGAdapter(conn=conn, redis=redis, embed_cache=embed_cache),
        products=ProductsAdapter(
            conn=conn, embed_cache=embed_cache, tenant_branch_id=tenant_branch_id
        ),
        promotions=PromotionsAdapter(conn=conn, tenant_branch_id=tenant_branch_id),
        chatwoot=ChatwootAdapter(
            conn=conn,
            chatwoot_client=get_chatwoot_client(),
            meta_client=meta_client,
        ),
        conversation=ConversationAdapter(conn=conn, redis=redis),
    )


__all__ = [
    "ChatwootAdapter",
    "ChatwootPort",
    "ConversationAdapter",
    "ConversationPort",
    "LLMAdapter",
    "LLMPort",
    "Ports",
    "ProductsAdapter",
    "ProductsPort",
    "PromotionsAdapter",
    "PromotionsPort",
    "RAGAdapter",
    "RAGPort",
    "build_ports",
]
