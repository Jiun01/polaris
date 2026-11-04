"""EA fork (Easy!Appointments) Bearer-JWT client tests (Phase 7 PR-6).

TDD-RED stage: written BEFORE services/booking/client.py exists.

Targets EA fork v1.5.2-r1 contract (ghcr.io/jiun01/polaris-ea:v1.5.2-r1):
  - URL: ``{base_url}/index.php/api/v1/{resource}`` (CodeIgniter convention)
  - Auth: Bearer JWT (HS256), per-call signed by Polaris
  - JWT claims: ``{tenant_id, aud='ea-fork', iss='polaris', iat, exp}``
  - EA server-side enforces tenant scope via the JWT's tenant_id claim
    (verified in EA fork's §6 cross-tenant isolation sweep against the
    booted image)

Coverage:
  - JWT is HS256-signed with claims matching the contract
  - per-tenant scoping: same client, different tenant_id → different JWT
  - list_services GET /api/v1/services
  - list_availabilities GET /api/v1/availabilities with service_id + range
  - create_appointment POST /api/v1/appointments
  - cancel_appointment DELETE /api/v1/appointments/{id}
  - reschedule_appointment PUT /api/v1/appointments/{id}
  - 5xx → EABookingTransientError, 4xx → EABookingStructuralError
  - timeout → transient
  - public_booking_url composes calendar.getpolaris.my/book/{slug}
  - missing JWT signing secret → EABookingConfigError (fail-closed)
"""

from __future__ import annotations

from datetime import date
from typing import Any
from uuid import uuid4

import httpx
import jwt as pyjwt
import pytest
from polaris_api.services.booking.client import (
    EABookingClient,
    EABookingConfigError,
    EABookingStructuralError,
    EABookingTransientError,
    public_booking_url,
)

_BASE = "https://calendar.getpolaris.my"
_SECRET = "test-ea-jwt-signing-secret-not-placeholder"


@pytest.fixture
def fake_http(monkeypatch: pytest.MonkeyPatch) -> Any:
    captured: dict[str, Any] = {"calls": [], "responses": []}

    class _StubClient:
        async def __aenter__(self) -> _StubClient:
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def request(
            self,
            method: str,
            url: str,
            *,
            json: dict[str, Any] | None = None,
            headers: dict[str, str] | None = None,
            params: dict[str, Any] | None = None,
            timeout: float | None = None,  # noqa: ARG002, ASYNC109 — B6 shared-client kwarg
        ) -> httpx.Response:
            captured["calls"].append(
                {
                    "method": method,
                    "url": url,
                    "json": json,
                    "headers": headers or {},
                    "params": params or {},
                }
            )
            if not captured["responses"]:
                raise AssertionError(
                    f"stub: no response programmed for {method} {url}"
                )
            programmed = captured["responses"].pop(0)
            if isinstance(programmed, BaseException):
                raise programmed
            return programmed

    class _HttpxShim:
        AsyncClient = staticmethod(lambda *a, **kw: _StubClient())
        Response = httpx.Response
        TimeoutException = httpx.TimeoutException
        ConnectError = httpx.ConnectError

    import polaris_api.services.booking.client as mod

    monkeypatch.setattr(mod, "httpx", _HttpxShim())
    return captured


@pytest.fixture
def client() -> EABookingClient:
    return EABookingClient(
        base_url=_BASE,
        jwt_signing_secret=_SECRET,
        jwt_audience="ea-fork",
        jwt_issuer="polaris",
        jwt_ttl_seconds=300,
        timeout_s=1.0,
        max_retries=0,
    )


def _decode_bearer(headers: dict[str, str]) -> dict[str, Any]:
    auth = headers.get("Authorization", "")
    assert auth.startswith("Bearer "), f"missing Bearer token, got {auth!r}"
    token = auth[len("Bearer ") :]
    return pyjwt.decode(
        token, _SECRET, algorithms=["HS256"], audience="ea-fork"
    )


# =====================================================================
# Config / fail-closed
# =====================================================================


@pytest.mark.asyncio
async def test_placeholder_secret_raises_config_error(fake_http: Any) -> None:
    bad = EABookingClient(
        base_url=_BASE,
        jwt_signing_secret="placeholder-ea-jwt-secret",
        jwt_audience="ea-fork",
        jwt_issuer="polaris",
        jwt_ttl_seconds=300,
        timeout_s=1.0,
        max_retries=0,
    )
    with pytest.raises(EABookingConfigError):
        await bad.list_services(tenant_id=uuid4())


@pytest.mark.asyncio
async def test_empty_base_url_raises_config_error(fake_http: Any) -> None:
    bad = EABookingClient(
        base_url="",
        jwt_signing_secret=_SECRET,
        jwt_audience="ea-fork",
        jwt_issuer="polaris",
        jwt_ttl_seconds=300,
        timeout_s=1.0,
        max_retries=0,
    )
    with pytest.raises(EABookingConfigError):
        await bad.list_services(tenant_id=uuid4())


# =====================================================================
# JWT shape
# =====================================================================


@pytest.mark.asyncio
async def test_jwt_carries_tenant_id_and_audience(
    client: EABookingClient, fake_http: Any
) -> None:
    tenant_id = uuid4()
    fake_http["responses"] = [httpx.Response(200, json=[])]
    await client.list_services(tenant_id=tenant_id)

    headers = fake_http["calls"][0]["headers"]
    decoded = _decode_bearer(headers)
    assert decoded["tenant_id"] == str(tenant_id)
    assert decoded["aud"] == "ea-fork"
    assert decoded["iss"] == "polaris"
    assert "exp" in decoded
    assert "iat" in decoded


@pytest.mark.asyncio
async def test_jwt_differs_per_tenant(
    client: EABookingClient, fake_http: Any
) -> None:
    """Same client, two tenants → distinct JWTs with distinct tenant_id claims.

    Polaris-side guarantee: the client never reuses a JWT across tenant
    contexts. The EA fork's server-side enforcement is verified in the
    §6 cross-tenant isolation sweep against the booted image."""
    tenant_a = uuid4()
    tenant_b = uuid4()
    fake_http["responses"] = [
        httpx.Response(200, json=[]),
        httpx.Response(200, json=[]),
    ]
    await client.list_services(tenant_id=tenant_a)
    await client.list_services(tenant_id=tenant_b)

    tok_a = _decode_bearer(fake_http["calls"][0]["headers"])
    tok_b = _decode_bearer(fake_http["calls"][1]["headers"])
    assert tok_a["tenant_id"] == str(tenant_a)
    assert tok_b["tenant_id"] == str(tenant_b)
    assert tok_a["tenant_id"] != tok_b["tenant_id"]


# =====================================================================
# Happy paths
# =====================================================================


@pytest.mark.asyncio
async def test_list_services_happy(
    client: EABookingClient, fake_http: Any
) -> None:
    fake_http["responses"] = [
        httpx.Response(
            200, json=[{"id": 1, "name": "Consultation", "duration": 30}]
        )
    ]
    services = await client.list_services(tenant_id=uuid4())
    assert len(services) == 1
    assert services[0]["name"] == "Consultation"
    call = fake_http["calls"][0]
    assert call["method"] == "GET"
    assert call["url"].endswith("/index.php/api/v1/services")


@pytest.mark.asyncio
async def test_list_availabilities_happy(
    client: EABookingClient, fake_http: Any
) -> None:
    fake_http["responses"] = [
        httpx.Response(200, json=["2026-06-01T09:00:00", "2026-06-01T10:00:00"])
    ]
    slots = await client.list_availabilities(
        tenant_id=uuid4(),
        service_id=1,
        provider_id=2,
        date_from=date(2026, 6, 1),
        date_to=date(2026, 6, 7),
    )
    assert len(slots) == 2
    call = fake_http["calls"][0]
    assert call["method"] == "GET"
    assert call["url"].endswith("/index.php/api/v1/availabilities")
    assert call["params"].get("service_id") == 1
    assert call["params"].get("provider_id") == 2


@pytest.mark.asyncio
async def test_create_appointment_happy(
    client: EABookingClient, fake_http: Any
) -> None:
    fake_http["responses"] = [
        httpx.Response(
            201,
            json={
                "id": 42,
                "service_id": 1,
                "provider_id": 2,
                "customer_id": 99,
                "start_datetime": "2026-06-01T09:00:00",
                "end_datetime": "2026-06-01T09:30:00",
            },
        )
    ]
    appointment = await client.create_appointment(
        tenant_id=uuid4(),
        service_id=1,
        provider_id=2,
        customer={
            "first_name": "Alice",
            "last_name": "Doe",
            "email": "alice@example.com",
            "phone": "60123456789",
        },
        start_datetime="2026-06-01T09:00:00",
        end_datetime="2026-06-01T09:30:00",
        notes="Booked via Polaris bot",
    )
    assert appointment["id"] == 42
    call = fake_http["calls"][0]
    assert call["method"] == "POST"
    assert call["url"].endswith("/index.php/api/v1/appointments")
    assert call["json"]["service_id"] == 1


@pytest.mark.asyncio
async def test_cancel_appointment_happy(
    client: EABookingClient, fake_http: Any
) -> None:
    fake_http["responses"] = [httpx.Response(204, text="")]
    await client.cancel_appointment(tenant_id=uuid4(), appointment_id=42)
    call = fake_http["calls"][0]
    assert call["method"] == "DELETE"
    assert call["url"].endswith("/index.php/api/v1/appointments/42")


@pytest.mark.asyncio
async def test_reschedule_appointment_happy(
    client: EABookingClient, fake_http: Any
) -> None:
    fake_http["responses"] = [
        httpx.Response(
            200,
            json={
                "id": 42,
                "start_datetime": "2026-06-02T09:00:00",
                "end_datetime": "2026-06-02T09:30:00",
            },
        )
    ]
    updated = await client.reschedule_appointment(
        tenant_id=uuid4(),
        appointment_id=42,
        start_datetime="2026-06-02T09:00:00",
        end_datetime="2026-06-02T09:30:00",
    )
    assert updated["start_datetime"] == "2026-06-02T09:00:00"
    call = fake_http["calls"][0]
    assert call["method"] == "PUT"
    assert call["url"].endswith("/index.php/api/v1/appointments/42")


@pytest.mark.asyncio
async def test_get_settings_happy(
    client: EABookingClient, fake_http: Any
) -> None:
    fake_http["responses"] = [
        httpx.Response(
            200,
            json=[
                {"name": "book_advance_timeout", "value": "1440"},
                {"name": "default_timezone", "value": "Asia/Kuala_Lumpur"},
            ],
        )
    ]
    settings = await client.get_settings(tenant_id=uuid4())
    assert {s["name"] for s in settings} == {
        "book_advance_timeout",
        "default_timezone",
    }
    call = fake_http["calls"][0]
    assert call["method"] == "GET"
    assert call["url"].endswith("/index.php/api/v1/settings")
    # Must override EA's default_length=20 page cap, else later-sorted settings
    # (default_timezone, future_booking_limit) get truncated out of the mirror.
    assert call["params"].get("length") == 1000


@pytest.mark.asyncio
async def test_get_settings_non_array_returns_empty(
    client: EABookingClient, fake_http: Any
) -> None:
    fake_http["responses"] = [httpx.Response(200, json={"oops": True})]
    assert await client.get_settings(tenant_id=uuid4()) == []


@pytest.mark.asyncio
async def test_list_blocked_periods_happy(
    client: EABookingClient, fake_http: Any
) -> None:
    fake_http["responses"] = [
        httpx.Response(
            200,
            json=[
                {
                    "id": 1,
                    "name": "Holiday",
                    "start": "2026-12-25 00:00:00",
                    "end": "2026-12-25 23:59:00",
                    "notes": "closed",
                }
            ],
        )
    ]
    periods = await client.list_blocked_periods(tenant_id=uuid4())
    assert periods[0]["name"] == "Holiday"
    call = fake_http["calls"][0]
    assert call["method"] == "GET"
    assert call["url"].endswith("/index.php/api/v1/blocked_periods")
    assert call["params"].get("length") == 1000


# =====================================================================
# Error classification
# =====================================================================


@pytest.mark.asyncio
async def test_4xx_raises_structural(
    client: EABookingClient, fake_http: Any
) -> None:
    fake_http["responses"] = [
        httpx.Response(
            404,
            json={"code": 404, "message": "The requested record was not found!"},
        )
    ]
    with pytest.raises(EABookingStructuralError) as exc:
        await client.cancel_appointment(tenant_id=uuid4(), appointment_id=99999)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_5xx_raises_transient(
    client: EABookingClient, fake_http: Any
) -> None:
    fake_http["responses"] = [httpx.Response(503, text="Service Unavailable")]
    with pytest.raises(EABookingTransientError) as exc:
        await client.list_services(tenant_id=uuid4())
    assert exc.value.status_code == 503


@pytest.mark.asyncio
async def test_timeout_classified_transient(
    client: EABookingClient, fake_http: Any
) -> None:
    fake_http["responses"] = [httpx.TimeoutException("read timeout")]
    with pytest.raises(EABookingTransientError):
        await client.list_services(tenant_id=uuid4())


# =====================================================================
# Customer URL composer
# =====================================================================


def test_public_booking_url_composes_slug() -> None:
    url = public_booking_url(base_url=_BASE, tenant_slug="acme")
    assert url == "https://calendar.getpolaris.my/book/acme"


def test_public_booking_url_rejects_empty_slug() -> None:
    with pytest.raises(ValueError):
        public_booking_url(base_url=_BASE, tenant_slug="")


def test_public_booking_url_rejects_path_traversal() -> None:
    with pytest.raises(ValueError):
        public_booking_url(base_url=_BASE, tenant_slug="../etc/passwd")


def test_public_booking_url_strips_trailing_slash_in_base() -> None:
    url = public_booking_url(base_url=_BASE + "/", tenant_slug="acme")
    assert url == "https://calendar.getpolaris.my/book/acme"


# =====================================================================
# provision_admin payload — optional company_* / timezone fields
# =====================================================================


@pytest.mark.asyncio
async def test_provision_admin_includes_set_optionals_omits_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Optional company_name/company_link/timezone are added to the HMAC-signed
    body only when non-empty, keeping the wire format backward-compatible with
    older EA images (mirrors the existing ``username`` behaviour)."""
    import json as _json

    captured: dict[str, Any] = {}

    class _Stub:
        async def __aenter__(self) -> _Stub:
            return self

        async def __aexit__(self, *a: Any) -> None:
            return None

        async def request(
            self,
            method: str,
            url: str,
            *,
            content: bytes | None = None,
            headers: dict[str, str] | None = None,
            timeout: float | None = None,  # noqa: ARG002, ASYNC109 — B6 shared-client kwarg
        ) -> httpx.Response:
            captured["body"] = _json.loads(content or b"{}")
            return httpx.Response(
                201,
                json={
                    "admin_user_id": 1,
                    "provider_id": 2,
                    "service_id": 3,
                    "username": "admin-acme",
                },
            )

    class _Shim:
        AsyncClient = staticmethod(lambda *a, **kw: _Stub())
        Response = httpx.Response
        TimeoutException = httpx.TimeoutException
        ConnectError = httpx.ConnectError

    import polaris_api.services.booking.client as mod

    monkeypatch.setattr(mod, "httpx", _Shim())

    c = EABookingClient(
        base_url=_BASE,
        jwt_signing_secret=_SECRET,
        jwt_audience="ea-fork",
        jwt_issuer="polaris",
        jwt_ttl_seconds=300,
        timeout_s=1.0,
        max_retries=0,
        provisioning_hmac_secret="test-ea-provisioning-hmac-not-placeholder",
    )

    # All optionals set → present in the body.
    await c.provision_admin(
        tenant_id=uuid4(),
        slug="acme",
        email="a@acme.my",
        first_name="Jane",
        last_name="Tan",
        company_name="Acme Spa",
        company_link="https://acme.my",
        timezone="Asia/Kuala_Lumpur",
    )
    body = captured["body"]
    assert body["company_name"] == "Acme Spa"
    assert body["company_link"] == "https://acme.my"
    assert body["timezone"] == "Asia/Kuala_Lumpur"

    # None set → keys omitted entirely.
    await c.provision_admin(
        tenant_id=uuid4(),
        slug="acme",
        email="a@acme.my",
        first_name="Jane",
        last_name="Tan",
    )
    body2 = captured["body"]
    assert "company_name" not in body2
    assert "company_link" not in body2
    assert "timezone" not in body2


@pytest.mark.asyncio
async def test_sync_tenant_wire_format(monkeypatch: pytest.MonkeyPatch) -> None:
    """sync_tenant POSTs the HMAC-signed slug-routing body to the EA sync
    endpoint; on a 2xx ``{ok:true}`` it returns the parsed body."""
    import json as _json

    captured: dict[str, Any] = {}

    class _Stub:
        async def __aenter__(self) -> _Stub:
            return self

        async def __aexit__(self, *a: Any) -> None:
            return None

        async def request(
            self,
            method: str,
            url: str,
            *,
            content: bytes | None = None,
            headers: dict[str, str] | None = None,
            timeout: float | None = None,  # noqa: ARG002, ASYNC109 — B6 shared-client kwarg
        ) -> httpx.Response:
            captured["method"] = method
            captured["url"] = url
            captured["headers"] = headers or {}
            captured["body"] = _json.loads(content or b"{}")
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "tenant_id": captured["body"].get("tenant_id"),
                    "slug": captured["body"].get("slug"),
                    "status": captured["body"].get("status"),
                },
            )

    class _Shim:
        AsyncClient = staticmethod(lambda *a, **kw: _Stub())
        Response = httpx.Response
        TimeoutException = httpx.TimeoutException
        ConnectError = httpx.ConnectError

    import polaris_api.services.booking.client as mod

    monkeypatch.setattr(mod, "httpx", _Shim())

    c = EABookingClient(
        base_url=_BASE,
        jwt_signing_secret=_SECRET,
        jwt_audience="ea-fork",
        jwt_issuer="polaris",
        jwt_ttl_seconds=300,
        timeout_s=1.0,
        max_retries=0,
        provisioning_hmac_secret="test-ea-provisioning-hmac-not-placeholder",
    )

    tid = uuid4()
    out = await c.sync_tenant(tenant_id=tid, slug="acme")
    assert out["ok"] is True
    assert captured["method"] == "POST"
    assert captured["url"].endswith("/index.php/api/v1/internal/tenants/sync")
    # HMAC-signed (covers the exact bytes).
    assert captured["headers"].get("X-Polaris-Signature", "").startswith("sha256=")
    body = captured["body"]
    assert body["tenant_id"] == str(tid)
    assert body["slug"] == "acme"
    assert body["status"] == "active"  # default
