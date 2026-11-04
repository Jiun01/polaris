"""Seed the demo tenant's knowledge base — Phase 4 PR-6.

Idempotent. Seeds three things under the polaris-internal tenant
(UUID per migration 008b, ADR-040):

  1. The polaris-internal ``tenants`` row — already seeded by
     migration 008b; we just verify it exists.
  2. A ``tenant_channels`` row pinning the demo WABA phone_number_id
     (META_DEMO_PHONE_NUMBER_ID) to the polaris-internal tenant so
     the Phase 2 webhook resolver can find a tenant for inbound
     demo traffic. Skipped if a row with the same phone_number_id
     already exists (idempotent).
  3. ~18 ``knowledge_chunks`` rows describing Polaris itself —
     pricing tiers, channels, RAG, flow builder, ingestion, etc. —
     so the demo bot has something to retrieve from. Embeddings via
     services/embed.embed_query (OpenRouter qwen/qwen3-embedding-8b
     at 1024 dim).

Usage:

  uv run python -m apps.api.scripts.seed_demo_knowledge

Requires real ANTHROPIC_API_KEY + OPENROUTER_API_KEY in `.env` (the
script will tell you which one is missing).

EMBEDDING MODEL (ADR-059):
  Chunks are embedded via whatever EMBEDDING_MODEL points at — locked to
  OpenRouter `qwen/qwen3-embedding-8b` MRL-1024, matching the desktop
  ingestion app (Polaris Ingestion System V2). If you ever change the
  model, the embedding space changes, so:
    1. Update EMBEDDING_MODEL on BOTH .env files (API + ingestion)
    2. DELETE FROM knowledge_chunks WHERE tenant_id = polaris-internal
    3. Re-run this script
  Chunks embedded against a different model are unreachable from queries
  embedded with the new one (different embedding space).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import sys
from dataclasses import dataclass
from pathlib import Path

import asyncpg

# Direct-script invocation needs the repo root on sys.path so
# `from polaris_api.services ...` resolves outside of pytest.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from polaris_api.config import get_settings  # noqa: E402 — after sys.path tweak
from polaris_api.services import embed  # noqa: E402
from polaris_api.services.demo_sessions import (  # noqa: E402
    POLARIS_INTERNAL_TENANT_ID,
)

logger = logging.getLogger(__name__)

# Chatwoot inbox id for the demo channel. Unique-indexed on
# tenant_channels — pick a high number so we don't collide with
# real tenant inboxes (1-99) or platform reserved ids.
_DEMO_CHATWOOT_INBOX_ID: int = 999_000_001


@dataclass(slots=True, frozen=True)
class DemoChunk:
    """One knowledge chunk to seed. ``source`` is what the chunk's
    citation will read as in the bot's reply."""
    source: str
    content: str


_DEMO_CHUNKS: list[DemoChunk] = [
    DemoChunk(
        source="polaris-overview.md",
        content=(
            "Polaris is a multi-tenant chatbot platform for SMEs in "
            "Malaysia. It handles inbound conversations across WhatsApp "
            "Business API, Instagram Direct, and Facebook Messenger from "
            "one unified inbox. Each tenant gets a configurable bot that "
            "can answer FAQs, route to human agents, and run multi-step "
            "flows like bookings and product recommendations."
        ),
    ),
    DemoChunk(
        source="polaris-overview.md",
        content=(
            "Polaris was built specifically for Malaysian businesses. "
            "It supports English, Bahasa Malaysia, and Simplified Chinese "
            "(简体中文) natively — the bot auto-detects the language of "
            "each inbound message and replies in the same language."
        ),
    ),
    DemoChunk(
        source="pricing.md",
        content=(
            "Polaris has four pricing tiers: Starter (RM99/month), Pro "
            "(RM299/month), Ultra (RM799/month), and Enterprise (custom "
            "pricing — contact sales). Each tier includes a monthly bot "
            "reply quota; messages above the quota are billed per-reply. "
            "All tiers include the same channel surfaces — only the "
            "quota size + advanced features differ."
        ),
    ),
    DemoChunk(
        source="pricing.md",
        content=(
            "Cohere semantic rerank is included on Ultra and Enterprise "
            "tiers by default. It improves answer relevance on questions "
            "where keyword search misses the intent — for example, "
            "'how do I sign up?' matching a doc titled 'onboarding'. "
            "Starter and Pro tenants can opt in per-tenant if they want "
            "the rerank cost on their account."
        ),
    ),
    DemoChunk(
        source="channels.md",
        content=(
            "WhatsApp connection: paste your WhatsApp Cloud API system-"
            "user token + phone_number_id into the portal. Polaris will "
            "verify the connection and start receiving inbound webhooks "
            "within a minute. Your Meta Business must be verified for "
            "production sending; demo/test mode works without "
            "verification for the first 5 recipient numbers."
        ),
    ),
    DemoChunk(
        source="channels.md",
        content=(
            "Instagram Direct + Facebook Messenger connect via Facebook "
            "Login OAuth — click 'Connect' in the portal, pick which "
            "Page or IG account to authorize, and Polaris handles the "
            "webhook subscription. Per-channel inboxes can be enabled "
            "or disabled independently."
        ),
    ),
    DemoChunk(
        source="rag-knowledge-base.md",
        content=(
            "Polaris uses RAG (retrieval-augmented generation) to answer "
            "from your own content. You upload PDF, DOCX, HTML, or "
            "Markdown files (or point Polaris at a URL to crawl) via the "
            "ingestion desktop app. The app chunks the content, "
            "generates embeddings (Qwen3), and uploads to Polaris. The "
            "bot retrieves the top-5 most relevant chunks per inbound "
            "question and grounds its reply in them."
        ),
    ),
    DemoChunk(
        source="rag-knowledge-base.md",
        content=(
            "Hybrid retrieval combines BM25 keyword search with vector "
            "semantic search via Reciprocal Rank Fusion. Both halves "
            "complement each other: BM25 catches exact-term questions "
            "('pricing'), vectors catch paraphrased intent ('how much "
            "does it cost'). Ultra+ tenants additionally rerank the "
            "top-25 candidates via Cohere for a quality bump on "
            "ambiguous queries."
        ),
    ),
    DemoChunk(
        source="flow-builder.md",
        content=(
            "Flow Builder is Polaris's visual canvas for designing "
            "multi-step conversation flows. Drag-and-drop nodes for: "
            "FAQ answer, ask question, condition branch, collect input, "
            "call external API, schedule booking, send template, hand "
            "off to human agent. Each flow is versioned — publish a "
            "new version and roll back if the metrics drop."
        ),
    ),
    DemoChunk(
        source="flow-builder.md",
        content=(
            "Flows complement the bot's default RAG-answering. When a "
            "tenant hasn't built any flows, the bot answers everything "
            "from RAG + LLM. Once flows are published, the bot first "
            "checks if the inbound matches a flow's trigger; if yes, "
            "runs the flow; if no, falls back to default RAG answering."
        ),
    ),
    DemoChunk(
        source="handoff.md",
        content=(
            "Human handoff: when the bot classifies an inbound message "
            "as 'handoff' (explicit request for a human, hostile "
            "message, legal/medical/financial question), the conversation "
            "transfers to a human agent in Chatwoot. The agent sees the "
            "full conversation history with the bot's intent + RAG "
            "citations annotated."
        ),
    ),
    DemoChunk(
        source="bookings.md",
        content=(
            "Bookings are powered by an Easy!Appointments integration. "
            "Tenants configure their availability calendar, services, "
            "and durations in Polaris. Customers ask the bot for a "
            "booking; the bot collects preferred date/time, checks "
            "availability, confirms the slot, and the booking syncs to "
            "the tenant's calendar. PDPA opt-in is captured at booking "
            "time."
        ),
    ),
    DemoChunk(
        source="broadcasts.md",
        content=(
            "Broadcasts let tenants send WhatsApp templates to opted-in "
            "audience segments. Audience members are imported via CSV "
            "(E.164 phone numbers + consent column) or built from past "
            "conversations. Broadcasts respect WhatsApp template "
            "approval rules and MM Lite routing for MARKETING messages "
            "when the tenant is onboarded."
        ),
    ),
    DemoChunk(
        source="privacy-pdpa.md",
        content=(
            "Polaris is PDPA-compliant. Tenants own their data; Polaris "
            "is the processor. Each tenant signs a DPA at sign-up. "
            "End-customer data (conversations, contacts) is retained "
            "per the tenant's configured retention window (default 90 "
            "days) and purged via a nightly sweeper. Data export and "
            "deletion requests are honored within 30 days."
        ),
    ),
    DemoChunk(
        source="security.md",
        content=(
            "Per-tenant data isolation is enforced at the database level "
            "via Postgres Row-Level Security. Every read + write carries "
            "a tenant_id GUC; the SQL policy strips other tenants' rows "
            "even if application code were buggy. Tenant secrets (Meta "
            "tokens, Stripe keys) are encrypted at rest with libsodium "
            "XChaCha20-Poly1305."
        ),
    ),
    DemoChunk(
        source="onboarding.md",
        content=(
            "Onboarding takes about 30 minutes. Sign up on the portal "
            "(magic link via email), confirm your Meta Business is "
            "verified, paste your WhatsApp Cloud API token, and Polaris "
            "verifies the connection. Upload your knowledge base files "
            "via the ingestion desktop app, design any custom flows in "
            "the Flow Builder, and you're live."
        ),
    ),
    DemoChunk(
        source="demo-info.md",
        content=(
            "This bot you're talking to right now is the Polaris demo. "
            "It runs on the live Polaris API with the Polaris product "
            "documentation as its knowledge base — exactly the same "
            "setup any tenant would have. Demo sessions are limited to "
            "50 messages and expire after 24 hours; ask me anything "
            "about Polaris features, pricing, or how to onboard."
        ),
    ),
    DemoChunk(
        source="contact.md",
        content=(
            "Talk to a human about Polaris: WhatsApp +60 11-5458 1688 "
            "(this number). Email contact@polaris.my. For pricing "
            "questions and Enterprise demos, ask the bot to 'connect "
            "me with sales' and it will hand you off to a human agent "
            "during business hours (Mon-Fri 9am-6pm MYT)."
        ),
    ),
]


async def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = get_settings()
    if not settings.polaris_db_url_service:
        logger.error(
            "POLARIS_DB_URL_SERVICE not set — seed needs the BYPASSRLS "
            "service-role DSN to write tenant_channels + knowledge_chunks "
            "across tenant boundaries (the polaris-internal tenant + the "
            "chunks both need to be written without a tenant context)."
        )
        return 2
    if not settings.meta_demo_phone_number_id:
        logger.error(
            "META_DEMO_PHONE_NUMBER_ID not set — needed for the demo "
            "tenant_channels row. Get it from Meta App Dashboard → "
            "WhatsApp → API Setup → 'From' dropdown."
        )
        return 2
    if (
        not settings.openrouter_api_key
        or settings.openrouter_api_key.startswith("placeholder-")
    ):
        logger.error(
            "OPENROUTER_API_KEY not set or placeholder — needed to embed "
            "the demo knowledge chunks. Get from openrouter.ai/settings/keys."
        )
        return 2

    conn = await asyncpg.connect(settings.polaris_db_url_service)
    try:
        await _verify_polaris_internal_tenant(conn)
        channel_id = await _upsert_demo_channel(
            conn, phone_number_id=settings.meta_demo_phone_number_id,
        )
        logger.info("demo tenant_channels.id = %s", channel_id)
        chunks_inserted = await _seed_chunks(conn)
        logger.info(
            "seed complete: %d demo chunks under polaris-internal "
            "(tenant_channels.id=%s)", chunks_inserted, channel_id,
        )
    finally:
        await conn.close()
    return 0


async def _verify_polaris_internal_tenant(conn: asyncpg.Connection) -> None:
    row = await conn.fetchrow(
        "SELECT id, slug FROM tenants WHERE id = $1",
        POLARIS_INTERNAL_TENANT_ID,
    )
    if row is None:
        raise RuntimeError(
            "polaris-internal tenant row missing — migration 008b should "
            "have seeded it. Run `make migrate` first."
        )
    logger.info("polaris-internal tenant verified (slug=%s)", row["slug"])


async def _upsert_demo_channel(
    conn: asyncpg.Connection, *, phone_number_id: str,
) -> str:
    """Insert (or find) the demo tenant_channels row.

    Uniqueness key in the SQL is ``chatwoot_inbox_id`` (migration
    004). We use a reserved high inbox id (999_000_001) so the
    upsert can target it deterministically without colliding with
    real tenant inboxes.
    """
    row = await conn.fetchrow(
        """
        INSERT INTO tenant_channels (
            tenant_id, channel, chatwoot_inbox_id, phone_number_id, status
        ) VALUES ($1, 'whatsapp', $2, $3, 'active')
        ON CONFLICT (chatwoot_inbox_id) DO UPDATE
          SET phone_number_id = EXCLUDED.phone_number_id
        RETURNING id
        """,
        POLARIS_INTERNAL_TENANT_ID,
        _DEMO_CHATWOOT_INBOX_ID,
        phone_number_id,
    )
    if row is None:  # pragma: no cover
        raise RuntimeError("_upsert_demo_channel: no row returned")
    return str(row["id"])


async def _ensure_seed_ingestion_request(conn: asyncpg.Connection) -> str:
    """Create a single 'completed' ingestion_requests row for the seed batch.

    knowledge_chunks.request_id is NOT NULL — every chunk must link to
    its source ingestion job per Phase 3 schema. For the seed we
    fabricate one row representing "the demo bootstrap batch" and
    point all 18 chunks at its id. Idempotent: looks for an existing
    seed-batch row by a sentinel source_text before inserting.
    """
    sentinel = "polaris-internal-demo-seed:v1"
    existing = await conn.fetchrow(
        "SELECT id FROM ingestion_requests "
        "WHERE tenant_id = $1 AND source_type = 'text' AND source_text = $2",
        POLARIS_INTERNAL_TENANT_ID, sentinel,
    )
    if existing is not None:
        return str(existing["id"])
    row = await conn.fetchrow(
        """
        INSERT INTO ingestion_requests (
            tenant_id, source_type, source_text, status,
            started_at, completed_at, chunks_added
        ) VALUES ($1, 'text', $2, 'completed', now(), now(), $3)
        RETURNING id
        """,
        POLARIS_INTERNAL_TENANT_ID, sentinel, len(_DEMO_CHUNKS),
    )
    if row is None:  # pragma: no cover
        raise RuntimeError("_ensure_seed_ingestion_request: no row returned")
    return str(row["id"])


async def _seed_chunks(conn: asyncpg.Connection) -> int:
    """Insert demo knowledge_chunks. Idempotent via content_hash UNIQUE.

    Uses the service-role connection (BYPASSRLS) so we don't need to
    set app.current_tenant_id — but we still scope tenant_id
    explicitly on every INSERT. Embeddings via services/embed; one
    OpenRouter call per chunk. ~$0.001 total for 18 chunks.
    """
    request_id = await _ensure_seed_ingestion_request(conn)
    logger.info("seed_demo: using ingestion_requests.id = %s", request_id)
    inserted = 0
    for chunk in _DEMO_CHUNKS:
        content_hash = hashlib.sha256(chunk.content.encode("utf-8")).hexdigest()
        existing = await conn.fetchrow(
            "SELECT id FROM knowledge_chunks "
            "WHERE tenant_id = $1 AND content_hash = $2",
            POLARIS_INTERNAL_TENANT_ID, content_hash,
        )
        if existing is not None:
            continue
        embedding = await embed.embed_query(chunk.content)
        if all(v == 0.0 for v in embedding):
            logger.warning(
                "seed_demo: chunk from %s embedded as zero vector — "
                "OpenRouter likely unreachable. Continuing; RAG will "
                "still serve BM25 results.", chunk.source,
            )
        # pgvector accepts the string literal form '[x,y,z]' without
        # needing pgvector.asyncpg.register_vector(); avoids the
        # extra dep for this one-off seed script.
        embedding_literal = "[" + ",".join(repr(float(v)) for v in embedding) + "]"
        await conn.execute(
            """
            INSERT INTO knowledge_chunks (
                tenant_id, request_id, source_name, source_type, chunk_index,
                content, content_hash, embedding, token_count
            ) VALUES ($1, $2, $3, 'document', $4, $5, $6, $7::vector, $8)
            """,
            POLARIS_INTERNAL_TENANT_ID, request_id, chunk.source, inserted,
            chunk.content, content_hash, embedding_literal,
            len(chunk.content) // 4,
        )
        inserted += 1
    return inserted


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
