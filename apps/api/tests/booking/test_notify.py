"""Tests for services/booking/_notify (Phase B).

Pure helpers — no DB, no network. Covers tz formatting (incl. the UTC
fallback and cross-platform day formatting), named substitution, and the
template-vs-text send decision.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from polaris_api.services.booking._notify import (
    format_slot,
    render_named,
    send_appointment_message,
    slot_date_time,
)

# 09:00 UTC on a Wednesday.
_UTC_DT = datetime(2026, 7, 1, 9, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# format_slot / slot_date_time — tz handling
# ---------------------------------------------------------------------------


def test_format_slot_utc_default() -> None:
    out = format_slot(_UTC_DT, None)
    assert out == "Wednesday, 1 Jul 2026 at 09:00 (UTC)"


def test_format_slot_converts_to_tenant_tz() -> None:
    """09:00 UTC is 17:00 in Asia/Kuala_Lumpur (UTC+8)."""
    out = format_slot(_UTC_DT, "Asia/Kuala_Lumpur")
    assert out == "Wednesday, 1 Jul 2026 at 17:00 (Asia/Kuala_Lumpur)"


def test_format_slot_unknown_tz_falls_back_to_utc() -> None:
    out = format_slot(_UTC_DT, "Mars/Olympus_Mons")
    assert out == "Wednesday, 1 Jul 2026 at 09:00 (UTC)"


def test_slot_date_time_splits_parts_in_tenant_tz() -> None:
    date_str, time_str = slot_date_time(_UTC_DT, "Asia/Kuala_Lumpur")
    assert date_str == "Wednesday, 1 Jul 2026"
    assert time_str == "17:00"


# ---------------------------------------------------------------------------
# render_named
# ---------------------------------------------------------------------------


def test_render_named_substitutes_known_vars() -> None:
    out = render_named(
        "Hi! {{service}} is booked for {{date}} at {{time}}.",
        {"service": "Haircut", "date": "Wed, 1 Jul", "time": "17:00"},
    )
    assert out == "Hi! Haircut is booked for Wed, 1 Jul at 17:00."


def test_render_named_unknown_var_becomes_empty() -> None:
    """Unsupported placeholders (e.g. {{provider}} which we can't fill at
    confirm time) render as empty rather than leaking raw braces."""
    out = render_named("See {{provider}} for {{service}}.", {"service": "X"})
    assert out == "See  for X."


# ---------------------------------------------------------------------------
# send_appointment_message — template vs text
# ---------------------------------------------------------------------------


def _fake_meta_client() -> AsyncMock:
    from polaris_api.services.meta_whatsapp import MetaWhatsAppClient

    client = AsyncMock(spec=MetaWhatsAppClient)
    client.send_template = AsyncMock(return_value="wamid.template")
    client.send_text = AsyncMock(return_value="wamid.text")
    return client


@pytest.mark.asyncio(loop_scope="function")
async def test_send_uses_template_when_name_present() -> None:
    client = _fake_meta_client()
    wamid = await send_appointment_message(
        client,
        phone_number_id="PN1",
        to="60123",
        access_token="tok",
        template_name="appointment_confirmation",
        template_language="ms_MY",
        template_vars={"1": "Haircut", "2": "Wed 17:00"},
        free_form_text="ignored",
    )
    assert wamid == "wamid.template"
    client.send_template.assert_awaited_once()
    client.send_text.assert_not_awaited()
    kwargs = client.send_template.await_args.kwargs
    assert kwargs["template_name"] == "appointment_confirmation"
    assert kwargs["language"] == "ms_MY"
    assert kwargs["use_marketing_endpoint"] is False


@pytest.mark.asyncio(loop_scope="function")
async def test_send_falls_back_to_text_when_no_template() -> None:
    client = _fake_meta_client()
    wamid = await send_appointment_message(
        client,
        phone_number_id="PN1",
        to="60123",
        access_token="tok",
        template_name=None,
        template_language="en",
        template_vars={},
        free_form_text="You're booked!",
    )
    assert wamid == "wamid.text"
    client.send_text.assert_awaited_once()
    client.send_template.assert_not_awaited()
    assert client.send_text.await_args.kwargs["body"] == "You're booked!"
