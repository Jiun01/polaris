"""Off-Meta channels M3 — public widget config endpoint tests.

DB-free: mocks widget_service.get_public_config. Read-only design — the config
returns each channel's direct https target (the bubble links straight to it; no
server redirect). Non-allowlisted targets are filtered out (defence in depth).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from polaris_api.main import app
from polaris_api.services import widget as widget_service
from polaris_api.services.widget import WidgetConfigRow

_KEY = "pub_key_123"


def _config(channels: list[dict] | None = None) -> WidgetConfigRow:
    return WidgetConfigRow(
        tenant_id=uuid4(),
        enabled=True,
        greeting="Talk to us",
        channels=channels
        if channels is not None
        else [
            {"kind": "telegram", "label": "Telegram", "target": "https://t.me/acme_bot"},
            {"kind": "whatsapp", "label": "WhatsApp", "target": "https://wa.me/60123"},
        ],
    )


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.mark.asyncio
async def test_config_returns_channels_with_direct_targets(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(widget_service, "get_public_config", AsyncMock(return_value=_config()))
    resp = await client.get(f"/api/v1/widget/{_KEY}/config")
    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == "*"
    body = resp.json()
    assert body["greeting"] == "Talk to us"
    assert body["channels"] == [
        {"kind": "telegram", "label": "Telegram", "target": "https://t.me/acme_bot"},
        {"kind": "whatsapp", "label": "WhatsApp", "target": "https://wa.me/60123"},
    ]


@pytest.mark.asyncio
async def test_config_filters_non_allowlisted_targets(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    bad = _config(channels=[
        {"kind": "telegram", "label": "Telegram", "target": "https://t.me/ok"},
        {"kind": "x", "label": "Evil", "target": "https://evil.com/x"},
        {"kind": "y", "label": "JS", "target": "javascript:alert(1)"},
    ])
    monkeypatch.setattr(widget_service, "get_public_config", AsyncMock(return_value=bad))
    resp = await client.get(f"/api/v1/widget/{_KEY}/config")
    assert resp.status_code == 200
    targets = [c["target"] for c in resp.json()["channels"]]
    assert targets == ["https://t.me/ok"]  # evil.com + javascript: dropped


@pytest.mark.asyncio
async def test_config_unknown_or_disabled_404(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(widget_service, "get_public_config", AsyncMock(return_value=None))
    resp = await client.get(f"/api/v1/widget/{_KEY}/config")
    assert resp.status_code == 404
    assert "cache-control" not in resp.headers  # 404s are not cached
