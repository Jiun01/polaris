"""``lookup_promotions`` tool — reuses the promotions adapter.

Mirrors ``lookup_products``. Surfaces the tenant's currently-active promotions
(``is_active`` AND now within the validity window), branch-scoped by the
conversation's branch inside the adapter. The description tells the model to
call this ONLY when the customer asks about deals/discounts — mirroring the
classic path's "Never push a promotion unprompted" guard — so promos are never
volunteered.
"""

from __future__ import annotations

from typing import Any

from polaris_api.services.agent.tools.base import (
    ToolContext,
    ToolResult,
    register_tool,
)


@register_tool(
    name="lookup_promotions",
    description=(
        "Check the business's current promotions, deals, or discounts and return "
        "any that are active right now. Use this ONLY when the customer asks about "
        "deals, discounts, offers, or promotions — never volunteer a promotion "
        "unprompted."
    ),
    input_schema={"type": "object", "properties": {}},
)
async def lookup_promotions(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    promos = await ctx.ports.promotions.fetch_active(tenant_id=ctx.tenant_id)
    if not promos:
        return ToolResult("There are no active promotions right now.")
    lines = "\n".join(
        f"- {p.title}: {p.discount_text} — {p.description}"
        + (f" (Terms: {p.terms})" if p.terms else "")
        for p in promos
    )
    return ToolResult(lines)
