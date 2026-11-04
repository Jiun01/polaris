"""The agent tool-use loop (ADR-063).

Runs the standard Anthropic tool-use loop: the model emits ``tool_use`` blocks,
we execute the matching registered tools, feed ``tool_result`` blocks back, and
repeat until the model returns a plain text reply or the iteration cap is hit.

Bounded + safe:
  * Only the tenant's enabled tools are offered (``tools_for``) — a disabled tool
    is absent from the request, so the prompt can't summon it.
  * Hard cap ``max_tool_iterations``; on cap we make one final no-tools call so
    the customer always gets a text reply.
  * A tool that raises is reported back to the model as an error ``tool_result``
    rather than crashing the turn.
"""

from __future__ import annotations

import logging
import time
from typing import Any, cast

from polaris_shared_types import IntentTier, ResponseEnvelope, UsageMetrics

from polaris_api.services import llm
from polaris_api.services.agent.result import AgentReplyResult
from polaris_api.services.agent.tools.base import (
    ToolContext,
    anthropic_tool_params,
    tools_for,
)

logger = logging.getLogger(__name__)


async def run_agent_reply(
    ctx: ToolContext,
    *,
    enabled_tools: list[str],
    max_tool_iterations: int,
    system_prompt: str,
    conversation_history: list[dict[str, str]],
    tier: str | None = None,
) -> AgentReplyResult:
    """Drive the tool loop and return the final reply envelope.

    ``conversation_history`` is the prior turns (oldest first); the current user
    message rides on ``ctx.last_user_message``.

    NOTE on ``tier`` dual use (ADR-031 D4): this same parameter feeds two
    different tier concepts today. ``tools_for(enabled_tools, tier=tier)``
    below expects a *plan* tier (``starter``/``pro``/``ultra``/``enterprise``)
    to gate ``ToolSpec.min_tier``. ``llm.generate_with_tools(model_tier=tier)``
    below expects an *intent* tier (``simple``/``medium``/``hard``/``handoff``)
    to pick Haiku vs Sonnet. The only caller (``try_run_agent``) currently
    passes an ``IntentTier`` value (e.g. ``"hard"``), which is harmless for
    ``tools_for`` today because no registered tool sets ``min_tier`` — an
    unrecognized string just resolves to the lowest rank, matching everything.
    If a future tool sets ``min_tier``, this collision needs its own plan-tier
    parameter threaded through separately; flagged here rather than silently
    fixed since splitting it is out of scope for this batch.
    """
    specs = tools_for(enabled_tools, tier=tier)
    tool_params = anthropic_tool_params(specs)
    spec_by_name = {s.name: s for s in specs}

    messages: list[dict[str, Any]] = list(conversation_history)
    messages.append({"role": "user", "content": ctx.last_user_message})

    t0 = time.monotonic()
    final_text = ""
    last_usage: UsageMetrics = UsageMetrics(input_tokens=0, output_tokens=0)
    tools_invoked: list[str] = []

    # ``tier`` is dual-purpose (see NOTE above) so it's typed ``str | None``;
    # the caller (``try_run_agent``) only ever passes an ``IntentTier`` value
    # or ``None``, so the cast is safe. ``generate_with_tools`` itself falls
    # back to Haiku for anything it doesn't recognize as "hard".
    model_tier = cast(IntentTier, tier) if tier is not None else "simple"

    for _ in range(max(1, max_tool_iterations)):
        turn = await llm.generate_with_tools(
            system_prompt=system_prompt,
            messages=messages,
            tools=tool_params,
            model_tier=model_tier,
        )
        last_usage = turn.usage
        if turn.stop_reason != "tool_use" or not turn.tool_calls:
            final_text = turn.text
            break

        messages.append({"role": "assistant", "content": turn.raw_assistant_content})
        results: list[dict[str, Any]] = []
        for call in turn.tool_calls:
            spec = spec_by_name.get(call.name)
            if spec is None:
                results.append(_tool_result_block(call.id, f"Unknown tool {call.name!r}.", True))
                continue
            tools_invoked.append(call.name)
            try:
                res = await spec.handler(call.input, ctx)
            except Exception:  # noqa: BLE001 — never let a tool crash the turn
                logger.exception("agent tool %s failed", call.name)
                results.append(
                    _tool_result_block(call.id, "The tool failed; continue without it.", True)
                )
                continue
            results.append(_tool_result_block(call.id, res.content, res.is_error))
        messages.append({"role": "user", "content": results})
    else:
        # Iteration cap hit mid-tool-use: one final no-tools call for a reply.
        turn = await llm.generate_with_tools(
            system_prompt=system_prompt,
            messages=messages,
            tools=[],
            model_tier=model_tier,
        )
        final_text = turn.text
        last_usage = turn.usage

    if not final_text.strip():
        final_text = (
            "Sorry, I could not put together a reply just now. Let me get someone "
            "from the team to help."
        )

    language = llm._detect_language(ctx.last_user_message or "")  # noqa: SLF001
    envelope = ResponseEnvelope(
        content=final_text,
        intent="medium",
        model_used="haiku",
        language_detected=language,
        usage=last_usage,
        latency_ms=int((time.monotonic() - t0) * 1000),
        rag_chunk_ids=[],
        fallback_used=False,
    )
    return AgentReplyResult(envelope=envelope, tools_invoked=tuple(tools_invoked))


def _tool_result_block(tool_use_id: str, text: str, is_error: bool) -> dict[str, Any]:
    return {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "content": [{"type": "text", "text": text}],
        "is_error": is_error,
    }
