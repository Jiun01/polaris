"""``capture_lead`` tool — records contact details to the audience.

Thin wrapper over ``audience.upsert_audience_member`` (idempotent: backfills a
missing name, never overwrites an existing one). Any email/phone the customer
shared rides in ``metadata`` for the team to follow up.
"""

from __future__ import annotations

from typing import Any

from polaris_api.services.agent.tools.base import (
    ToolContext,
    ToolResult,
    register_tool,
)
from polaris_api.services.audience.members import upsert_audience_member


@register_tool(
    name="capture_lead",
    description=(
        "Save the customer's contact details to the business's audience so the "
        "team can follow up. Use this once the customer shares who they are or "
        "leaves an email or phone number. Pass whatever you have; all fields are "
        "optional."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "The customer's name, if shared."},
            "email": {"type": "string", "description": "The customer's email, if shared."},
            "phone": {"type": "string", "description": "The customer's phone, if shared."},
        },
        "required": [],
    },
)
async def capture_lead(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    name = str(args.get("name") or "").strip() or ctx.customer_name or None
    metadata: dict[str, Any] = {}
    email = str(args.get("email") or "").strip()
    phone = str(args.get("phone") or "").strip()
    if email:
        metadata["email"] = email
    if phone:
        metadata["phone"] = phone
    await upsert_audience_member(
        ctx.conn,
        tenant_id=ctx.tenant_id,
        channel=ctx.channel,
        channel_user_id=ctx.channel_user_id,
        channel_user_name=name,
        metadata=metadata or None,
    )
    return ToolResult("Saved the customer's details to your audience for follow-up.")
