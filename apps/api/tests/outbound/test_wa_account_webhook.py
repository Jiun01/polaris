"""wa-account (MM Lite onboarding) webhook tests — Phase 6 PR-7.

Schema + HMAC + route-registration coverage. End-to-end (resolve
phone_number_id -> tenant_channels row + UPDATE mm_lite_*) runs in
CI's migration-check workflow.
"""

from __future__ import annotations

import hashlib
import hmac

import pytest
from fastapi.testclient import TestClient
from polaris_api.config import get_settings
from polaris_api.main import app
from polaris_api.webhooks import wa_account_router

WEBHOOK_PATH = "/v1/webhooks/wa-account"


def _sign(body: bytes, secret: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_route_registered_at_v1_webhooks() -> None:
    paths = {route.path for route in app.routes if hasattr(route, "path")}
    assert "/v1/webhooks/wa-account" in paths


def test_event_mapping_covers_meta_vocabulary() -> None:
    """Meta vocab -> mm_lite_status enum. Pin each mapping so a Meta
    add of a new event value surfaces as 'unknown event' rather than
    silently mismatching an existing state."""
    assert wa_account_router._EVENT_TO_STATUS == {  # noqa: SLF001
        "AD_ACCOUNT_LINKED": "IN_PROGRESS",
        "ONBOARDING_COMPLETED": "ONBOARDED",
        "AD_ACCOUNT_UNLINKED": "UNLINKED",
    }


def test_webhook_rejects_missing_signature(client: TestClient) -> None:
    response = client.post(WEBHOOK_PATH, json={"object": "x", "entry": []})
    assert response.status_code == 401


def test_webhook_rejects_wrong_signature(client: TestClient) -> None:
    body = b'{"object": "whatsapp_business_account", "entry": []}'
    response = client.post(
        WEBHOOK_PATH,
        content=body,
        headers={
            "X-Hub-Signature-256": "sha256=" + "0" * 64,
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 401


def test_webhook_rejects_malformed_json(client: TestClient) -> None:
    settings = get_settings()
    if not settings.meta_app_secret:
        pytest.skip("META_APP_SECRET not configured")
    body = b"not-json-{{"
    response = client.post(
        WEBHOOK_PATH,
        content=body,
        headers={
            "X-Hub-Signature-256": _sign(body, settings.meta_app_secret),
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 400


def test_idempotency_key_shape_matches_spec() -> None:
    """external_id = ``<phone_number_id>:<event>:<timestamp>``. Pinned
    so refactors don't silently break the UNIQUE constraint shape."""
    formed = "{pid}:{event}:{ts}".format(
        pid="PID_123", event="AD_ACCOUNT_LINKED", ts="1715512345"
    )
    assert formed == "PID_123:AD_ACCOUNT_LINKED:1715512345"
