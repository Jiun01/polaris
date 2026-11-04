"""Tenant-broadcasts router module-level tests — Phase 6 PR-5.

Route registration + main-app mount. Full lifecycle against real DB
runs in CI.
"""

from __future__ import annotations

from polaris_api.routers import tenant_broadcasts as router_module


def test_router_exposes_eight_endpoints() -> None:
    """8 routes: POST /, schedule, pause, resume, cancel; GET /, /{id},
    /{id}/recipients."""
    paths = {r.path for r in router_module.router.routes}
    assert paths == {
        "/",
        "/{broadcast_id}",
        "/{broadcast_id}/schedule",
        "/{broadcast_id}/pause",
        "/{broadcast_id}/resume",
        "/{broadcast_id}/cancel",
        "/{broadcast_id}/recipients",
    }


def test_routes_mounted_under_broadcasts_prefix() -> None:
    """Pin the absolute paths so refactors don't relocate the Phase 8
    portal contract."""
    from polaris_api.main import app

    paths = {route.path for route in app.routes if hasattr(route, "path")}
    expected = {
        "/api/tenant/broadcasts/",
        "/api/tenant/broadcasts/{broadcast_id}",
        "/api/tenant/broadcasts/{broadcast_id}/schedule",
        "/api/tenant/broadcasts/{broadcast_id}/pause",
        "/api/tenant/broadcasts/{broadcast_id}/resume",
        "/api/tenant/broadcasts/{broadcast_id}/cancel",
        "/api/tenant/broadcasts/{broadcast_id}/recipients",
    }
    assert expected.issubset(paths)


def test_error_mapper_returns_distinct_status_per_subclass() -> None:
    """Each typed service error maps to a distinct HTTP envelope. Pin
    the mappings so 4xx behaviour is observably stable."""
    from polaris_api.services.broadcast import service

    cases = [
        (service.BroadcastTierBlockedError("x"), 403, "BROADCAST_TIER_BLOCKED"),
        (
            service.BroadcastTemplateNotApprovedError("x"),
            422,
            "BROADCAST_TEMPLATE_NOT_APPROVED",
        ),
        (
            service.BroadcastAudienceEmptyError("x"),
            422,
            "BROADCAST_AUDIENCE_EMPTY",
        ),
        (
            service.BroadcastQuotaExceededError("x"),
            429,
            "BROADCAST_QUOTA_EXCEEDED",
        ),
        (
            service.BroadcastStateInvalidError("x"),
            409,
            "BROADCAST_STATE_INVALID",
        ),
        (service.BroadcastError("x"), 404, "BROADCAST_NOT_FOUND"),
    ]
    for exc, expected_status, expected_code in cases:
        http_exc = router_module._map_service_error(exc)  # noqa: SLF001
        assert http_exc.status_code == expected_status, (
            f"{type(exc).__name__} should map to {expected_status} but got "
            f"{http_exc.status_code}"
        )
        assert http_exc.detail["code"] == expected_code  # type: ignore[index]
