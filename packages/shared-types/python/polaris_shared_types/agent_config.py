"""Agent tool-surface config — Pydantic SSOT (ADR-063).

Per-tenant configuration for the tool-calling agent reply path. Stored in
``tenants.settings.agent_config`` JSONB (mirrors ``booking_config`` /
``followup_config``). Default-disabled: a tenant with no ``agent_config`` (or
``enabled=false``) gets the exact classic classify→rag→generate pipeline.

The ``TOOL_CATALOG`` is the platform-owned list the portal renders as toggles.
The actual tool *descriptions* and JSON-schema *parameters* live in
``services/agent/tools/`` (code, platform-owned) — the catalog here only carries
the human-facing label/blurb so portal and API agree on the toggle list (defined
once, generated to TS, never hand-rolled — per the v1.0.23–26 channel-narrowness
incidents).
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator

_FORBID = ConfigDict(extra="forbid")


# Stable tool ids. MUST match the @register_tool names in
# ``polaris_api/services/agent/tools/``. Adding a tool = append here + register
# the handler; the portal toggle list is driven from TOOL_CATALOG below.
AGENT_TOOL_IDS: tuple[str, ...] = (
    "search_knowledge",
    "lookup_products",
    "lookup_promotions",
    "handoff_to_human",
    "capture_lead",
)

# Retrieval tools that ship ON by default for new tenants (WS2) — product +
# promo catalog lookups. Single source referenced by admin provisioning AND the
# backfill script so "which tools are default-on" never drifts across the two.
DEFAULT_RETRIEVAL_TOOLS: tuple[str, ...] = ("lookup_products", "lookup_promotions")


class AgentToolInfo(BaseModel):
    """One catalog entry — what the portal renders as a toggle."""

    model_config = _FORBID

    id: str
    label: str
    description: str


# Platform-owned catalog (label + human blurb for the portal toggle list).
TOOL_CATALOG: tuple[AgentToolInfo, ...] = (
    AgentToolInfo(
        id="search_knowledge",
        label="Answer from knowledge base",
        description="Let the assistant look up answers in your uploaded knowledge base.",
    ),
    AgentToolInfo(
        id="lookup_products",
        label="Look up products",
        description="Let the assistant find and describe items from your product catalog.",
    ),
    AgentToolInfo(
        id="lookup_promotions",
        label="Look up promotions",
        description="Let the assistant share your current promotions and deals when a customer asks.",
    ),
    AgentToolInfo(
        id="handoff_to_human",
        label="Hand off to a human",
        description="Let the assistant escalate the conversation to your team when needed.",
    ),
    AgentToolInfo(
        id="capture_lead",
        label="Capture lead details",
        description="Let the assistant save a customer's name and contact details to your audience.",
    ),
)


class AgentConfig(BaseModel):
    """Per-tenant agent config. Lives in ``tenants.settings.agent_config``."""

    model_config = _FORBID

    enabled: bool = False
    instructions: str | None = Field(default=None, max_length=4000)
    enabled_tools: list[str] = Field(default_factory=list)
    max_tool_iterations: int = Field(default=5, ge=1, le=10)


# --------------------------------------------------------------------
# Persona blocks (Agent-screen redesign 2026-06-21)
# --------------------------------------------------------------------
#
# The portal "Bot persona" editor replaces the single raw-XML override textarea
# with a grid of named blocks. Each block has a human ``display_name`` (e.g.
# "Product Guidance") and a machine ``slug`` (e.g. ``product_guidance``) that
# becomes the XML tag the model sees: ``<product_guidance>…</product_guidance>``.
# Stored as a list under ``tenants.settings.persona_blocks`` (JSONB) — same
# precedent as ``agent_config`` / ``email_draft_prompt``, so no new column.

# Platform-owned XML tags a tenant block must never shadow or inject. Safety
# guards are still appended AFTER the persona body (recency wins), so this is
# defense-in-depth on top of that ordering.
RESERVED_BLOCK_SLUGS: frozenset[str] = frozenset(
    {
        "non_negotiable_rules",
        "format",
        "email_draft",
        "using_tools",
        "runtime_language",
        "knowledge_chunk",
        "tenant_instructions",
    }
)

_SLUG_RE = r"^[a-z0-9_]+$"

# Bound mirrors the per-block UI: 10 blocks × 2000 chars is well within the model
# context budget and a sane upper bound (the old single override was 4000).
MAX_PERSONA_BLOCKS = 10


class PersonaBlock(BaseModel):
    """One named persona section. ``display_name`` is human; ``slug`` is the tag."""

    model_config = _FORBID

    display_name: str = Field(..., min_length=1, max_length=60)
    slug: str = Field(..., min_length=1, max_length=50, pattern=_SLUG_RE)
    body: str = Field(..., min_length=1, max_length=2000)

    @field_validator("slug")
    @classmethod
    def _reject_reserved(cls, v: str) -> str:
        if v in RESERVED_BLOCK_SLUGS:
            raise ValueError(f"slug '{v}' is reserved by the platform")
        return v


def slugify_block_name(name: str) -> str:
    """Derive a stable snake_case slug from a human block name.

    ``"Product Guidance!"`` -> ``"product_guidance"``. Empty/punctuation-only
    names fall back to ``"block"`` so the result always matches ``_SLUG_RE``. A
    name that slugifies onto a reserved tag (e.g. "Non Negotiable Rules") gets a
    ``custom_`` prefix so the result is never itself reserved.
    """
    s = re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")
    s = re.sub(r"_+", "_", s) or "block"
    if s in RESERVED_BLOCK_SLUGS:
        s = f"custom_{s}"
    return s


# Palette the portal renders under "+ Add block" — one-click starter sections.
RECOMMENDED_PERSONA_BLOCKS: tuple[PersonaBlock, ...] = (
    PersonaBlock(
        display_name="Product Guidance",
        slug="product_guidance",
        body=(
            "How to talk about our products: highlight the key benefits and help "
            "customers pick the right option. Never quote a price that isn't in "
            "the knowledge base."
        ),
    ),
    PersonaBlock(
        display_name="Promotions",
        slug="promotions",
        body=(
            "Mention current promotions only when they're relevant to what the "
            "customer is asking about. Never invent a discount or offer."
        ),
    ),
    PersonaBlock(
        display_name="FAQ Handling",
        slug="faq",
        body=(
            "For common questions, answer directly and concisely using the "
            "knowledge base. Keep it short and conversational."
        ),
    ),
    PersonaBlock(
        display_name="Shipping & Delivery",
        slug="shipping",
        body=(
            "Explain shipping options and timing only from documented policy. "
            "Never promise a specific delivery date or stock level."
        ),
    ),
    PersonaBlock(
        display_name="Returns & Refunds",
        slug="returns",
        body=(
            "Walk customers through the documented returns process. For complex "
            "or undocumented refund approvals, hand off to a human."
        ),
    ),
)
