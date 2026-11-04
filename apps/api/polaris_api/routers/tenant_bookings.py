"""Tenant-facing booking-config API — Phase 8 PR-8.

Mounted at ``/api/tenant/bookings``. Surfaces the booking configuration
the portal Bookings screen (#9) reads + writes. Tenant_admin tunes
``cancellation_lead_hours``, ``confirmation_message``, and the
``reschedule_window_hours``; the EA fork (Phase 7 PR-6) reads the
same JSONB at runtime.

Storage decision: per ADR-049 + the GA trim, booking config rides in
``tenants.settings.booking_config`` JSONB rather than a dedicated
table. Three reasons:

  1. The config is a small, flat blob — a row would be overkill.
  2. RLS on ``tenants`` already gates reads/writes; no new policy
     surface.
  3. EA-side reads through Polaris (per the "portal never holds EA
     Bearer tokens" rule) so the EA fork only sees the settings via
     ``/api/tenant/bookings/config`` GET.

Two endpoints:

  * ``GET  /config``  — return the current config (or defaults when
                        the tenant has never saved one).
  * ``PUT  /config``  — replace the config blob. Validation matches
                        the portal form (cancellation_lead_hours in
                        [0, 168], confirmation_message ≤ 2000 chars).

Defaults mirror portal_design_prompt §9: cancellation lead-time 24h,
reschedule window 24h, AI confirmation message left empty so the
tenant has to supply one before the booking flow auto-confirms.
"""

from __future__ import annotations

import json
import logging
from typing import Annotated, Any

import asyncpg
from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field

from polaris_api.deps.tenant import (
    TenantContext,
    get_request_conn,
    get_tenant_context,
)
from polaris_api.services.booking.settings_sync import (
    DEFAULT_REFRESH_INTERVAL_S,
    EA_OWNED_BOOKING_CONFIG_KEYS,
    sync_ea_calendar,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["tenant-bookings"])

# Defaults match portal_design_prompt §9 + portal form fallbacks.
_DEFAULT_CANCELLATION_LEAD_HOURS = 24
_DEFAULT_RESCHEDULE_WINDOW_HOURS = 24


class BookingConfig(BaseModel):
    """Tenant-tunable booking knobs.

    ``cancellation_lead_hours`` — minimum hours before the appointment
    when the customer can still cancel without contacting support.
    EA enforces this server-side.

    ``reschedule_window_hours`` — same idea for reschedule requests.

    ``confirmation_message`` — the WhatsApp text the bot sends after
    EA confirms an appointment. Left empty means "EA's default
    template" (configured per service in EA admin).

    ``allow_self_serve_cancellation`` — toggles the inline "Cancel my
    appointment" CTA on the booking confirmation. When False, the
    customer is routed to handoff instead.

    ``reminder_offset_hours`` — how many hours before start_at the
    reminder is sent. Default 24. Range 1–168 (1h to 7d).

    ``quiet_hours_start`` / ``quiet_hours_end`` — 24h clock strings
    (e.g. "22:00", "08:00"). When set, the worker defers reminders
    that fall within the quiet window to quiet_hours_end the same or
    next day. Both must be set to enable quiet-hours enforcement;
    setting only one has no effect. Stored as strings; format HH:MM.

    ``tenant_timezone`` — IANA zone (e.g. "Asia/Kuala_Lumpur") used to
    format appointment date/times in customer messages. Empty/None →
    times render in UTC. Invalid zones soft-fall-back to UTC at send.

    Template names (each an approved WhatsApp template; empty = "use the
    free-form text path / no send"):
    ``confirmation_template_name`` (+ ``_language``) — sent immediately
    when EA confirms a booking; delivers outside the 24h window. Falls
    back to free-form ``confirmation_message`` when unset.
    ``reminder_template_name`` (+ ``_language``) — the pre-appointment
    reminder; falls back to free-form text when unset.
    ``cancellation_template_name`` / ``reschedule_template_name``
    (+ ``_language``) — optional notices on cancel / reschedule.
    """

    cancellation_lead_hours: int = Field(
        default=_DEFAULT_CANCELLATION_LEAD_HOURS, ge=0, le=168
    )
    reschedule_window_hours: int = Field(
        default=_DEFAULT_RESCHEDULE_WINDOW_HOURS, ge=0, le=168
    )
    confirmation_message: str = Field(default="", max_length=2000)
    allow_self_serve_cancellation: bool = Field(default=True)
    reminder_offset_hours: int = Field(default=24, ge=1, le=168)
    quiet_hours_start: str | None = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    quiet_hours_end: str | None = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    tenant_timezone: str | None = Field(default=None, max_length=64)
    confirmation_template_name: str | None = Field(default=None, max_length=512)
    confirmation_template_language: str | None = Field(default=None, max_length=10)
    reminder_template_name: str | None = Field(default=None, max_length=512)
    reminder_template_language: str | None = Field(default=None, max_length=10)
    cancellation_template_name: str | None = Field(default=None, max_length=512)
    cancellation_template_language: str | None = Field(default=None, max_length=10)
    reschedule_template_name: str | None = Field(default=None, max_length=512)
    reschedule_template_language: str | None = Field(default=None, max_length=10)
    # Phase 2 in-chat slot picker. When true, the conversational booking handler
    # offers available slots in the chat instead of sending a booking link.
    # Defaults to false (link mode) so Phase 2 ships dark until the operator
    # opts in from the Bookings page.
    inchat_picker: bool = Field(default=False)


class EaCalendar(BaseModel):
    """Read-only mirror of the tenant's EA (Calendar admin) booking settings.

    Populated by ``settings_sync.sync_ea_calendar`` from EA — EA is the source
    of truth. The portal renders this read-only and drives a "next refresh"
    countdown off ``next_refresh_at``. All fields are optional so a never-synced
    tenant (or a malformed EA value) renders "not configured" instead of 500ing.
    """

    lead_time_hours: int | None = None
    timezone: str | None = None
    working_plan: dict[str, Any] | None = None
    blocked_periods: list[dict[str, Any]] = Field(default_factory=list)
    future_booking_limit_days: int | None = None
    synced_at: str | None = None
    next_refresh_at: str | None = None
    refresh_interval_s: int | None = None


class BookingConfigView(BookingConfig):
    """GET/sync response: the editable bot config plus the read-only EA mirror.

    Subclasses ``BookingConfig`` (so all flat fields stay in the response for
    back-compat) and adds ``ea_calendar``. ``None`` when the tenant has no EA
    mirror yet.
    """

    ea_calendar: EaCalendar | None = None


def _config_from_settings(settings_jsonb: Any) -> BookingConfig:
    """Lift ``tenants.settings.booking_config`` into the typed shape.

    Missing keys (no config saved yet, partial save) fall back to the
    BookingConfig field defaults; unknown keys are dropped at parse
    time via Pydantic. Returns a fully-populated config either way so
    the portal never sees a half-shaped response.
    """
    if isinstance(settings_jsonb, str):
        try:
            settings_jsonb = json.loads(settings_jsonb)
        except json.JSONDecodeError:
            settings_jsonb = {}
    if not isinstance(settings_jsonb, dict):
        settings_jsonb = {}
    raw = settings_jsonb.get("booking_config") or {}
    if not isinstance(raw, dict):
        raw = {}
    # Strip unknown keys so a stale field saved by a previous deploy
    # doesn't 422 the read path. Pydantic's default mode silently
    # drops keys outside the schema.
    return BookingConfig.model_validate(raw)


def _ea_calendar_from_settings(settings_jsonb: Any) -> EaCalendar | None:
    """Lift ``tenants.settings.ea_calendar`` (the EA mirror) into the typed shape.

    Returns None when the tenant has never synced (no ``ea_calendar`` block) so
    the portal shows "not synced yet" rather than an empty panel.
    """
    if isinstance(settings_jsonb, str):
        try:
            settings_jsonb = json.loads(settings_jsonb)
        except json.JSONDecodeError:
            return None
    if not isinstance(settings_jsonb, dict):
        return None
    raw = settings_jsonb.get("ea_calendar")
    if not isinstance(raw, dict):
        return None
    return EaCalendar.model_validate(raw)


def _view_from_settings(settings_jsonb: Any) -> BookingConfigView:
    """Build the combined editable-config + read-only EA-mirror view."""
    config = _config_from_settings(settings_jsonb)
    return BookingConfigView(
        **config.model_dump(),
        ea_calendar=_ea_calendar_from_settings(settings_jsonb),
    )


@router.get(
    "/config",
    response_model=BookingConfigView,
    summary="Read the calling tenant's booking configuration + EA mirror",
)
async def get_config(
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    conn: Annotated[asyncpg.Connection, Depends(get_request_conn)],
) -> BookingConfigView:
    """Returns the saved config + EA mirror, or full defaults when nothing is set."""
    row = await conn.fetchrow(
        "SELECT settings FROM tenants WHERE id = $1", ctx.tenant_id,
    )
    if row is None:
        # Shouldn't happen — JWT was issued against this tenant — but
        # return defaults rather than raising so the portal renders
        # the empty form predictably.
        return BookingConfigView()
    return _view_from_settings(row["settings"])


@router.put(
    "/config",
    response_model=BookingConfigView,
    status_code=status.HTTP_200_OK,
    summary="Update the editable (bot-messaging) booking configuration",
)
async def put_config(
    body: BookingConfig,
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    conn: Annotated[asyncpg.Connection, Depends(get_request_conn)],
) -> BookingConfigView:
    """Replace the booking_config blob inside tenants.settings.

    Uses ``jsonb_set`` so we touch only the booking_config key — other settings
    (bot persona, business hours, EA mirror) stay intact.

    EA is the source of truth for the lead-time + timezone keys
    (``EA_OWNED_BOOKING_CONFIG_KEYS``): the portal renders them read-only, and
    we re-overlay the stored values here so a stale client can't overwrite the
    synced policy. Only the bot-only messaging fields are actually mutable.
    """
    # Re-read the stored config so we can preserve EA-owned policy keys
    # server-side (defence against a stale/forged client PUT).
    row = await conn.fetchrow(
        "SELECT settings FROM tenants WHERE id = $1", ctx.tenant_id,
    )
    stored = _config_from_settings(row["settings"]) if row is not None else BookingConfig()

    # Pass a dict so asyncpg serialises it as a JSONB object.
    # model_dump_json() returns a Python str which asyncpg encodes as
    # a JSONB string literal ("\"...\"") instead of a JSONB object,
    # causing silent data loss on every read.
    payload = body.model_dump(mode="json")
    for key in EA_OWNED_BOOKING_CONFIG_KEYS:
        # EA-sourced — keep what the last sync wrote, ignore the client value.
        payload[key] = getattr(stored, key)
    await conn.execute(
        """
        UPDATE tenants
        SET settings = jsonb_set(
            -- COALESCE only catches SQL NULL; a JSON scalar/`null` settings value
            -- makes jsonb_set raise "cannot set path in scalar". Coerce anything
            -- that isn't a JSON object to {} first.
            CASE WHEN jsonb_typeof(settings) = 'object' THEN settings ELSE '{}'::jsonb END,
            '{booking_config}',
            $1::jsonb,
            true
        ),
        updated_at = now()
        WHERE id = $2
        """,
        payload,
        ctx.tenant_id,
    )
    logger.info(
        "tenant_bookings.config_saved tenant=%s lead=%s window=%s",
        ctx.tenant_id,
        payload.get("cancellation_lead_hours"),
        payload.get("reschedule_window_hours"),
    )
    # Return the freshly-saved config plus the (unchanged) EA mirror.
    return BookingConfigView(
        **BookingConfig.model_validate(payload).model_dump(),
        ea_calendar=_ea_calendar_from_settings(row["settings"]) if row is not None else None,
    )


@router.post(
    "/config/sync",
    response_model=BookingConfigView,
    status_code=status.HTTP_200_OK,
    summary="Pull the latest EA settings into the bot mirror (Update bot knowledge)",
)
async def sync_config(
    ctx: Annotated[TenantContext, Depends(get_tenant_context)],
    conn: Annotated[asyncpg.Connection, Depends(get_request_conn)],
) -> BookingConfigView:
    """Manual "Update bot knowledge" — re-read EA and refresh the mirror now.

    Never raises on EA failure (``sync_ea_calendar`` is fail-soft); the response
    simply carries the prior mirror so the portal can show "couldn't refresh".
    The countdown interval matches the scheduled tick so the displayed
    ``next_refresh_at`` lines up either way.
    """
    result = await sync_ea_calendar(
        conn, tenant_id=ctx.tenant_id, interval_s=DEFAULT_REFRESH_INTERVAL_S
    )
    logger.info(
        "tenant_bookings.config_sync tenant=%s ok=%s reason=%s",
        ctx.tenant_id,
        result.ok,
        result.reason,
    )
    row = await conn.fetchrow(
        "SELECT settings FROM tenants WHERE id = $1", ctx.tenant_id,
    )
    if row is None:
        return BookingConfigView()
    return _view_from_settings(row["settings"])
