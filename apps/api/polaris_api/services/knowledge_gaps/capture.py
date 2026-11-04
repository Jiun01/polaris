"""Knowledge-gap miss capture — Knowledge Gap Detection / M1.

Called from the inbound pipeline (anon pool, RLS, tenant GUC already set) to log
one ``knowledge_gap_misses`` row whenever the bot failed to confidently answer.

Two entry points:
  * ``classify_retrieval_miss`` — a pure helper deciding whether a generated
    reply's RAG result counts as a miss, and which signal it is. Side-effect
    free so the decision is unit-testable without a DB.
  * ``record_miss`` — a fail-soft INSERT. A capture failure must NEVER block the
    customer reply (the reply is already persisted/dispatched by the time we get
    here). The INSERT is wrapped in a nested ``transaction()`` SAVEPOINT so that
    an error rolls back ONLY the capture, leaving the pipeline's outer
    transaction healthy to commit the reply — a plain try/except would not, since
    a failed statement poisons the surrounding asyncpg transaction.

Capture is gated by ``settings.knowledge_gap_capture_enabled`` at the pipeline
call site, not here, so this module stays a thin, testable unit.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Sequence
from typing import Literal
from uuid import UUID

import asyncpg
from polaris_shared_types.knowledge_chunk import KnowledgeChunkHit

logger = logging.getLogger(__name__)

SignalType = Literal["no_retrieval", "low_retrieval", "handoff"]

# Defensive cap on the stored question text. The inbound body / voice transcript
# can be arbitrarily long; the gap table is for aggregation, not full replay, so
# we bound the PII surface + row size. Mirrors the handoff_private_note_max_len
# posture. Truncation is silent — the clustering signal survives the cut.
_MAX_QUESTION_LEN = 2000


def parse_stashed_score(raw: object) -> float | None:
    """Parse the flow-stashed ``last_rag_top_score`` var into a float, fail-soft.

    The var is a tenant-influenced surface (a flow node, a test fixture, or a
    JSONB replay could write a non-numeric or non-finite value). Returns ``None``
    on anything that isn't a finite number, so a malformed value can NEVER raise
    on the reply's critical path (the float parse lives OUTSIDE record_miss's
    savepoint, so an unguarded ``float()`` here would abort the reply dispatch).
    """
    if raw is None:
        return None
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def classify_score_miss(
    top_score: float | None, threshold: float
) -> tuple[bool, SignalType | None, float | None]:
    """Core miss decision from a single retrieval ``top_score``.

    Returns ``(is_miss, signal_type, top_score)``:
      * ``(True, "no_retrieval", None)`` — ``top_score is None``: retrieval ran
        but matched nothing in the KB. Unambiguous; always a miss.
      * ``(True, "low_retrieval", top_score)`` — ``top_score`` is STRICTLY below
        ``threshold``. Only fires when ``threshold > 0`` (the GA default 0.0
        disables it — the score's scale differs between the hybrid-RRF and
        vector-only paths, so the floor needs tuning first).
      * ``(False, None, top_score)`` — healthy retrieval; not a miss.

    Shared by the Phase 4 path (``classify_retrieval_miss``, which has the chunk
    list) and the flow path (which only has the score stashed by ``llm_reply``).
    """
    if top_score is None:
        return (True, "no_retrieval", None)
    if threshold > 0.0 and top_score < threshold:
        return (True, "low_retrieval", top_score)
    return (False, None, top_score)


def classify_retrieval_miss(
    chunks: Sequence[KnowledgeChunkHit], threshold: float
) -> tuple[bool, SignalType | None, float | None]:
    """Decide whether a generated reply's RAG result is a knowledge-gap miss.

    Thin wrapper over ``classify_score_miss``: reduces the chunk list to its MAX
    ``rrf_score`` (higher = better; we don't assume the list is pre-sorted), so
    an empty list collapses to ``None`` → ``no_retrieval``.

    NOTE (report F3): ``rrf_score`` is rank-derived — the #1 vector-only hit is
    ALWAYS ``1/(k+1)`` regardless of true similarity, so a correct answer and a
    no-match score identically and NO threshold can separate them. Prefer
    ``classify_confidence_miss`` (cosine-distance based, migration 113) for the
    ``low_retrieval`` signal; this remains for the flow path that only stashes a
    score.
    """
    top_score = max((c.rrf_score for c in chunks), default=None)
    return classify_score_miss(top_score, threshold)


def retrieval_confidence(chunks: Sequence[KnowledgeChunkHit]) -> float | None:
    """Best cosine similarity across the hits, in ``[0, 1]`` (higher = closer).

    Derived from the per-hit cosine ``distance`` the hybrid RPC now returns
    (migration 113, report F2): ``similarity = 1 - min(distance)``. Unlike
    ``rrf_score`` this preserves relevance MAGNITUDE, so it can actually
    distinguish a strong match from the nearest-of-the-irrelevant.

    Returns ``None`` when no hit carries a distance — either zero chunks, or only
    BM25-only hits (a lexical match the vector leg didn't surface, which we treat
    as "can't judge confidence" rather than "no confidence").
    """
    dists = [c.distance for c in chunks if c.distance is not None]
    if not dists:
        return None
    return 1.0 - min(dists)


def classify_confidence_miss(
    chunks: Sequence[KnowledgeChunkHit], min_similarity: float
) -> tuple[bool, SignalType | None, float | None]:
    """Knowledge-gap decision from retrieval CONFIDENCE (cosine similarity).

    Returns ``(is_miss, signal_type, confidence)``:
      * empty chunk list → ``(True, "no_retrieval", None)``.
      * a confidence below ``min_similarity`` (and ``min_similarity > 0``) →
        ``(True, "low_retrieval", confidence)``. ``min_similarity = 0`` keeps the
        signal disabled (the calibrated floor must be set per deployment — a
        synthetic corpus can't pin it).
      * BM25-only hits (no distance, confidence ``None``) → never a low_retrieval
        miss (lexical match implies some relevance); not a miss.
      * otherwise healthy → ``(False, None, confidence)``.
    """
    if not chunks:
        return (True, "no_retrieval", None)
    confidence = retrieval_confidence(chunks)
    if confidence is None:
        return (False, None, None)
    if min_similarity > 0.0 and confidence < min_similarity:
        return (True, "low_retrieval", confidence)
    return (False, None, confidence)


async def tenant_in_capture_tier(
    conn: asyncpg.Connection, tenant_id: UUID, tiers: frozenset[str]
) -> bool:
    """True if the tenant's ``plan_tier`` is in ``tiers`` (the Ultra-gate set).

    Knowledge Gap Detection is an Ultra-tier feature — a non-Ultra tenant
    captures nothing (no rows, no PII). Reads the same ``tenants.plan_tier``
    column the RAG tier-gate uses. Fail-safe: a missing/unreadable row yields
    False (capture nothing) rather than over-capturing.

    Kept for callers that don't already have a tenant snapshot in hand
    (other tests/consumers). The inbound pipeline uses
    ``plan_tier_in_capture_tier`` instead (audit Batch B / B1) since it
    already loaded ``plan_tier`` via the consolidated tenant snapshot.
    """
    try:
        row = await conn.fetchrow(
            "SELECT plan_tier FROM tenants WHERE id = $1", tenant_id
        )
    except Exception:  # noqa: BLE001 — gate read is best-effort; default closed
        return False
    return row is not None and row["plan_tier"] in tiers


def plan_tier_in_capture_tier(plan_tier: str | None, tiers: frozenset[str]) -> bool:
    """Pure, DB-free variant of ``tenant_in_capture_tier`` (audit Batch B / B1).

    Takes an already-known ``plan_tier`` (e.g. from the consolidated tenant
    snapshot the inbound pipeline loads once per message) instead of issuing
    its own ``SELECT ... FROM tenants``. ``None`` (missing/unknown tenant)
    is never in ``tiers`` — same fail-closed default as the DB-backed
    version.
    """
    return plan_tier is not None and plan_tier in tiers


async def record_miss(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    conversation_id: UUID,
    message_id: UUID | None,
    customer_question: str,
    signal_type: SignalType,
    retrieval_top_score: float | None,
    channel: str,
    detected_language: str | None,
) -> None:
    """Best-effort INSERT of one ``knowledge_gap_misses`` row.

    Fail-soft: any error (RLS, FK, connection) is logged and swallowed — miss
    capture must never break the customer-facing reply. The INSERT runs in a
    nested ``transaction()`` SAVEPOINT so a failure rolls back only this write
    and leaves the pipeline's outer transaction able to commit the reply.
    """
    try:
        async with conn.transaction():
            await conn.execute(
                """
                INSERT INTO knowledge_gap_misses (
                    tenant_id, conversation_id, message_id, customer_question,
                    signal_type, retrieval_top_score, channel, detected_language
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                """,
                tenant_id,
                conversation_id,
                message_id,
                customer_question[:_MAX_QUESTION_LEN],
                signal_type,
                retrieval_top_score,
                channel,
                detected_language,
            )
    except Exception:  # noqa: BLE001 — capture is best-effort; never block reply
        logger.warning(
            "knowledge_gaps.record_miss: capture failed tenant=%s conv=%s "
            "signal=%s — reply unaffected",
            tenant_id,
            conversation_id,
            signal_type,
            exc_info=True,
        )
