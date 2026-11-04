"""Tests for booking_reminders reminder-text formatters (Phase B).

These were untested before the tenant-timezone change. They now route through
``_notify.format_slot`` — verify the tz conversion + UTC fallback + the var
shape the approved reminder template expects.
"""

from __future__ import annotations

from datetime import UTC, datetime

from polaris_api.services.booking_reminders import (
    _build_reminder_text,
    _reminder_template_vars,
)

_ROW = {
    "service_name": "Consultation",
    "start_at": datetime(2026, 7, 1, 9, 0, tzinfo=UTC),  # 09:00 UTC, Wed
}


def test_template_vars_positional_shape_and_utc_default() -> None:
    out = _reminder_template_vars(_ROW, None)
    assert out["1"] == "Consultation"
    assert out["2"] == "Wednesday, 1 Jul 2026 at 09:00 (UTC)"


def test_template_vars_uses_tenant_timezone() -> None:
    out = _reminder_template_vars(_ROW, "Asia/Kuala_Lumpur")
    assert out["2"] == "Wednesday, 1 Jul 2026 at 17:00 (Asia/Kuala_Lumpur)"


def test_template_vars_defaults_service_name() -> None:
    row = {"service_name": None, "start_at": _ROW["start_at"]}
    assert _reminder_template_vars(row, None)["1"] == "your appointment"


def test_build_reminder_text_includes_tenant_tz_slot() -> None:
    text = _build_reminder_text(_ROW, "Asia/Kuala_Lumpur")
    assert "Consultation" in text
    assert "17:00 (Asia/Kuala_Lumpur)" in text
    assert "*CONFIRM*" in text
