"""Unit tests for the admin-health `_run_probe` helper (no DB required).

`_run_probe` centralises the try/except/log + fallback boilerplate the
per-tile `_safe_*` wrappers used to repeat. These cover the two paths the
plan calls out: a probe that succeeds is returned verbatim; a probe that
raises is swallowed and `fallback(exc)` is returned.
"""

from __future__ import annotations

from polaris_api.routers.admin_health import _run_probe


async def test_run_probe_returns_value_on_success() -> None:
    async def ok() -> str:
        return "value"

    result = await _run_probe(ok(), lambda _exc: "fallback", "test")

    assert result == "value"


async def test_run_probe_returns_fallback_with_exception_on_error() -> None:
    async def boom() -> str:
        raise RuntimeError("kaboom")

    result = await _run_probe(boom(), lambda exc: f"fallback:{exc}", "test")

    assert result == "fallback:kaboom"
