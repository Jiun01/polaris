"""Unit tests for the Agent-screen prompt introspection helpers.

These cover the read-only prompt surface added to ``GET /api/tenant/settings``
for the Agent screen: the rendered default persona (so the portal can prefill
the persona editor instead of showing a blank box) and the non-overridable
guardrail blocks. Pure functions / constants — no DB or HTTP needed.
"""

from __future__ import annotations

from polaris_api.routers.tenant_settings import (
    _AGENT_GUARDRAILS,
    _render_default_prompt,
)


def test_render_default_prompt_substitutes_tenant_names() -> None:
    out = _render_default_prompt("Aria", "Acme Salon")
    assert "Aria" in out
    assert "Acme Salon" in out
    # Sanity: it is the customer-service persona body, not an empty string.
    assert "customer service assistant" in out.lower()


def test_render_default_prompt_falls_back_to_platform_defaults() -> None:
    out = _render_default_prompt(None, None)
    assert "Pol" in out
    assert "Polaris" in out


def test_render_default_prompt_treats_blank_as_unset() -> None:
    # asyncpg hands back the JSON value verbatim; a stored empty string should
    # still fall back rather than render "You are , the assistant for ".
    out = _render_default_prompt("", "")
    assert "Pol" in out
    assert "Polaris" in out


def test_agent_guardrails_are_present_and_nonempty() -> None:
    assert len(_AGENT_GUARDRAILS) == 4
    titles = {g.title for g in _AGENT_GUARDRAILS}
    assert "Safety rules (always enforced)" in titles
    for block in _AGENT_GUARDRAILS:
        assert block.title.strip()
        assert block.body.strip()
        # Bodies are stripped — no leading/trailing whitespace bleed.
        assert block.body == block.body.strip()
