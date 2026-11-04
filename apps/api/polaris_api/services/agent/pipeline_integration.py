"""Pipeline integration — ``try_run_agent`` (ADR-063).

Glue between ``pipeline.run()`` and the agent loop:

  * ``AgentReplyResult`` — the tenant has the agent enabled and it produced a
    reply. The pipeline persists + dispatches it via the same helpers the Phase 4
    path uses, so the customer-facing shape is identical.
  * ``None`` — the agent is disabled for this tenant. The pipeline falls through
    to the classic branch unchanged (the backward-compat guarantee).

Tenant scoping rides on the ``app.current_tenant_id`` GUC the caller already set;
the reused adapters stay tenant-scoped for free.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

import asyncpg
import redis.asyncio as redis_async
from polaris_shared_types.incoming_message import IncomingMessage

from polaris_api.services import llm, tenant_snapshot
from polaris_api.services._llm_prompts import (
    TenantPersona,
    build_agent_system_prompt,
    coerce_persona_blocks,
)
from polaris_api.services.agent.config import parse_agent_config
from polaris_api.services.agent.loop import run_agent_reply
from polaris_api.services.agent.ports import build_ports
from polaris_api.services.agent.result import AgentReplyResult
from polaris_api.services.agent.tools import TOOL_REGISTRY  # populate registry
from polaris_api.services.agent.tools.base import ToolContext
from polaris_api.services.meta_whatsapp import MetaWhatsAppClient

logger = logging.getLogger(__name__)

_ = TOOL_REGISTRY  # imported for its registration side effects


async def try_run_agent(
    *,
    conn: asyncpg.Connection,
    redis: redis_async.Redis,
    meta_client: MetaWhatsAppClient,
    incoming: IncomingMessage,
    conversation_id: UUID | None,
    channel: str,
    snapshot: dict[str, Any] | None = None,
    tenant_branch_id: UUID | None = None,
) -> AgentReplyResult | None:
    # M1: one tenant read — the agent-enabled check parses the agent_config
    # blob the snapshot already carries, instead of a second query.
    # audit Batch B (B1): the caller (pipeline._try_agent_runtime) now loads
    # the tenant snapshot ONCE near the top of _run_inner and passes it here;
    # only load it ourselves when a caller doesn't have one yet (other
    # callers/tests), so this stays a safe optional-with-fallback seam.
    if snapshot is None:
        snapshot = await tenant_snapshot.load_tenant_snapshot(conn, incoming.tenant_id)
    config = parse_agent_config(snapshot.get("agent_config"))
    if not config.enabled:
        return None

    persona = TenantPersona(
        assistant_name=snapshot.get("persona_assistant_name"),
        company_name=snapshot.get("persona_company_name"),
        system_prompt_override=snapshot.get("system_prompt"),
        persona_blocks=coerce_persona_blocks(snapshot.get("persona_blocks")),
    )
    user_text = incoming.body or ""
    language = llm._detect_language(user_text)  # noqa: SLF001
    # ADR-031 model-tier routing (D4): reuse the same Haiku classifier the
    # classic path calls (pipeline.py's classify_intent) so agent-enabled
    # tenants get Sonnet escalation on "hard" messages too, instead of
    # always defaulting to Haiku regardless of complexity.
    intent_tier = await llm.classify_intent(user_text, tenant_id=incoming.tenant_id)
    # NOTE: the first positional arg to build_agent_system_prompt is typed
    # IntentTier but controls prompt *style* framing, not model selection —
    # confirmed unused inside the function body (verified at Task 7 edit
    # time). Keeping it hardcoded "medium" is intentional; conflating it
    # with intent_tier (the model-tier value) is out of scope here.
    system_prompt = build_agent_system_prompt(
        "medium", language, persona, channel=channel, instructions=config.instructions
    )

    ports = await build_ports(
        conn=conn, redis=redis, meta_client=meta_client, tenant_branch_id=tenant_branch_id
    )

    history: list[dict[str, str]] = []
    try:
        history = await ports.conversation.load_history(
            tenant_id=incoming.tenant_id, conversation_id=conversation_id
        )
    except Exception:  # noqa: BLE001 — history is best-effort, never block the reply
        logger.warning("try_run_agent: history load failed tenant=%s", incoming.tenant_id)

    ctx = ToolContext(
        tenant_id=incoming.tenant_id,
        conversation_id=conversation_id,
        channel=channel,
        channel_user_id=incoming.sender.channel_user_id,
        customer_name=getattr(incoming.sender, "channel_user_name", None),
        last_user_message=user_text,
        conn=conn,
        ports=ports,
    )

    return await run_agent_reply(
        ctx,
        enabled_tools=config.enabled_tools,
        max_tool_iterations=config.max_tool_iterations,
        system_prompt=system_prompt,
        conversation_history=history,
        tier=intent_tier,
    )


__all__ = ["try_run_agent"]
