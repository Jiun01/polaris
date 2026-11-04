"""Tests for POST /v1/webhooks/ea — EA inbound webhook (v2.1 PR-1).

Uses FastAPI AsyncClient bound to the main ``app`` (mirrors meta_router
HMAC tests). ``confirm_booking`` is monkeypatched so no DB is needed.

Coverage:
  * Missing signature header → 401
  * Malformed signature (no sha256= prefix) → 401
  * Wrong HMAC signature → 401
  * Unconfigured / empty ea_webhook_hmac_secret → 401
  * Placeholder ea_webhook_hmac_secret → 401
  * Valid signature + valid binding token → 200 {outcome: "confirmed"}
  * Valid signature + already-confirmed redelivery → 200 {outcome: "already_confirmed"}
  * Valid signature + invalid/expired binding token → 200 {outcome: "rejected_token"}
  * Valid signature + missing start_datetime → 422
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from polaris_api.config import get_settings
from polaris_api.main import app
from polaris_api.services.booking import link_flow as link_flow_module
from polaris_api.services.booking.binding_token import (
    mint as mint_binding_token,
)

pytestmark = pytest.mark.asyncio(loop_scope="function")

_HMAC_SECRET = "test-ea-webhook-hmac-secret-not-placeholder"
_BINDING_SECRET = "test-binding-secret-for-ea-webhook-tests"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sign(raw: bytes, secret: str = _HMAC_SECRET) -> str:
    """Produce the X-Polaris-Signature header value."""
    digest = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _make_payload(
    *,
    polaris_ref: str = "dummy-ref",
    appointment_id: str = "42",
    start_datetime: str | None = "2026-07-01T09:00:00",
    end_datetime: str | None = "2026-07-01T09:30:00",
    service_name: str | None = "Consultation",
) -> bytes:
    body: dict[str, Any] = {
        "polaris_ref": polaris_ref,
        "appointment_id": appointment_id,
    }
    if start_datetime is not None:
        body["start_datetime"] = start_datetime
    if end_datetime is not None:
        body["end_datetime"] = end_datetime
    if service_name is not None:
        body["service_name"] = service_name
    return json.dumps(body).encode("utf-8")


@dataclass(frozen=True)
class FakeConfirmResult:
    outcome: str
    booking_id: UUID | None


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def configured_ea_settings() -> Iterator[None]:
    """Install known ea_webhook_hmac_secret + binding secret for the test."""
    get_settings.cache_clear()
    settings = get_settings()
    orig_hmac = settings.ea_webhook_hmac_secret
    orig_binding = settings.booking_binding_secret
    settings.ea_webhook_hmac_secret = _HMAC_SECRET
    settings.booking_binding_secret = _BINDING_SECRET
    try:
        yield
    finally:
        settings.ea_webhook_hmac_secret = orig_hmac
        settings.booking_binding_secret = orig_binding
        get_settings.cache_clear()


@pytest.fixture
def mocked_confirm(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Monkeypatch link_flow.confirm_booking to avoid any DB calls."""
    mock = AsyncMock(
        return_value=FakeConfirmResult(outcome="confirmed", booking_id=uuid4())
    )
    monkeypatch.setattr(link_flow_module, "confirm_booking", mock)
    return mock


@pytest_asyncio.fixture(loop_scope="function")
async def ea_client(
    configured_ea_settings: None,
    mocked_confirm: AsyncMock,
) -> AsyncIterator[AsyncClient]:
    """ASGI-bound AsyncClient with EA secrets installed + confirm mocked."""
    _ = configured_ea_settings
    _ = mocked_confirm
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


def _mint_valid_token() -> str:
    """Mint a real binding token using the test binding secret."""
    return mint_binding_token(
        jti=uuid4().hex,
        tenant_id=uuid4(),
        booking_id=uuid4(),
        ttl_seconds=3600,
        secret=_BINDING_SECRET,
    )


# ---------------------------------------------------------------------------
# HMAC verification failures → 401
# ---------------------------------------------------------------------------


async def test_missing_signature_header_returns_401(
    ea_client: AsyncClient,
) -> None:
    body = _make_payload()
    resp = await ea_client.post(
        "/v1/webhooks/ea",
        content=body,
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 401


async def test_missing_sha256_prefix_returns_401(
    ea_client: AsyncClient,
) -> None:
    """A signature header without 'sha256=' prefix is malformed → 401."""
    body = _make_payload()
    raw_digest = hmac.new(
        _HMAC_SECRET.encode(), body, hashlib.sha256
    ).hexdigest()
    resp = await ea_client.post(
        "/v1/webhooks/ea",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Polaris-Signature": raw_digest,  # no sha256= prefix
        },
    )
    assert resp.status_code == 401


async def test_wrong_signature_returns_401(
    ea_client: AsyncClient,
) -> None:
    body = _make_payload()
    wrong_sig = _sign(body, secret="wrong-secret-entirely")
    resp = await ea_client.post(
        "/v1/webhooks/ea",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Polaris-Signature": wrong_sig,
        },
    )
    assert resp.status_code == 401


async def test_tampered_body_returns_401(
    ea_client: AsyncClient,
) -> None:
    """Sign original body but send a different body → 401."""
    original = _make_payload(appointment_id="10")
    sig = _sign(original)
    tampered = _make_payload(appointment_id="99")  # different body
    resp = await ea_client.post(
        "/v1/webhooks/ea",
        content=tampered,
        headers={
            "Content-Type": "application/json",
            "X-Polaris-Signature": sig,
        },
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Unconfigured secret → 401
# ---------------------------------------------------------------------------


async def test_empty_ea_webhook_secret_fails_closed(
    mocked_confirm: AsyncMock,
) -> None:
    """An empty ea_webhook_hmac_secret must reject every request."""
    get_settings.cache_clear()
    settings = get_settings()
    orig = settings.ea_webhook_hmac_secret
    settings.ea_webhook_hmac_secret = ""
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            body = _make_payload()
            # Compute a valid-looking sig for the empty secret.
            sig = _sign(body, secret="")
            resp = await ac.post(
                "/v1/webhooks/ea",
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Polaris-Signature": sig,
                },
            )
        assert resp.status_code == 401
    finally:
        settings.ea_webhook_hmac_secret = orig
        get_settings.cache_clear()


async def test_placeholder_ea_webhook_secret_fails_closed(
    mocked_confirm: AsyncMock,
) -> None:
    """A placeholder ea_webhook_hmac_secret must also reject every request."""
    get_settings.cache_clear()
    settings = get_settings()
    orig = settings.ea_webhook_hmac_secret
    settings.ea_webhook_hmac_secret = "placeholder-ea-webhook-secret"
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            body = _make_payload()
            sig = _sign(body, secret="placeholder-ea-webhook-secret")
            resp = await ac.post(
                "/v1/webhooks/ea",
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Polaris-Signature": sig,
                },
            )
        assert resp.status_code == 401
    finally:
        settings.ea_webhook_hmac_secret = orig
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Valid signature + valid binding token → 200 confirmed
# ---------------------------------------------------------------------------


async def test_valid_signature_valid_token_returns_confirmed(
    ea_client: AsyncClient,
    mocked_confirm: AsyncMock,
) -> None:
    token = _mint_valid_token()
    body = _make_payload(polaris_ref=token)
    sig = _sign(body)

    resp = await ea_client.post(
        "/v1/webhooks/ea",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Polaris-Signature": sig,
        },
    )

    assert resp.status_code == 200
    assert resp.json() == {"outcome": "confirmed"}
    mocked_confirm.assert_awaited_once()


async def test_redelivery_already_confirmed_returns_200(
    ea_client: AsyncClient,
    mocked_confirm: AsyncMock,
) -> None:
    """On redelivery, confirm_booking returns already_confirmed — router must
    still 200 so the EA fork stops retrying."""
    mocked_confirm.return_value = FakeConfirmResult(
        outcome="already_confirmed", booking_id=uuid4()
    )

    token = _mint_valid_token()
    body = _make_payload(polaris_ref=token)
    sig = _sign(body)

    resp = await ea_client.post(
        "/v1/webhooks/ea",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Polaris-Signature": sig,
        },
    )

    assert resp.status_code == 200
    assert resp.json() == {"outcome": "already_confirmed"}


# ---------------------------------------------------------------------------
# Invalid / expired binding token → 200 rejected_token
# ---------------------------------------------------------------------------


async def test_expired_binding_token_returns_rejected_token_200(
    ea_client: AsyncClient,
    mocked_confirm: AsyncMock,
) -> None:
    """An expired binding token must be acked with 200 {rejected_token}
    so the EA fork doesn't retry infinitely (retrying won't fix expiry)."""
    # Mint a token with now far in the past so it is expired.
    expired_token = mint_binding_token(
        jti=uuid4().hex,
        tenant_id=uuid4(),
        booking_id=uuid4(),
        ttl_seconds=1,
        secret=_BINDING_SECRET,
        now=1_000_000,  # epoch far in the past → already expired
    )
    body = _make_payload(polaris_ref=expired_token)
    sig = _sign(body)

    resp = await ea_client.post(
        "/v1/webhooks/ea",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Polaris-Signature": sig,
        },
    )

    assert resp.status_code == 200
    assert resp.json() == {"outcome": "rejected_token"}
    # confirm_booking must NOT be called for a rejected token.
    mocked_confirm.assert_not_awaited()


async def test_malformed_binding_token_returns_rejected_token_200(
    ea_client: AsyncClient,
    mocked_confirm: AsyncMock,
) -> None:
    """A binding token that is completely garbage must be acked 200 rejected."""
    body = _make_payload(polaris_ref="not-a-valid-token-at-all")
    sig = _sign(body)

    resp = await ea_client.post(
        "/v1/webhooks/ea",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Polaris-Signature": sig,
        },
    )

    assert resp.status_code == 200
    assert resp.json() == {"outcome": "rejected_token"}
    mocked_confirm.assert_not_awaited()


# ---------------------------------------------------------------------------
# Missing start_datetime → 422
# ---------------------------------------------------------------------------


async def test_missing_start_datetime_returns_422(
    ea_client: AsyncClient,
    mocked_confirm: AsyncMock,
) -> None:
    """A booking webhook without start_datetime cannot be confirmed → 422."""
    token = _mint_valid_token()
    body = _make_payload(polaris_ref=token, start_datetime=None)
    sig = _sign(body)

    resp = await ea_client.post(
        "/v1/webhooks/ea",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Polaris-Signature": sig,
        },
    )

    assert resp.status_code == 422
    mocked_confirm.assert_not_awaited()


# ---------------------------------------------------------------------------
# confirm_booking wiring: correct params extracted from binding token + body
# ---------------------------------------------------------------------------


async def test_valid_request_passes_correct_params_to_confirm_booking(
    ea_client: AsyncClient,
    mocked_confirm: AsyncMock,
) -> None:
    """Router must pass the tenant_id/jti from the verified binding token and
    the appointment_id/start_at from the body."""
    tenant_id = uuid4()
    jti = uuid4().hex
    token = mint_binding_token(
        jti=jti,
        tenant_id=tenant_id,
        booking_id=uuid4(),
        ttl_seconds=3600,
        secret=_BINDING_SECRET,
    )
    appointment_id = "77"
    start_str = "2026-08-01T10:00:00"
    body = _make_payload(
        polaris_ref=token,
        appointment_id=appointment_id,
        start_datetime=start_str,
        end_datetime="2026-08-01T10:30:00",
        service_name="Haircut",
    )
    sig = _sign(body)

    resp = await ea_client.post(
        "/v1/webhooks/ea",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Polaris-Signature": sig,
        },
    )

    assert resp.status_code == 200
    kwargs = mocked_confirm.await_args.kwargs
    assert kwargs["tenant_id"] == tenant_id
    assert kwargs["jti"] == jti
    assert kwargs["appointment_id"] == appointment_id
    # The router tags a naive EA datetime as UTC (bookings.start_at is TIMESTAMPTZ).
    assert kwargs["start_at"] == datetime.fromisoformat(start_str).replace(tzinfo=UTC)
    assert kwargs["service_name"] == "Haircut"
