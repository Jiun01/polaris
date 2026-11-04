"""Tenant Settings router — Phase 8 PR-4.

Mounted at ``/api/tenant/settings``.

Two endpoints:

  * ``GET  /``  — read the current tenant's editable settings:
                  bot persona, persona tone, business hours, handoff
                  messages (in/after hours), AI disclosure flag.
  * ``PATCH /`` — partial update; clients send any subset of the
                  editable fields. Validation is enforced via Pydantic
                  shapes (length caps, business-hours shape, persona
                  tone allow-list).

Email change is INTENTIONALLY NOT in this router — it requires a
multi-step verification flow (mail-to-new + notice-to-old + confirm
endpoint + new token table) that grows the PR scope. The Settings UI
surface ships in PR-4; email change ships as a follow-up.

Auth: standard portal-JWT path via ``get_tenant_context``. RLS scopes
the underlying ``tenants`` row by ``app.current_tenant_id`` GUC.
"""

from __future__ import annotations

import json
import logging
from typing import Annotated, Literal, cast

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, status
from polaris_shared_types import (
    MAX_PERSONA_BLOCKS,
    TOOL_CATALOG,
    AgentConfig,
    AgentToolInfo,
    PersonaBlock,
)
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from polaris_api.deps.tenant import (
    TenantContext,
    get_request_conn,
    get_tenant_context,
)

# Read-only introspection of the prompt-assembly layers so the portal can show
# the EFFECTIVE persona (the rendered default when no override is stored) and
# the non-overridable guardrails. Imported as constants — this router never
# edits the prompt module, keeping the agent-screen change isolated from the
# messaging/LLM rehaul that may be touching ``_llm_prompts``.
from polaris_api.services._llm_prompts import (  # noqa: PLC2701 — single-source introspection
    _AGENT_TOOL_RULES,
    _CHAT_FORMAT_RULES,
    _DEFAULT_ASSISTANT_NAME,
    _DEFAULT_COMPANY_NAME,
    _RAG_INJECTION_GUARD,
    _SAFETY_GUARDS,
    _V1_TEMPLATE,
    coerce_persona_blocks,
    default_persona_blocks,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["tenant-settings"])

_FORBID = ConfigDict(extra="forbid")

# Persona tone enum — matches portal_design_prompt.md §13 + the values
# baked into the bot's system prompt assembly (Phase 4).
PersonaTone = Literal["professional", "friendly", "casual", "formal"]


class GuardrailBlock(BaseModel):
    """One non-overridable prompt layer, surfaced read-only on the Agent screen.

    These are the safety / format / tool / RAG guards that wrap every persona
    (``_llm_prompts.assemble_system_prompt``). A tenant can read them so they
    understand exactly what the bot is always told, but cannot edit them.
    """

    model_config = _FORBID

    title: str
    body: str


# Built once at import from the prompt-assembly constants (single source). The
# ``.strip()`` drops the trailing line-continuation whitespace so the portal
# renders clean monospace blocks.
_AGENT_GUARDRAILS: tuple[GuardrailBlock, ...] = (
    GuardrailBlock(
        title="Safety rules (always enforced)", body=_SAFETY_GUARDS.strip()
    ),
    GuardrailBlock(title="Chat formatting", body=_CHAT_FORMAT_RULES.strip()),
    GuardrailBlock(title="Tool use", body=_AGENT_TOOL_RULES.strip()),
    GuardrailBlock(
        title="Knowledge-base safety guard", body=_RAG_INJECTION_GUARD.strip()
    ),
)


def _render_default_prompt(
    assistant_name: str | None, company_name: str | None
) -> str:
    """Render the built-in persona template with the tenant's names.

    Mirrors ``_llm_prompts._base_persona`` for the no-override case (minus the
    runtime ``<language>`` suffix, which is request-time only). Lets the portal
    prefill the persona box with the EFFECTIVE prompt instead of a blank box.
    """
    return _V1_TEMPLATE.format(
        assistant_name=assistant_name or _DEFAULT_ASSISTANT_NAME,
        company_name=company_name or _DEFAULT_COMPANY_NAME,
    )


class BusinessHoursDay(BaseModel):
    """One row in the weekly business-hours grid.

    ``open``/``close`` are 24-hour HH:MM strings. ``closed=true`` means
    the bot has no in-hours window that day; handoffs that fire on a
    closed day fall through to ``handoff_message_after_hours`` per
    ADR-019.
    """

    model_config = _FORBID

    open: str = Field(..., pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    close: str = Field(..., pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    closed: bool = False

    @model_validator(mode="after")
    def _close_after_open(self) -> BusinessHoursDay:
        """Reject a window whose ``close`` is not strictly after ``open``.

        The field patterns already guarantee well-formed ``HH:MM`` strings,
        but nothing stopped a tenant from saving ``open=18:00 close=09:00``
        (QA finding M6) — which silently corrupts the Phase-4 in-hours vs
        after-hours handoff routing. Closed days keep placeholder times, so
        the check only applies when the day is open. String compare is safe
        here: zero-padded ``HH:MM`` is lexicographically ordered by time.
        """
        if not self.closed and self.close <= self.open:
            raise ValueError("close must be strictly after open")
        return self


class BusinessHours(BaseModel):
    """Weekly schedule — Mon-Sun keys, mirrored from
    ``tenants.business_hours`` JSONB. Phase 1 documents the shape;
    Phase 4 reads it for handoff routing.
    """

    model_config = _FORBID

    mon: BusinessHoursDay
    tue: BusinessHoursDay
    wed: BusinessHoursDay
    thu: BusinessHoursDay
    fri: BusinessHoursDay
    sat: BusinessHoursDay
    sun: BusinessHoursDay


class TenantSettings(BaseModel):
    """Read shape for ``GET /api/tenant/settings``."""

    model_config = _FORBID

    tenant_id: str
    company_name: str
    timezone: str
    default_country_code: str
    system_prompt: str = Field(..., max_length=4000)
    # The rendered built-in persona for THIS tenant (names substituted). The
    # portal prefills the persona editor with ``system_prompt`` when an override
    # is stored, else this — so the box shows the effective prompt, never blank.
    default_system_prompt: str = ""
    # Agent-screen redesign 2026-06-21: the tenant's saved persona blocks (empty
    # until they save the block editor) + the seeded default blocks (names
    # substituted) the portal prefills the editor with. Stored in
    # tenants.settings.persona_blocks JSONB — same precedent as agent_config.
    persona_blocks: list[PersonaBlock] = Field(default_factory=list)
    default_persona_blocks: list[PersonaBlock] = Field(default_factory=list)
    persona_tone: PersonaTone
    business_hours: BusinessHours
    handoff_message_in_hours: str = Field(..., max_length=2000)
    handoff_message_after_hours: str = Field(..., max_length=2000)
    ai_disclosure_enabled: bool
    plan_tier: str
    retention_message_days: int
    # Email channel M2 (RD #11): per-tenant email-draft tone prompt, stored in
    # tenants.settings JSONB. Null when unset.
    email_draft_prompt: str | None = None
    # Agent tool-surface (ADR-063): per-tenant agent config + the platform tool
    # catalog the portal renders as toggles. Stored in tenants.settings JSONB.
    agent_config: AgentConfig = Field(default_factory=AgentConfig)
    agent_tool_catalog: list[AgentToolInfo] = Field(default_factory=lambda: list(TOOL_CATALOG))
    # Non-overridable prompt layers, read-only. Lets the Agent screen show the
    # full set of rules the bot is always given (safety / format / tool / RAG).
    agent_guardrails: list[GuardrailBlock] = Field(
        default_factory=lambda: list(_AGENT_GUARDRAILS)
    )


class TenantSettingsUpdate(BaseModel):
    """PATCH shape — every field optional.

    A field's absence means 'don't touch it'; the SQL update builds
    a COALESCE / dynamic SET clause so missing keys don't overwrite
    existing values with NULL.
    """

    model_config = _FORBID

    system_prompt: str | None = Field(None, max_length=4000)
    persona_tone: PersonaTone | None = None
    business_hours: BusinessHours | None = None
    handoff_message_in_hours: str | None = Field(None, max_length=2000)
    handoff_message_after_hours: str | None = Field(None, max_length=2000)
    ai_disclosure_enabled: bool | None = None
    # Email channel M2 (RD #11): per-tenant prompt shaping the bot's email
    # draft tone. Persisted to tenants.settings JSONB (read by
    # load_tenant_snapshot), not a column.
    email_draft_prompt: str | None = Field(None, max_length=4000)
    # Agent tool-surface (ADR-063): whole-object replace into settings JSONB.
    agent_config: AgentConfig | None = None
    # Agent-screen redesign 2026-06-21: the persona block list. Whole-list replace
    # into settings.persona_blocks JSONB. Per-block validation (slug pattern,
    # reserved-slug + length) is enforced by PersonaBlock; here we cap the count
    # and reject duplicate slugs (two blocks sharing a tag is ambiguous).
    persona_blocks: list[PersonaBlock] | None = Field(None, max_length=MAX_PERSONA_BLOCKS)

    @field_validator("persona_blocks")
    @classmethod
    def _no_duplicate_slugs(
        cls, v: list[PersonaBlock] | None
    ) -> list[PersonaBlock] | None:
        if v is None:
            return v
        slugs = [b.slug for b in v]
        if len(slugs) != len(set(slugs)):
            raise ValueError("persona_blocks contains duplicate slugs")
        return v


_GET_SQL = """
SELECT
    t.id::text                     AS tenant_id,
    t.slug AS company_name,
    COALESCE(t.timezone, 'Asia/Kuala_Lumpur') AS timezone,
    COALESCE(t.default_country_code, 'MY')   AS default_country_code,
    COALESCE(t.system_prompt, '')            AS system_prompt,
    COALESCE(t.persona_tone, 'professional') AS persona_tone,
    COALESCE(t.business_hours, '{}'::jsonb)  AS business_hours,
    COALESCE(t.handoff_message_in_hours,
             'Connecting you with a teammate. They''ll be with you shortly.') AS handoff_message_in_hours,
    COALESCE(t.handoff_message_after_hours,
             'Our team is offline. They''ll reach out first thing on the next business day.') AS handoff_message_after_hours,
    COALESCE(t.ai_disclosure_enabled, true) AS ai_disclosure_enabled,
    COALESCE(tq.plan_tier, 'starter')        AS plan_tier,
    COALESCE(tq.retention_message_days, 90)  AS retention_message_days,
    t.settings->>'email_draft_prompt'        AS email_draft_prompt,
    t.settings->>'assistant_name'            AS persona_assistant_name,
    t.settings->>'company_name'              AS persona_company_name,
    t.settings->'agent_config'               AS agent_config,
    t.settings->'persona_blocks'             AS persona_blocks
  FROM tenants t
  LEFT JOIN tenant_quotas tq ON tq.tenant_id = t.id
 WHERE t.id = $1
"""

# 7 weekday keys, Mon-Sun, used as the default when business_hours is
# empty JSONB. Matches the portal grid's row order.
_DEFAULT_BUSINESS_HOURS: dict[str, dict[str, object]] = {
    day: {"open": "09:00", "close": "18:00", "closed": day in ("sat", "sun")}
    for day in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
}


def _coerce_business_hours(raw: object) -> BusinessHours:
    """Merge stored JSONB with the default skeleton so partial rows
    don't make the response shape ragged.

    Per-day dicts: any unknown key on a known day passes through to
    BusinessHoursDay, where ConfigDict(extra='forbid') will reject it
    loudly — that's the desired drift signal rather than a silent drop.

    Top-level unknown keys (e.g. a future 'timezone_override' added by
    a schema bump on tenants.business_hours): also passed through so
    BusinessHours's extra='forbid' surfaces the migration gap
    explicitly. If you intentionally extend the JSONB contract, update
    BusinessHours + bump this code in lockstep.
    """
    if not isinstance(raw, dict) or not raw:
        return BusinessHours.model_validate(_DEFAULT_BUSINESS_HOURS)
    merged: dict[str, object] = dict(_DEFAULT_BUSINESS_HOURS)
    for k, v in raw.items():
        if k in merged and isinstance(v, dict):
            merged[k] = {**cast(dict[str, object], merged[k]), **v}
        else:
            # Forward-compat surface: an unknown top-level key (or a
            # known day passed as a non-dict scalar) reaches
            # model_validate untouched. extra='forbid' on BusinessHours
            # will reject it with a ValidationError → 500 with a clear
            # signal so the migration gap doesn't hide as a silent drop.
            merged[k] = v
    return BusinessHours.model_validate(merged)


@router.get(
    "",
    response_model=TenantSettings,
    summary="Read editable tenant settings",
    responses={
        401: {"description": "missing_bearer_token or invalid_token"},
        403: {"description": "service_suspended (payment_overdue only)"},
        404: {"description": "tenant_not_found"},
    },
)
async def read_settings(
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    conn: Annotated[asyncpg.Connection, Depends(get_request_conn)],
) -> TenantSettings:
    """Read the Settings page payload in one round-trip.

    Defaults are applied at the SQL boundary (`COALESCE`) so a freshly
    provisioned tenant whose admin hasn't customised anything still
    renders the form with sensible starting values rather than a sea
    of empty fields.
    """
    row = await conn.fetchrow(_GET_SQL, ctx.tenant_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "TENANT_NOT_FOUND", "message": "tenant row missing"},
        )
    return TenantSettings(
        tenant_id=row["tenant_id"],
        company_name=row["company_name"],
        timezone=row["timezone"],
        default_country_code=row["default_country_code"],
        system_prompt=row["system_prompt"],
        default_system_prompt=_render_default_prompt(
            row["persona_assistant_name"], row["persona_company_name"]
        ),
        persona_blocks=_coerce_persona_blocks(row["persona_blocks"]),
        default_persona_blocks=default_persona_blocks(
            row["persona_assistant_name"], row["persona_company_name"]
        ),
        persona_tone=cast(PersonaTone, row["persona_tone"]),
        business_hours=_coerce_business_hours(row["business_hours"]),
        handoff_message_in_hours=row["handoff_message_in_hours"],
        handoff_message_after_hours=row["handoff_message_after_hours"],
        ai_disclosure_enabled=bool(row["ai_disclosure_enabled"]),
        plan_tier=row["plan_tier"],
        retention_message_days=int(row["retention_message_days"]),
        email_draft_prompt=row["email_draft_prompt"],
        agent_config=_coerce_agent_config(row["agent_config"]),
    )


def _coerce_agent_config(raw: object) -> AgentConfig:
    """Parse the ``agent_config`` JSONB (asyncpg may hand back a dict or a JSON
    string). Fail-soft to the disabled default so a malformed blob never 500s
    the Settings page."""
    if raw is None:
        return AgentConfig()
    blob: object = raw
    if isinstance(raw, str):
        try:
            blob = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return AgentConfig()
    if not isinstance(blob, dict):
        return AgentConfig()
    try:
        return AgentConfig.model_validate(blob)
    except Exception:  # noqa: BLE001
        return AgentConfig()


def _coerce_persona_blocks(raw: object) -> list[PersonaBlock]:
    """Parse the ``persona_blocks`` JSONB list for the read shape.

    Delegates to the shared runtime helper and normalises ``None`` to ``[]`` so a
    tenant who hasn't saved blocks yet gets an empty list (the portal then seeds
    the editor with ``default_persona_blocks``)."""
    return coerce_persona_blocks(raw) or []


@router.patch(
    "",
    response_model=TenantSettings,
    summary="Partially update tenant settings",
    responses={
        200: {"description": "settings updated (or no-op for empty body)"},
        400: {"description": "validation error on a supplied field"},
        401: {"description": "missing_bearer_token or invalid_token"},
        403: {"description": "service_suspended"},
        404: {"description": "tenant_not_found"},
    },
)
async def update_settings(
    body: TenantSettingsUpdate,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    conn: Annotated[asyncpg.Connection, Depends(get_request_conn)],
) -> TenantSettings:
    """Partial update.

    Builds the SET clause dynamically from supplied fields. ``$n``
    placeholders only — no f-string interpolation of user-controlled
    values. Empty-body PATCH is a no-op that returns the current row
    (idiomatic; lets the portal POST a 'save' without dirty tracking).
    """
    # Collect updates as (kind, target, value). kind 'col' → "target = $N" where
    # target is a hardcoded column literal; kind 'jsonb' → merge target as a key
    # into the settings JSONB. The explicit kind tag (vs. a magic sentinel column
    # name) makes it impossible to misroute a value into the wrong SQL shape. No
    # user-controlled string ever reaches the SQL template — only $N placeholders
    # carry external values.
    updates: list[tuple[str, str, object]] = []

    if body.system_prompt is not None:
        updates.append(("col", "system_prompt", body.system_prompt))
    if body.persona_tone is not None:
        updates.append(("col", "persona_tone", body.persona_tone))
    if body.business_hours is not None:
        # Pass the dict DIRECTLY — the SET builder binds this to `$i::jsonb`
        # (cast_suffix below) and the JSONB codec (pools.py) serialises ONCE.
        # json.dumps(...) would double-encode into a JSONB string, breaking
        # `business_hours->>'...'` reads (see services/audit.py).
        updates.append(("col", "business_hours", body.business_hours.model_dump()))
    if body.handoff_message_in_hours is not None:
        updates.append(
            ("col", "handoff_message_in_hours", body.handoff_message_in_hours)
        )
    if body.handoff_message_after_hours is not None:
        updates.append(
            ("col", "handoff_message_after_hours", body.handoff_message_after_hours)
        )
    if body.ai_disclosure_enabled is not None:
        updates.append(("col", "ai_disclosure_enabled", body.ai_disclosure_enabled))
    if body.email_draft_prompt is not None:
        # Stored in tenants.settings JSONB (where load_tenant_snapshot reads it).
        updates.append(("jsonb", "email_draft_prompt", body.email_draft_prompt))
    if body.agent_config is not None:
        # Whole-object replace into settings.agent_config (ADR-063). The dict is
        # bound to `$i::jsonb` inside jsonb_build_object (kind 'jsonb_obj'); the
        # JSONB codec (pools.py) serialises it ONCE. json.dumps(...) would
        # double-encode into a JSONB string (see services/audit.py).
        updates.append(("jsonb_obj", "agent_config", body.agent_config.model_dump()))
    if body.persona_blocks is not None:
        # Whole-list replace into settings.persona_blocks. The list of dicts rides
        # as a ::jsonb-cast $N (kind 'jsonb_obj'); the JSONB codec serialises it
        # ONCE. json.dumps(...) would double-encode into a JSONB string.
        updates.append(
            (
                "jsonb_obj",
                "persona_blocks",
                [b.model_dump() for b in body.persona_blocks],
            )
        )

    if updates:
        set_parts: list[str] = []
        args: list[object] = []
        # All settings-JSONB keys must merge in ONE `settings = …` assignment —
        # Postgres rejects assigning the same column twice in a single UPDATE, so
        # a PATCH touching e.g. agent_config + persona_blocks would 500 if each
        # got its own SET clause. Collect the key/value pairs and emit one
        # jsonb_build_object covering all of them.
        jsonb_pairs: list[str] = []
        for i, (kind, target, val) in enumerate(updates, start=1):
            if kind == "jsonb":
                # target is a hardcoded settings-key literal, never user input.
                jsonb_pairs.append(f"'{target}', ${i}::text")
            elif kind == "jsonb_obj":
                # Whole-object JSONB value (e.g. agent_config / persona_blocks).
                # target is a hardcoded key literal; value rides as a ::jsonb $N.
                jsonb_pairs.append(f"'{target}', ${i}::jsonb")
            else:
                cast_suffix = "::jsonb" if target == "business_hours" else ""
                set_parts.append(f"{target} = ${i}{cast_suffix}")
            args.append(val)
        if jsonb_pairs:
            set_parts.append(
                "settings = COALESCE(settings, '{}'::jsonb) "
                f"|| jsonb_build_object({', '.join(jsonb_pairs)})"
            )
        # Final positional arg = tenant_id for the WHERE.
        args.append(ctx.tenant_id)
        sql = (
            f"UPDATE tenants SET {', '.join(set_parts)} "  # noqa: S608 — literals only
            f"WHERE id = ${len(args)}"
        )
        result = await conn.execute(sql, *args)
        if result.endswith(" 0"):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "TENANT_NOT_FOUND", "message": "tenant row missing"},
            )

    # Return the fresh state so the client doesn't have to reconcile
    # against its own optimistic copy.
    return await read_settings(ctx=ctx, conn=conn)
