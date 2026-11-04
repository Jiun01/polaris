"""Cross-tenant isolation tests for the EA booking client — Phase 7 PR-6.

Mirrors the Pre-Phase-7 §6 E2E cross-tenant sweep against the booted
EA fork image (which is the AUTHORITATIVE gate). These Polaris-side
tests verify the contract Polaris MUST HONOR for that sweep to be
meaningful:

  1. Different tenant_id args → distinct JWT tenant_id claims
  2. Forging a JWT for tenant B by re-signing with the wrong secret
     produces a token EA will reject (we can't test EA's rejection
     here without booting the image, but we CAN verify the signature
     check catches it)
  3. Same client instance, alternating tenant calls — no claim leak
     across requests
  4. JWT expires (short TTL) — replay window bounded

The EA-side enforcement (MY_Model tenant scoping on every SELECT
+ INSERT + UPDATE + DELETE) is verified separately in the EA fork's
§6.5/§6.6 E2E sweep against ghcr.io/jiun01/polaris-ea:v1.5.2-r1.
These tests guarantee Polaris doesn't sabotage that contract by
sending malformed tokens or reusing claims.

Per ADR-024: tenant isolation is a SHARED responsibility — EA enforces,
Polaris doesn't bypass.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import httpx
import jwt as pyjwt
import pytest
from polaris_api.services.booking.client import EABookingClient

_BASE = "https://calendar.getpolaris.my"
_SECRET = "cross-tenant-isolation-test-secret"
_WRONG_SECRET = "attacker-controlled-different-secret"


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
                    "headers": headers or {},
                    "params": params or {},
                    "json": json,
                }
            )
            return captured["responses"].pop(0)

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


def _decode(headers: dict[str, str], secret: str = _SECRET) -> dict[str, Any]:
    token = headers["Authorization"][len("Bearer ") :]
    return pyjwt.decode(token, secret, algorithms=["HS256"], audience="ea-fork")


# =====================================================================
# Property 1 — distinct tenant_id args produce distinct JWT claims
# =====================================================================


@pytest.mark.asyncio
async def test_two_tenants_get_distinct_jwt_claims(
    client: EABookingClient, fake_http: Any
) -> None:
    """50-fuzz on tenant pairs: every (A, B) call sequence emits two
    JWTs with DIFFERENT tenant_id claims. This is the property the EA
    fork relies on for MY_Model scoping."""
    pairs = [(uuid4(), uuid4()) for _ in range(50)]
    fake_http["responses"] = [httpx.Response(200, json=[])] * (len(pairs) * 2)

    for tenant_a, tenant_b in pairs:
        await client.list_services(tenant_id=tenant_a)
        await client.list_services(tenant_id=tenant_b)

    # Pair off captured calls and verify tenant_id distinctness.
    for i, (tenant_a, tenant_b) in enumerate(pairs):
        claim_a = _decode(fake_http["calls"][i * 2]["headers"])
        claim_b = _decode(fake_http["calls"][i * 2 + 1]["headers"])
        assert claim_a["tenant_id"] == str(tenant_a)
        assert claim_b["tenant_id"] == str(tenant_b)
        assert claim_a["tenant_id"] != claim_b["tenant_id"]


# =====================================================================
# Property 2 — signature verification catches wrong-secret forgeries
# =====================================================================


def test_wrong_secret_signed_token_fails_verification() -> None:
    """An attacker who knows the JWT claim shape but doesn't have the
    signing secret cannot produce a token EA will accept. The EA-side
    PHP verifier (firebase/php-jwt) checks the signature; we mirror
    that check here so we KNOW our token format produces signatures
    that fail validation under a wrong secret."""
    now = datetime.now(UTC)
    forged_payload = {
        "tenant_id": str(uuid4()),
        "aud": "ea-fork",
        "iss": "polaris",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=300)).timestamp()),
    }
    forged_token = pyjwt.encode(
        forged_payload, _WRONG_SECRET, algorithm="HS256"
    )
    # Verify with the LEGIT secret — must reject the wrong-secret signature.
    with pytest.raises(pyjwt.InvalidSignatureError):
        pyjwt.decode(forged_token, _SECRET, algorithms=["HS256"], audience="ea-fork")


# =====================================================================
# Property 3 — alternating calls don't leak claims across requests
# =====================================================================


@pytest.mark.asyncio
async def test_alternating_tenant_calls_no_claim_leak(
    client: EABookingClient, fake_http: Any
) -> None:
    """Same client instance, A→B→A→B alternation. Each request must
    carry the JWT claim for its OWN tenant_id, never a stale value
    from a previous call. Protects against shared mutable state in
    the client."""
    tenant_a = uuid4()
    tenant_b = uuid4()
    fake_http["responses"] = [httpx.Response(200, json=[])] * 4

    await client.list_services(tenant_id=tenant_a)
    await client.list_services(tenant_id=tenant_b)
    await client.list_services(tenant_id=tenant_a)
    await client.list_services(tenant_id=tenant_b)

    claims = [_decode(c["headers"]) for c in fake_http["calls"]]
    assert claims[0]["tenant_id"] == str(tenant_a)
    assert claims[1]["tenant_id"] == str(tenant_b)
    assert claims[2]["tenant_id"] == str(tenant_a)
    assert claims[3]["tenant_id"] == str(tenant_b)


# =====================================================================
# Property 4 — JWT TTL bounds replay window
# =====================================================================


def test_jwt_exp_within_configured_ttl() -> None:
    """Tokens carry exp ≤ now + ttl_seconds + 1s clock slop. Bounds
    replay if a token is exfiltrated."""
    ttl = 60
    client = EABookingClient(
        base_url=_BASE,
        jwt_signing_secret=_SECRET,
        jwt_audience="ea-fork",
        jwt_issuer="polaris",
        jwt_ttl_seconds=ttl,
        timeout_s=1.0,
        max_retries=0,
    )
    tenant_id = uuid4()
    before = datetime.now(UTC)
    token = client._mint_jwt(tenant_id)
    decoded = pyjwt.decode(
        token, _SECRET, algorithms=["HS256"], audience="ea-fork"
    )
    expected_max_exp = int((before + timedelta(seconds=ttl + 1)).timestamp())
    assert decoded["exp"] <= expected_max_exp
    assert decoded["exp"] > int(before.timestamp())


# =====================================================================
# Property 5 — every verb attaches a tenant-scoped JWT (no bypass)
# =====================================================================


@pytest.mark.asyncio
async def test_every_verb_carries_tenant_scoped_jwt(
    client: EABookingClient, fake_http: Any
) -> None:
    """All 5 EA verbs MUST attach a JWT carrying the caller's tenant_id.
    If a future refactor accidentally bypasses _headers() on one verb,
    this test catches the regression — the EA side would otherwise
    accept the call with NO tenant context (a cross-tenant leak risk)."""
    tenant = uuid4()
    fake_http["responses"] = [
        httpx.Response(200, json=[]),                          # list_services
        httpx.Response(200, json=[]),                          # list_availabilities
        httpx.Response(201, json={"id": 1}),                   # create_appointment
        httpx.Response(204, text=""),                          # cancel_appointment
        httpx.Response(200, json={"id": 1}),                   # reschedule_appointment
    ]

    from datetime import date

    await client.list_services(tenant_id=tenant)
    await client.list_availabilities(
        tenant_id=tenant,
        service_id=1,
        provider_id=2,
        date_from=date(2026, 6, 1),
        date_to=date(2026, 6, 7),
    )
    await client.create_appointment(
        tenant_id=tenant,
        service_id=1,
        provider_id=2,
        customer={"first_name": "A", "last_name": "B", "email": "a@b.c", "phone": "0"},
        start_datetime="2026-06-01T09:00:00",
        end_datetime="2026-06-01T09:30:00",
    )
    await client.cancel_appointment(tenant_id=tenant, appointment_id=1)
    await client.reschedule_appointment(
        tenant_id=tenant,
        appointment_id=1,
        start_datetime="2026-06-02T09:00:00",
        end_datetime="2026-06-02T09:30:00",
    )

    assert len(fake_http["calls"]) == 5
    for call in fake_http["calls"]:
        claim = _decode(call["headers"])
        assert claim["tenant_id"] == str(tenant), (
            f"verb {call['method']} {call['url']} carried wrong tenant_id"
        )
