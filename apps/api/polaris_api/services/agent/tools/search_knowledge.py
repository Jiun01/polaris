"""``search_knowledge`` tool — reuses the RAG adapter."""

from __future__ import annotations

from typing import Any

from polaris_api.services.agent.tools.base import (
    ToolContext,
    ToolResult,
    register_tool,
)


@register_tool(
    name="search_knowledge",
    description=(
        "Search the business's knowledge base for facts to answer the customer. "
        "Use this for any question about products, pricing, policies, hours, or "
        "how things work. Returns the most relevant excerpts. Always search "
        "before answering a factual question rather than guessing."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The search query — usually the customer's question.",
            }
        },
        "required": ["query"],
    },
)
async def search_knowledge(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    query = str(args.get("query") or "").strip() or ctx.last_user_message
    hits = await ctx.ports.rag.search(query=query, tenant_id=ctx.tenant_id, top_k=5)
    if not hits:
        return ToolResult("No matching knowledge-base entries were found.")
    excerpts = "\n\n".join(f"[{i + 1}] {h.content}" for i, h in enumerate(hits))
    return ToolResult(excerpts)
