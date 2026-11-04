"""``lookup_products`` tool — reuses the products adapter."""

from __future__ import annotations

from typing import Any

from polaris_api.services.agent.tools.base import (
    ToolContext,
    ToolResult,
    register_tool,
)


@register_tool(
    name="lookup_products",
    description=(
        "Find items from the business's product catalog that match what the "
        "customer is asking about. Returns matching product names and "
        "descriptions. Use this when the customer asks what is available, asks "
        "about a specific item, or wants a recommendation."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "What the customer is looking for.",
            }
        },
        "required": ["query"],
    },
)
async def lookup_products(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    query = str(args.get("query") or "").strip() or ctx.last_user_message
    hits = await ctx.ports.products.search_products(
        query=query, tenant_id=ctx.tenant_id, top_k=5
    )
    if not hits:
        return ToolResult("No matching products were found in the catalog.")
    lines = "\n".join(f"- {h.name}: {h.description}" for h in hits)
    return ToolResult(lines)
