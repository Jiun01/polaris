"""Tests for link_flow._send_confirmation_for_confirmed (Phase B).

Fakes the asyncpg connection + patches the notify/secrets/meta seams. Covers
the template-primary / free-form-fallback decision and every no-op guard
(channel, nothing-configured, no WA channel, no token) plus the best-effort
swallow.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from polaris_api.services.booking.link_flow import (
    _send_confirmation_for_confirmed,
)

pytestmark = pytest.mark.asyncio(loop_scope="function")

_START = datetime(2026, 7, 1, 9, 0, tzinfo=UTC)


def _patches(
    *,
    booking_config: dict,
    send_mock: AsyncMock,
    token: bytes | None = b"waba-token",
):
    """Bundle the four import seams + meta client into one context list."""
    return [
        patch(
            "polaris_api.services.booking.config.read_booking_config",
            new=AsyncMock(return_value=booking_config),
        ),
        patch(
            "polaris_api.services.booking._notify.send_appointment_message",
            new=send_mock,
        ),
        patch(
            "polaris_api.services.secrets.get",
            new=AsyncMock(return_value=token),
        ),
        patch(
            "polaris_api.services.meta_whatsapp.get_meta_whatsapp_client",
            new=MagicMock(return_value=MagicMock()),
        ),
    ]


async def _run(conn, send_mock, **kwargs):
    defaults = dict(
        tenant_id=uuid4(),
        booking_id=uuid4(),
        customer_handle="60123456789",
        channel="whatsapp",
        service_name="Haircut",
        start_at=_START,
    )
    defaults.update(kwargs)
    await _send_confirmation_for_confirmed(conn, **defaults)


async def test_sends_template_when_configured() -> None:
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"phone_number_id": "PN1"})
    send_mock = AsyncMock()
    bc = {
        "confirmation_template_name": "appointment_confirmation",
        "confirmation_template_language": "ms_MY",
        "tenant_timezone": "Asia/Kuala_Lumpur",
    }
    cms = _patches(booking_config=bc, send_mock=send_mock)
    with cms[0], cms[1], cms[2], cms[3]:
        await _run(conn, send_mock)

    send_mock.assert_awaited_once()
    kw = send_mock.await_args.kwargs
    assert kw["template_name"] == "appointment_confirmation"
    assert kw["template_language"] == "ms_MY"
    assert kw["phone_number_id"] == "PN1"
    assert kw["to"] == "60123456789"
    # 09:00 UTC → 17:00 in KL; slot is template var "2".
    assert "17:00" in kw["template_vars"]["2"]


async def test_falls_back_to_free_form_message() -> None:
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"phone_number_id": "PN1"})
    send_mock = AsyncMock()
    bc = {
        "confirmation_message": "Booked: {{service}} on {{date}} at {{time}}.",
        "tenant_timezone": "Asia/Kuala_Lumpur",
    }
    cms = _patches(booking_config=bc, send_mock=send_mock)
    with cms[0], cms[1], cms[2], cms[3]:
        await _run(conn, send_mock)

    send_mock.assert_awaited_once()
    kw = send_mock.await_args.kwargs
    assert kw["template_name"] is None
    assert kw["free_form_text"] == "Booked: Haircut on Wednesday, 1 Jul 2026 at 17:00."


async def test_no_send_when_channel_not_whatsapp() -> None:
    conn = AsyncMock()
    send_mock = AsyncMock()
    cms = _patches(booking_config={"confirmation_template_name": "x"}, send_mock=send_mock)
    with cms[0], cms[1], cms[2], cms[3]:
        await _run(conn, send_mock, channel="messenger")
    send_mock.assert_not_awaited()


async def test_no_send_when_nothing_configured() -> None:
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"phone_number_id": "PN1"})
    send_mock = AsyncMock()
    cms = _patches(booking_config={}, send_mock=send_mock)
    with cms[0], cms[1], cms[2], cms[3]:
        await _run(conn, send_mock)
    send_mock.assert_not_awaited()


async def test_no_send_when_no_active_wa_channel() -> None:
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=None)  # no active WA channel
    send_mock = AsyncMock()
    cms = _patches(booking_config={"confirmation_template_name": "x"}, send_mock=send_mock)
    with cms[0], cms[1], cms[2], cms[3]:
        await _run(conn, send_mock)
    send_mock.assert_not_awaited()


async def test_no_send_when_no_token() -> None:
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"phone_number_id": "PN1"})
    send_mock = AsyncMock()
    cms = _patches(
        booking_config={"confirmation_template_name": "x"},
        send_mock=send_mock,
        token=None,
    )
    with cms[0], cms[1], cms[2], cms[3]:
        await _run(conn, send_mock)
    send_mock.assert_not_awaited()


async def test_send_error_is_swallowed() -> None:
    from polaris_api.services.meta_whatsapp import MetaWhatsAppError

    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"phone_number_id": "PN1"})
    send_mock = AsyncMock(side_effect=MetaWhatsAppError("boom", status_code=131026))
    cms = _patches(booking_config={"confirmation_template_name": "x"}, send_mock=send_mock)
    with cms[0], cms[1], cms[2], cms[3]:
        # Must not raise.
        await _run(conn, send_mock)
    send_mock.assert_awaited_once()
