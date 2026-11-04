"""Shared booking-notification send primitive.

One place that (a) formats an appointment datetime in the tenant's timezone and
(b) turns a resolved (recipient, token, template-or-message) into an actual
WhatsApp send. Shared by the confirmation send (``link_flow.confirm_booking``)
and the reminder worker (``booking_reminders``) so the template-vs-free-form
decision and the datetime formatting live once.

Template-vs-text semantics (mirrors the reminder worker's original block):
  * a configured **approved template** delivers both inside AND outside
    WhatsApp's 24h customer-initiated window (utility / non-marketing endpoint);
  * a **free-form text** fallback only delivers inside the 24h window — outside
    it Meta returns 131026, which the caller catches.

This module does no DB or secret work — callers resolve ``phone_number_id`` and
``access_token`` and pass them in (keeps it pure + trivially testable).
"""

from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from polaris_api.services.chatwoot.templates import substitute

logger = logging.getLogger(__name__)

_DEFAULT_TZ = "UTC"


def _resolve_tz(tenant_timezone: str | None) -> tuple[str, ZoneInfo]:
    """Return (zone_name, ZoneInfo), falling back to UTC on unknown zones."""
    tz_name = tenant_timezone or _DEFAULT_TZ
    try:
        return tz_name, ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning("booking notify: unknown timezone %r — using UTC", tz_name)
        return _DEFAULT_TZ, ZoneInfo(_DEFAULT_TZ)


def slot_date_time(
    start_at: datetime, tenant_timezone: str | None
) -> tuple[str, str]:
    """Return ``(date_str, time_str)`` in the tenant tz for free-form messages.

    e.g. ``("Wednesday, 4 Jun 2026", "09:00")``. Portable day formatting
    (``local.day``) — no glibc ``%-d``.
    """
    _, tz = _resolve_tz(tenant_timezone)
    local = start_at.astimezone(tz)
    return (f"{local:%A}, {local.day} {local:%b %Y}", f"{local:%H:%M}")


def render_named(text: str, variables: dict[str, str]) -> str:
    """Substitute ``{{name}}`` placeholders; unknown names render as empty.

    Delegates to ``chatwoot.templates.substitute`` (same ``{{name}}`` syntax
    and missing-name-to-empty semantics) so tenant-authored confirmation copy
    behaves consistently across the product.
    """
    return substitute(text, variables)


def format_slot(start_at: datetime, tenant_timezone: str | None) -> str:
    """Format ``start_at`` for a customer message in the tenant's timezone.

    Falls back to UTC when ``tenant_timezone`` is unset or not a known IANA
    zone. Uses ``local.day`` (not ``%-d``) so the format is portable across
    Linux/macOS/Windows — ``%-d`` is a glibc extension that raises on Windows.

    Example: ``"Wednesday, 4 Jun 2026 at 09:00 (Asia/Kuala_Lumpur)"``.
    """
    tz_name, tz = _resolve_tz(tenant_timezone)
    # start_at is TIMESTAMPTZ (tz-aware); astimezone re-anchors to the tenant tz.
    local = start_at.astimezone(tz)
    return (
        f"{local:%A}, {local.day} {local:%b %Y} at {local:%H:%M} ({tz_name})"
    )


async def send_appointment_message(
    meta_client: object,
    *,
    phone_number_id: str,
    to: str,
    access_token: str,
    template_name: str | None,
    template_language: str,
    template_vars: dict[str, str],
    free_form_text: str,
) -> str:
    """Send via approved template when ``template_name`` is set, else free-form.

    Returns the provider message id (wamid). Raises ``MetaWhatsAppError`` on a
    non-2xx Graph response — the caller decides whether that is terminal or
    best-effort. ``meta_client`` is typed ``object`` to avoid importing the
    concrete client at module load (circular-import hygiene); the two methods
    used are part of the ``MetaWhatsAppClient`` contract.
    """
    from polaris_api.services.meta_whatsapp import MetaWhatsAppClient

    assert isinstance(meta_client, MetaWhatsAppClient)  # noqa: S101

    if template_name:
        return await meta_client.send_template(
            phone_number_id=phone_number_id,
            to=to,
            template_name=template_name,
            language=template_language,
            variables=template_vars,
            access_token=access_token,
            use_marketing_endpoint=False,
        )
    return await meta_client.send_text(
        phone_number_id=phone_number_id,
        to=to,
        body=free_form_text,
        access_token=access_token,
    )


__all__ = [
    "format_slot",
    "render_named",
    "send_appointment_message",
    "slot_date_time",
]
