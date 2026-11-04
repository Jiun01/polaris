"""A0 agent runtime — registry, tool-use loop, config, backward-compat.

The loop is exercised with a scripted fake ``llm.generate_with_tools`` so no
network is hit; the real tools run against a tiny fake Ports object.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from polaris_api.services import llm
from polaris_api.services.agent.config import load_agent_config
from polaris_api.services.agent.loop import run_agent_reply
from polaris_api.services.agent.tools import (
    TOOL_REGISTRY,
    anthropic_tool_params,
    tools_for,
)
from polaris_api.services.agent.tools.base import ToolContext
from polaris_api.services.llm import ToolCall, ToolTurn

from tests.conftest import make_mock_anthropic_message

# ----------------------------------------------------------------------
# Registry + tools_for (the hard guardrail)
# ----------------------------------------------------------------------


def test_registry_has_the_four_a0_tools() -> None:
    assert {"search_knowledge", "lookup_products", "handoff_to_human", "capture_lead"} <= set(
        TOOL_REGISTRY
    )


def test_tools_for_only_returns_enabled() -> None:
    specs = tools_for(["search_knowledge"])
    assert [s.name for s in specs] == ["search_knowledge"]


def test_tools_for_excludes_disabled_and_unknown() -> None:
    # A disabled tool is simply absent — the hard guardrail.
    assert tools_for([]) == []
    assert tools_for(["does_not_exist"]) == []
    names = [s.name for s in tools_for(["capture_lead", "ghost", "capture_lead"])]
    assert names == ["capture_lead"]  # unknown skipped, dupes collapsed


def test_anthropic_tool_params_shape() -> None:
    params = anthropic_tool_params(tools_for(["search_knowledge"]))
    assert params[0]["name"] == "search_knowledge"
    assert "description" in params[0]
    assert params[0]["input_schema"]["type"] == "object"


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _fake_chunk(content: str) -> Any:
    return SimpleNamespace(content=content)


def _fake_ports(rag_hits: list[Any]) -> Any:
    async def _search(*, query: str, tenant_id: Any, top_k: int = 5) -> list[Any]:
        return rag_hits

    return SimpleNamespace(rag=SimpleNamespace(search=_search))


def _ctx(ports: Any) -> ToolContext:
    return ToolContext(
        tenant_id=uuid4(),
        conversation_id=uuid4(),
        channel="whatsapp",
        channel_user_id="60123",
        customer_name="Aira",
        last_user_message="what time you open?",
        conn=SimpleNamespace(),  # unused by search_knowledge
        ports=ports,
    )


def _turn(text: str = "", calls: tuple[ToolCall, ...] = ()) -> ToolTurn:
    stop = "tool_use" if calls else "end_turn"
    return ToolTurn(
        stop_reason=stop,
        text=text,
        tool_calls=calls,
        raw_assistant_content=[{"type": "text", "text": text}],
        usage=llm.UsageMetrics(input_tokens=1, output_tokens=1),
    )


# ----------------------------------------------------------------------
# The loop
# ----------------------------------------------------------------------


async def test_loop_calls_tool_then_returns_text(monkeypatch: pytest.MonkeyPatch) -> None:
    ports = _fake_ports([_fake_chunk("We open 9am-9pm daily.")])
    ctx = _ctx(ports)

    script = iter(
        [
            _turn(calls=(ToolCall(id="t1", name="search_knowledge", input={"query": "hours"}),)),
            _turn(text="We're open 9am to 9pm every day."),
        ]
    )

    async def fake_gwt(**_kwargs: Any) -> ToolTurn:
        return next(script)

    monkeypatch.setattr(llm, "generate_with_tools", fake_gwt)

    result = await run_agent_reply(
        ctx,
        enabled_tools=["search_knowledge"],
        max_tool_iterations=5,
        system_prompt="sys",
        conversation_history=[],
    )
    assert result.envelope.content == "We're open 9am to 9pm every day."
    assert result.tools_invoked == ("search_knowledge",)


async def test_loop_reports_unknown_tool_as_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A tool the model names but that isn't enabled is reported back as an
    error result, and the loop still completes with a reply."""
    ctx = _ctx(_fake_ports([]))
    script = iter(
        [
            _turn(calls=(ToolCall(id="t1", name="not_enabled", input={}),)),
            _turn(text="Let me help directly."),
        ]
    )

    async def fake_gwt(**_kwargs: Any) -> ToolTurn:
        return next(script)

    monkeypatch.setattr(llm, "generate_with_tools", fake_gwt)
    result = await run_agent_reply(
        ctx,
        enabled_tools=["search_knowledge"],
        max_tool_iterations=5,
        system_prompt="sys",
        conversation_history=[],
    )
    assert result.envelope.content == "Let me help directly."
    assert result.tools_invoked == ()  # not_enabled was never a real spec


async def test_loop_respects_iteration_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the model keeps calling tools, the cap triggers one final no-tools
    call that returns a plain reply (the loop never runs unbounded)."""
    ports = _fake_ports([_fake_chunk("info")])
    ctx = _ctx(ports)
    calls_seen = {"n": 0}

    async def fake_gwt(*, tools: list[Any], **_kwargs: Any) -> ToolTurn:
        calls_seen["n"] += 1
        if tools:  # still offering tools → keep asking for a tool
            return _turn(calls=(ToolCall(id=f"t{calls_seen['n']}", name="search_knowledge", input={}),))
        return _turn(text="Final fallback reply.")  # the no-tools call

    monkeypatch.setattr(llm, "generate_with_tools", fake_gwt)
    result = await run_agent_reply(
        ctx,
        enabled_tools=["search_knowledge"],
        max_tool_iterations=2,
        system_prompt="sys",
        conversation_history=[],
    )
    assert result.envelope.content == "Final fallback reply."
    # 2 capped iterations (each offering tools) + 1 final no-tools call.
    assert calls_seen["n"] == 3


# ----------------------------------------------------------------------
# ADR-031 model-tier routing (audit Batch D / D4)
# ----------------------------------------------------------------------


async def test_loop_forwards_tier_to_generate_with_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``tier`` passed into ``run_agent_reply`` reaches ``generate_with_tools``
    as ``model_tier`` on the normal (tool-use) call path."""
    ctx = _ctx(_fake_ports([]))
    seen_tiers: list[Any] = []

    async def fake_gwt(*, model_tier: Any = "simple", **_kwargs: Any) -> ToolTurn:
        seen_tiers.append(model_tier)
        return _turn(text="ok")

    monkeypatch.setattr(llm, "generate_with_tools", fake_gwt)
    await run_agent_reply(
        ctx,
        enabled_tools=["search_knowledge"],
        max_tool_iterations=5,
        system_prompt="sys",
        conversation_history=[],
        tier="hard",
    )
    assert seen_tiers == ["hard"]


async def test_loop_forwards_tier_on_iteration_cap_fallback_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``tier`` also reaches the iteration-cap fallback's no-tools call."""
    ctx = _ctx(_fake_ports([]))
    seen_tiers: list[Any] = []

    async def fake_gwt(*, tools: list[Any], model_tier: Any = "simple", **_kwargs: Any) -> ToolTurn:
        seen_tiers.append(model_tier)
        if tools:
            return _turn(calls=(ToolCall(id="t1", name="search_knowledge", input={}),))
        return _turn(text="Final fallback reply.")

    monkeypatch.setattr(llm, "generate_with_tools", fake_gwt)
    await run_agent_reply(
        ctx,
        enabled_tools=["search_knowledge"],
        max_tool_iterations=1,
        system_prompt="sys",
        conversation_history=[],
        tier="hard",
    )
    # One capped tool-offering call + one no-tools fallback call, both "hard".
    assert seen_tiers == ["hard", "hard"]


async def test_loop_defaults_tier_to_simple_when_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ``tier`` passed (the 3 pre-D4 call sites above) preserves the
    existing default behaviour: ``model_tier="simple"``."""
    ctx = _ctx(_fake_ports([]))
    seen_tiers: list[Any] = []

    async def fake_gwt(*, model_tier: Any = "simple", **_kwargs: Any) -> ToolTurn:
        seen_tiers.append(model_tier)
        return _turn(text="ok")

    monkeypatch.setattr(llm, "generate_with_tools", fake_gwt)
    await run_agent_reply(
        ctx,
        enabled_tools=["search_knowledge"],
        max_tool_iterations=5,
        system_prompt="sys",
        conversation_history=[],
    )
    assert seen_tiers == ["simple"]


async def test_hard_tier_resolves_to_sonnet_model(mock_anthropic: Any) -> None:
    """End-to-end through the real ``generate_with_tools``: a "hard" tier
    resolves to ``settings.llm_sonnet_model`` on the mocked Anthropic call —
    the same escalation the classic path gets via ``_model_for_intent``."""
    from polaris_api.config import get_settings

    mock_anthropic.messages.create.return_value = make_mock_anthropic_message(
        text="Sonnet-quality reply."
    )
    turn = await llm.generate_with_tools(
        system_prompt="sys",
        messages=[{"role": "user", "content": "hi"}],
        tools=[],
        model_tier="hard",
    )
    assert turn.text == "Sonnet-quality reply."
    settings = get_settings()
    call_kwargs = mock_anthropic.messages.create.call_args.kwargs
    assert call_kwargs["model"] == settings.llm_sonnet_model


# ----------------------------------------------------------------------
# Config + backward-compat
# ----------------------------------------------------------------------


class _FakeConn:
    def __init__(self, settings: Any) -> None:
        self._settings = settings

    async def fetchrow(self, _sql: str, _tenant_id: Any) -> dict[str, Any] | None:
        return {"settings": self._settings}


async def test_config_defaults_disabled_when_absent() -> None:
    cfg = await load_agent_config(_FakeConn({}), uuid4())
    assert cfg.enabled is False
    assert cfg.enabled_tools == []
    assert cfg.max_tool_iterations == 5


async def test_config_parses_enabled_blob() -> None:
    cfg = await load_agent_config(
        _FakeConn({"agent_config": {"enabled": True, "enabled_tools": ["search_knowledge"]}}),
        uuid4(),
    )
    assert cfg.enabled is True
    assert cfg.enabled_tools == ["search_knowledge"]


async def test_config_malformed_blob_falls_back_to_disabled() -> None:
    cfg = await load_agent_config(_FakeConn({"agent_config": "not-a-dict"}), uuid4())
    assert cfg.enabled is False


async def test_try_run_agent_returns_none_when_disabled() -> None:
    """The backward-compat guarantee: disabled agent → None → pipeline falls
    through to the classic path unchanged."""
    from polaris_api.services.agent.pipeline_integration import try_run_agent
    from polaris_shared_types.incoming_message import IncomingMessage

    incoming = SimpleNamespace(
        tenant_id=uuid4(),
        body="hi",
        sender=SimpleNamespace(channel_user_id="60123", channel_user_name="A"),
    )
    result = await try_run_agent(
        conn=_FakeConn({}),  # agent_config absent → disabled
        redis=None,  # never reached (returns before building ports)
        meta_client=None,
        incoming=incoming,  # type: ignore[arg-type]
        conversation_id=uuid4(),
        channel="whatsapp",
    )
    assert result is None
    _ = IncomingMessage  # imported to assert the module loads cleanly
