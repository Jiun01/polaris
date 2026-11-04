"""Agent tool registry — import side effects populate ``TOOL_REGISTRY``.

Importing this package registers every tool. ``A0`` ships four tools that
reuse the adapters in ``services/agent/ports.py``; booking / send_template /
interactive tools are follow-ups (the registry makes adding one a
single-file drop).
"""

from __future__ import annotations

from polaris_api.services.agent.tools import (  # noqa: F401
    capture_lead,
    handoff_to_human,
    lookup_products,
    lookup_promotions,
    search_knowledge,
)
from polaris_api.services.agent.tools.base import (
    TOOL_REGISTRY,
    ToolContext,
    ToolResult,
    ToolSpec,
    anthropic_tool_params,
    register_tool,
    tools_for,
)

__all__ = [
    "TOOL_REGISTRY",
    "ToolContext",
    "ToolResult",
    "ToolSpec",
    "anthropic_tool_params",
    "register_tool",
    "tools_for",
]
