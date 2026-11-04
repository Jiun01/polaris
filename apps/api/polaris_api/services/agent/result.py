"""Agent reply result shape (kept separate to avoid import cycles)."""

from __future__ import annotations

from dataclasses import dataclass

from polaris_shared_types import ResponseEnvelope


@dataclass(frozen=True)
class AgentReplyResult:
    """What the agent loop returns: the reply envelope + which tools fired."""

    envelope: ResponseEnvelope
    tools_invoked: tuple[str, ...]
