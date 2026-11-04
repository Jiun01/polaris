"""``handoff_to_human`` tool — reuses the Chatwoot handoff adapter.

This is the structured replacement for the deleted ``[HANDOFF_TRIGGER]`` sentinel
(B1): the model invokes a real tool instead of emitting a magic string that
leaked to customers.
"""

from __future__ import annotations

from typing import Any

from polaris_api.services.agent.tools.base import (
    ToolContext,
    ToolResult,
    register_tool,
)

# Default Chatwoot team id for bot-initiated handoffs (the unassigned/default
# team). Mirrors the flow templates' handoff target.
_DEFAULT_TEAM_ID = "0"


@register_tool(
    name="handoff_to_human",
    description=(
        "Escalate the conversation to a human agent. Use this when you cannot "
        "resolve the request, the customer explicitly asks for a person, the "
        "topic is sensitive (a complaint, a refund needing approval, anything "
        "legal/medical/financial), or the customer is upset. Returns whether the "
        "handoff was accepted. Do not promise a specific response time."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "description": "A short note for the human agent explaining the situation.",
            }
        },
        "required": ["reason"],
    },
)
async def handoff_to_human(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    if ctx.conversation_id is None:
        return ToolResult(
            "Handoff is not available in this context; continue helping the customer."
        )
    reason = str(args.get("reason") or "").strip() or "Customer requested a human agent."
    accepted = await ctx.ports.chatwoot.handoff(
        conversation_id=ctx.conversation_id,
        team_id=_DEFAULT_TEAM_ID,
        private_note=reason,
        tenant_id=ctx.tenant_id,
        customer_name=ctx.customer_name,
        last_reply=ctx.last_user_message,
    )
    if accepted:
        return ToolResult(
            "Handoff started — a human agent has been notified. Let the customer "
            "know someone from the team will follow up, without promising a time."
        )
    return ToolResult(
        "A human agent could not be reached right now. Apologize briefly and offer "
        "to take a message or keep helping."
    )
