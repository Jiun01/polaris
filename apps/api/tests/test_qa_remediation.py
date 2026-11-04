"""Regression tests for the 2026-06-14 browser-QA defect remediation.

Covers the new server-side behaviours added for:
  * M6 — business-hours ``close`` must be strictly after ``open``.
  * H1 — the EA health probe must report degraded on a 5xx from the
          public booking host (httpx does NOT raise on 5xx).
"""

from __future__ import annotations

from typing import Any

import pytest
from polaris_api.routers.admin_health import _probe_ea
from polaris_api.routers.tenant_settings import BusinessHoursDay
from pydantic import ValidationError

# --------------------------------------------------------------------------- #
# M6 — business-hours cross-field validation
# --------------------------------------------------------------------------- #


def test_business_hours_rejects_close_before_open() -> None:
    with pytest.raises(ValidationError):
        BusinessHoursDay(open="18:00", close="09:00", closed=False)


def test_business_hours_rejects_close_equal_open() -> None:
    with pytest.raises(ValidationError):
        BusinessHoursDay(open="09:00", close="09:00", closed=False)


def test_business_hours_accepts_valid_window() -> None:
    day = BusinessHoursDay(open="09:00", close="18:00", closed=False)
    assert day.open == "09:00"
    assert day.close == "18:00"


def test_business_hours_skips_check_when_closed() -> None:
    # Closed days keep placeholder times; the close>open rule must not apply.
    day = BusinessHoursDay(open="18:00", close="09:00", closed=True)
    assert day.closed is True


# --------------------------------------------------------------------------- #
# H1 — EA health probe must branch on HTTP status
# --------------------------------------------------------------------------- #


class _FakeSettings:
    ea_health_url = "https://calendar.example.test/"


class _FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class _FakeClient:
    """Minimal async-context-manager stand-in for httpx.AsyncClient."""

    def __init__(self, status_code: int, **_: Any) -> None:
        self._status_code = status_code

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None

    async def get(self, _url: str) -> _FakeResponse:
        return _FakeResponse(self._status_code)


def _patch_httpx(monkeypatch: pytest.MonkeyPatch, status_code: int) -> None:
    import polaris_api.routers.admin_health as mod

    def _factory(**kwargs: Any) -> _FakeClient:
        return _FakeClient(status_code, **kwargs)

    monkeypatch.setattr(mod.httpx, "AsyncClient", _factory)


@pytest.mark.asyncio
async def test_ea_probe_degraded_on_502(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_httpx(monkeypatch, 502)
    result = await _probe_ea(_FakeSettings())  # type: ignore[arg-type]
    assert result.healthy is False
    assert result.error is not None and "502" in result.error


@pytest.mark.asyncio
async def test_ea_probe_healthy_on_200(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_httpx(monkeypatch, 200)
    result = await _probe_ea(_FakeSettings())  # type: ignore[arg-type]
    assert result.healthy is True
    assert result.error is None


# --------------------------------------------------------------------------- #
# L8 — promotion partial PATCH must reject explicit null on NOT-NULL fields
# --------------------------------------------------------------------------- #


def test_promotion_patch_rejects_explicit_null_on_required() -> None:
    from polaris_api.routers.tenant_promotions import TenantPromotionPatch

    for field in ("title", "description", "discount_text"):
        with pytest.raises(ValidationError):
            TenantPromotionPatch.model_validate({field: None})


def test_promotion_patch_allows_partial_and_nullable_clears() -> None:
    from polaris_api.routers.tenant_promotions import TenantPromotionPatch

    # Partial: a single field present, the rest omitted.
    p = TenantPromotionPatch.model_validate({"title": "New title"})
    assert p.title == "New title"
    assert "description" not in p.model_fields_set  # omitted, not null

    # Empty body is valid (no-op merge).
    assert TenantPromotionPatch.model_validate({}).title is None

    # Nullable columns CAN be explicitly cleared.
    cleared = TenantPromotionPatch.model_validate({"terms": None})
    assert cleared.terms is None
    assert "terms" in cleared.model_fields_set
