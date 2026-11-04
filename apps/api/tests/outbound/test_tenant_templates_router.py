"""Tenant-templates router module-level tests — Phase 6 PR-3.

Schema + envelope + import-graph tests that don't need a live DB.
The full submit → Meta-webhook → status-flip end-to-end is exercised
in PR-9 operator smoke against the sandbox WABA + by the migration-
check CI workflow once docker-compose Postgres is wired.

Coverage at PR-3:
  * Router module imports cleanly + exposes the 3 expected routes.
  * Routes are mounted under /api/tenant/templates on the main app.
  * Request body validation rejects malformed payloads at the
    Pydantic boundary.
"""

from __future__ import annotations

import pytest
from polaris_api.routers import tenant_templates as router_module
from pydantic import ValidationError


def test_router_exposes_three_endpoints() -> None:
    paths = {r.path for r in router_module.router.routes}
    # GET / and POST / share the same path; DELETE has the {id} segment.
    assert paths == {"/", "/{template_id}"}


def test_router_mounted_under_tenant_templates_prefix() -> None:
    """The main app mounts the router at /api/tenant/templates — pin
    expected absolute paths so a future refactor doesn't silently
    move the API surface (would break the Phase 8 portal)."""
    from polaris_api.main import app

    paths = {route.path for route in app.routes if hasattr(route, "path")}
    assert "/api/tenant/templates/" in paths
    assert "/api/tenant/templates/{template_id}" in paths


def test_meta_template_status_webhook_route_registered() -> None:
    from polaris_api.main import app

    paths = {route.path for route in app.routes if hasattr(route, "path")}
    assert "/v1/webhooks/meta-template-status" in paths


def test_submit_body_rejects_invalid_template_name() -> None:
    """Pydantic CHECK matches the migration 041 name pattern:
    lowercase + digits + underscore. Uppercase / hyphens / spaces
    all fail at the API boundary instead of round-tripping to Meta
    which would 400."""
    from uuid import uuid4

    Body = router_module._SubmitBody  # noqa: SLF001 — test-only access

    with pytest.raises(ValidationError):
        Body(
            tenant_channel_id=uuid4(),
            name="BadName",  # uppercase rejected
            language="en_US",
            category="UTILITY",
            components=[{"type": "BODY", "text": "Hi {{1}}"}],
        )

    with pytest.raises(ValidationError):
        Body(
            tenant_channel_id=uuid4(),
            name="bad-name",  # hyphen rejected
            language="en_US",
            category="UTILITY",
            components=[{"type": "BODY", "text": "Hi {{1}}"}],
        )


def test_submit_body_rejects_invalid_category() -> None:
    from uuid import uuid4

    Body = router_module._SubmitBody  # noqa: SLF001

    with pytest.raises(ValidationError):
        Body(
            tenant_channel_id=uuid4(),
            name="ok_name",
            language="en_US",
            category="PROMOTIONAL",  # not in Meta's enum
            components=[{"type": "BODY", "text": "Hi"}],
        )


def test_submit_body_requires_at_least_one_component() -> None:
    """min_length=1 on components prevents empty templates that Meta
    would 400 — fail-fast at our boundary."""
    from uuid import uuid4

    Body = router_module._SubmitBody  # noqa: SLF001

    with pytest.raises(ValidationError):
        Body(
            tenant_channel_id=uuid4(),
            name="ok_name",
            language="en_US",
            category="UTILITY",
            components=[],
        )
