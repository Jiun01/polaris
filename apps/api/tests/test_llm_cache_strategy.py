"""Load-bearing cache-strategy tests for ``services/llm.py`` — Phase 4 PR-1.

Per deep-dive §9.1 + the 2026-05-15 finance-billing-ops audit gap A,
the Polaris LLM wrapper MUST attach ``cache_control={"type":"ephemeral",
"ttl":...}`` ONLY to the system prompt block. Any other location burns
the cache slot on transient content (RAG context, user message, history)
and either silently fails to hit cache OR pollutes Anthropic's slot
across calls.

This file is the lint-style regression guard. If any of these tests
fail, the PR must be blocked from merge — the wrong cache strategy
isn't a "tweak later" item, it's a cost-per-message regression that
compounds across every reply.
"""

from __future__ import annotations

import ast
import pathlib
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

# Async runtime tests below decorate themselves with pytest.mark.asyncio;
# the AST-level lint tests stay sync (no event loop needed). Module-level
# pytestmark would apply to both and trigger PytestWarning on the lint
# tests, so we apply individually instead.


def _find_cache_control_in_code(path: pathlib.Path) -> list[tuple[int, str]]:
    """Return (line, kind) for every AST-level cache_control reference.

    Catches the failure modes the deep-dive §9.1 invariant cares about:
      * dict literals with a key of "cache_control" (`{"cache_control": ...}`)
      * function-call keyword args named cache_control (`f(cache_control=...)`)
      * attribute reads / assignments to `.cache_control`
    Ignores string content inside docstrings, comments, and unrelated
    string literals — those are harmless documentation, not behavior.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return []
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for key in node.keys:
                if isinstance(key, ast.Constant) and key.value == "cache_control":
                    hits.append((node.lineno, "dict_key"))
        elif isinstance(node, ast.keyword):
            if node.arg == "cache_control":
                # ast.keyword has no lineno; use the parent call's line
                # via its value node.
                hits.append((node.value.lineno, "kwarg"))
        elif isinstance(node, ast.Attribute):
            if node.attr == "cache_control":
                hits.append((node.lineno, "attribute"))
    return hits


# ----------------------------------------------------------------------
# Runtime guards — observe actual SDK kwargs at the boundary
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_system_block_carries_cache_control(mock_anthropic: MagicMock) -> None:
    """The single system TextBlockParam has cache_control set."""
    from polaris_api.services import llm

    await llm.classify_intent("hello")

    call_kwargs = mock_anthropic.messages.create.call_args.kwargs
    assert "system" in call_kwargs
    system = call_kwargs["system"]
    assert isinstance(system, list)
    assert len(system) == 1
    sb = system[0]
    assert sb["type"] == "text"
    assert sb["cache_control"] == {"type": "ephemeral", "ttl": "5m"}


@pytest.mark.asyncio
async def test_no_toplevel_cache_control_kwarg(mock_anthropic: MagicMock) -> None:
    """messages.create must NOT receive a top-level cache_control kwarg.

    The SDK's top-level ``cache_control`` auto-applies the marker to
    the LAST cacheable block in the request — which for our shape is
    the user message containing RAG context. Catastrophic for cache
    hit rate.
    """
    from polaris_api.services import llm

    await llm.generate_response(
        tenant_id=uuid4(),
        rag_context="some chunks here",
        conversation_history=[],
        user_message="hi",
        intent="simple",
    )

    call_kwargs = mock_anthropic.messages.create.call_args.kwargs
    assert "cache_control" not in call_kwargs, (
        "Top-level cache_control kwarg would auto-apply to RAG block — "
        "use per-block cache_control on the system text instead."
    )


@pytest.mark.asyncio
async def test_user_message_blocks_have_no_cache_control(
    mock_anthropic: MagicMock,
) -> None:
    """Walk every user-message content block; assert no cache_control."""
    from polaris_api.services import llm

    await llm.generate_response(
        tenant_id=uuid4(),
        rag_context="<knowledge_chunk id='1'>fact</knowledge_chunk>",
        conversation_history=[
            {"role": "user", "content": "prior question"},
            {"role": "assistant", "content": "prior answer"},
        ],
        user_message="follow-up",
        intent="medium",
    )

    call_kwargs = mock_anthropic.messages.create.call_args.kwargs
    messages = call_kwargs["messages"]
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                assert "cache_control" not in block, (
                    f"cache_control leaked into a message content block: {block}"
                )


@pytest.mark.asyncio
async def test_rag_context_appears_after_history(
    mock_anthropic: MagicMock,
) -> None:
    """RAG context lands in the trailing user message, not as its own role.

    Putting RAG context as a system-role message would mean it's
    cached (last cacheable system block) — same outcome as
    top-level cache_control. Putting it as an early user message is
    visually clean but semantically wrong (the model can't tell it
    apart from a prior user turn).

    Our shape: history first, then ONE final user message whose content list is
    [<rag>, <user_msg>, <language_nudge>]. The trailing language nudge (v1.0.42)
    is the recency lever that makes the model honor a mid-conversation language
    switch. This test asserts that shape.
    """
    from polaris_api.services import llm

    await llm.generate_response(
        tenant_id=uuid4(),
        rag_context="<knowledge_chunk id='1'>fact</knowledge_chunk>",
        conversation_history=[
            {"role": "user", "content": "prior question"},
            {"role": "assistant", "content": "prior answer"},
        ],
        user_message="follow-up",
        intent="medium",
    )

    messages = mock_anthropic.messages.create.call_args.kwargs["messages"]
    # Last message is user; its content is a list of blocks: RAG context first,
    # then the user message, then the per-turn language nudge (LAST = recency).
    last = messages[-1]
    assert last["role"] == "user"
    assert isinstance(last["content"], list)
    assert len(last["content"]) == 3
    assert last["content"][0]["text"].startswith("<knowledge_chunk")
    assert last["content"][1]["text"] == "follow-up"
    assert last["content"][2]["text"] == llm._language_nudge("en")


# ----------------------------------------------------------------------
# Lint guard — cache_control must not appear outside services/llm.py
# ----------------------------------------------------------------------


def test_codebase_uses_cache_control_only_in_llm_py() -> None:
    """AST guard: ``cache_control`` as a key/kwarg/attr appears only in llm.py.

    Walks every .py file under ``polaris_api/`` and checks AST nodes
    (dict keys, function-call kwargs, attribute access) for
    ``cache_control``. Ignores docstring / comment mentions — those
    are documentation, not behavior. If a future PR adds
    ``{"cache_control": ...}`` or ``foo(cache_control=...)`` outside
    llm.py, this fails loudly.
    """
    api_pkg = (
        pathlib.Path(__file__).parent.parent / "polaris_api"
    ).resolve()
    offenders: list[str] = []
    for path in api_pkg.rglob("*.py"):
        if path.name == "llm.py":
            continue
        if "__pycache__" in path.parts:
            continue
        hits = _find_cache_control_in_code(path)
        if hits:
            rel = str(path.relative_to(api_pkg.parent))
            for line, kind in hits:
                offenders.append(f"{rel}:{line} ({kind})")
    assert not offenders, (
        "cache_control found at AST level outside services/llm.py — "
        f"caching strategy must live in ONE place per deep-dive §9.1. "
        f"Offenders: {offenders}"
    )


def test_no_topkwarg_cache_control_in_llm_py() -> None:
    """AST guard: ``cache_control`` never appears as a function-call kwarg.

    All cache_control usage must be the per-block dict construction
    (``{"cache_control": {...}}`` inside a TextBlockParam dict). A
    bare ``cache_control=`` kwarg means we're passing it as a
    top-level kwarg to ``messages.create`` — the dangerous
    auto-apply-to-last-block path.
    """
    llm_path = (
        pathlib.Path(__file__).parent.parent
        / "polaris_api"
        / "services"
        / "llm.py"
    ).resolve()
    kwarg_hits = [
        (line, kind)
        for line, kind in _find_cache_control_in_code(llm_path)
        if kind == "kwarg"
    ]
    assert not kwarg_hits, (
        "cache_control= kwarg detected in llm.py — that's the SDK's "
        "auto-apply-to-last-block path which lands on RAG context. Use "
        f"per-block dict syntax instead. Offending lines: {kwarg_hits}"
    )
