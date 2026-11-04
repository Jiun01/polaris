"""Tests for GET /b/{code} — booking short-link redirect (v2/phase-13).

Fakes the service pool so no DB is needed; ``mint_binding_token`` and
``get_settings`` are patched. Covers:
  * valid pending row → 307 to the public EA wizard with polaris_ref + ids
  * WhatsApp adds prefill_phone; other channels don't
  * unknown / expired jti → 410
  * NULL ea ids (pre-migration-117 row) → 410
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from polaris_api.main import app

pytestmark = pytest.mark.asyncio(loop_scope="function")

_JTI = "a" * 32
_REDIRECT = "polaris_api.routers.booking_redirect"


def _make_pool(row: dict[str, Any] | None) -> MagicMock:
    """Fake service pool whose acquired conn.fetchrow returns ``row``."""
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=row)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=conn)
    cm.__aexit__ = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=cm)
    return pool


def _valid_row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "booking_id": uuid4(),
        "tenant_id": uuid4(),
        "ea_service_id": 3,
        "ea_provider_id": 7,
        "channel": "whatsapp",
        "customer_handle": "60123456789",
        "binding_expires_at": datetime.now(UTC) + timedelta(minutes=45),
        "tenant_slug": "acme-clinic",
    }
    row.update(overrides)
    return row


def _settings() -> MagicMock:
    s = MagicMock()
    s.ea_booking_public_url = "https://calendar.example.com"
    return s


def _patches(row: dict[str, Any] | None, *, token: str = "TESTTOK") -> tuple[Any, ...]:  # noqa: S107
    return (
        patch(f"{_REDIRECT}.get_service_pool", return_value=_make_pool(row)),
        patch(f"{_REDIRECT}.get_settings", return_value=_settings()),
        patch(f"{_REDIRECT}.mint_binding_token", return_value=token),
    )


@pytest_asyncio.fixture(loop_scope="function")
async def client() -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=False
    ) as c:
        yield c


async def test_redirect_success_307(client: AsyncClient) -> None:
    p1, p2, p3 = _patches(_valid_row())
    with p1, p2, p3:
        resp = await client.get(f"/b/{_JTI}")
    assert resp.status_code == 307
    loc = resp.headers["location"]
    assert loc.startswith("https://calendar.example.com/book/acme-clinic?")
    assert "service_id=3" in loc
    assert "provider_id=7" in loc
    assert "polaris_ref=TESTTOK" in loc
    assert "prefill_phone=60123456789" in loc  # whatsapp


async def test_redirect_non_whatsapp_has_no_prefill(client: AsyncClient) -> None:
    p1, p2, p3 = _patches(_valid_row(channel="telegram"))
    with p1, p2, p3:
        resp = await client.get(f"/b/{_JTI}")
    assert resp.status_code == 307
    assert "prefill_phone" not in resp.headers["location"]


async def test_redirect_unknown_or_expired_410(client: AsyncClient) -> None:
    p1, p2, p3 = _patches(None)  # no matching pending row
    with p1, p2, p3:
        resp = await client.get(f"/b/{_JTI}")
    assert resp.status_code == 410
    assert "expired" in resp.text.lower()


async def test_redirect_null_ea_ids_410(client: AsyncClient) -> None:
    # Pre-migration-117 row: ids NULL → can't rebuild the wizard URL.
    p1, p2, p3 = _patches(_valid_row(ea_service_id=None))
    with p1, p2, p3:
        resp = await client.get(f"/b/{_JTI}")
    assert resp.status_code == 410
