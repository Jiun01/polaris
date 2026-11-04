"""Agent tool registry + protocol (ADR-063).

Each tool module decorates a handler with ``@register_tool(...)`` and the
registry is built at import time. ``tools_for`` computes the specs an agent
may call = registered ∩ the tenant's ``enabled_tools`` ∩ tier-allowed. A
disabled tool is simply ABSENT from the list handed to the model — the hard
guardrail (the prompt cannot summon a tool that was never offered).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import asyncpg

from polaris_api.services.agent.ports import Ports


@dataclass(frozen=True)
class ToolContext:
    """Everything a tool needs to act, scoped to one inbound message.

    Runs on the pipeline's RLS-scoped connection (``app.current_tenant_id`` GUC
    already set), so the reused adapters stay tenant-scoped for free.
    """

    tenant_id: UUID
    conversation_id: UUID | None
    channel: str
    channel_user_id: str
    customer_name: str | None
    last_user_message: str
    conn: asyncpg.Connection
    ports: Ports


@dataclass(frozen=True)
class ToolResult:
    """A tool's outcome. ``content`` is fed back to the model as the
    ``tool_result`` text; ``is_error`` marks it as a failed call."""

    content: str
    is_error: bool = False


ToolHandler = Callable[[dict[str, Any], ToolContext], Awaitable[ToolResult]]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: ToolHandler
    min_tier: str | None = None  # None → available to every tier


TOOL_REGISTRY: dict[str, ToolSpec] = {}


def register_tool(
    *,
    name: str,
    description: str,
    input_schema: dict[str, Any],
    min_tier: str | None = None,
) -> Callable[[ToolHandler], ToolHandler]:
    def deco(handler: ToolHandler) -> ToolHandler:
        if name in TOOL_REGISTRY:
            raise ValueError(f"duplicate agent tool registration: {name!r}")
        TOOL_REGISTRY[name] = ToolSpec(
            name=name,
            description=description,
            input_schema=input_schema,
            handler=handler,
            min_tier=min_tier,
        )
        return handler

    return deco


_TIER_ORDER = {"starter": 0, "pro": 1, "ultra": 2, "enterprise": 3}


def _tier_ok(spec: ToolSpec, tier: str | None) -> bool:
    if spec.min_tier is None or tier is None:
        return True
    return _TIER_ORDER.get(tier, 0) >= _TIER_ORDER.get(spec.min_tier, 0)


def tools_for(enabled_tools: list[str], *, tier: str | None = None) -> list[ToolSpec]:
    """The specs an agent may call. Order follows ``enabled_tools``. Unknown
    tool names are skipped (forward-compat: a portal may toggle a tool a later
    deploy registers)."""
    out: list[ToolSpec] = []
    seen: set[str] = set()
    for name in enabled_tools:
        if name in seen:
            continue
        spec = TOOL_REGISTRY.get(name)
        if spec is None or not _tier_ok(spec, tier):
            continue
        seen.add(name)
        out.append(spec)
    return out


def anthropic_tool_params(specs: list[ToolSpec]) -> list[dict[str, Any]]:
    """Render specs into the Anthropic ``tools`` parameter shape."""
    return [
        {"name": s.name, "description": s.description, "input_schema": s.input_schema}
        for s in specs
    ]
