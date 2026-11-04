"""Tests for ChatwootClient.get_channel_token — channel auto-provisioning.

Covers:
  - 200 response returns the token string.
  - 401 / 404 / non-200 responses return None.
  - Network failure (timeout, ConnectError) returns None.
  - Missing config (empty internal_token or internal_base_url) returns None.
  - Token value is NEVER present in log output.
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from polaris_api.services.chatwoot.client import ChatwootClient

pytestmark = pytest.mark.asyncio(loop_scope="function")

_INTERNAL_BASE = "http://chatwoot-rails:3000"
_INTERNAL_TOKEN = "super-secret-internal-token-xyz"
_INBOX_ID = 42
_ACCOUNT_ID = 7
_SAMPLE_TOKEN = "EAABsbC..." + "x" * 40  # realistic-looking token, not logged


def _make_client() -> ChatwootClient:
    """A ChatwootClient configured for tests (non-placeholder token)."""
    return ChatwootClient(
        base_url="https://chat.getpolaris.my",
        bot_access_token="test-token-not-placeholder",
        agent_api_token="test-user-token-not-placeholder",
        timeout_s=1.0,
        max_retries=0,
        label_auto_provision=False,
    )


def _mock_settings(*, internal_token: str = _INTERNAL_TOKEN, internal_base: str = _INTERNAL_BASE) -> Any:
    """Return a MagicMock that looks like get_settings() for the values we need."""
    s = MagicMock()
    s.chatwoot_internal_token = internal_token
    s.chatwoot_internal_base_url = internal_base
    return s


def _fake_http_response(status_code: int, json_body: dict[str, Any] | None = None) -> httpx.Response:
    if json_body is not None:
        return httpx.Response(status_code, json=json_body)
    return httpx.Response(status_code, text="")


# =====================================================================
# Happy path
# =====================================================================


async def test_200_returns_token() -> None:
    """A 200 response with {token: '...'} returns the token string."""
    client = _make_client()

    mock_response = _fake_http_response(200, {"channel_type": "Channel::Whatsapp", "token": _SAMPLE_TOKEN})
    mock_async_client = AsyncMock()
    mock_async_client.__aenter__ = AsyncMock(return_value=mock_async_client)
    mock_async_client.__aexit__ = AsyncMock(return_value=None)
    mock_async_client.get = AsyncMock(return_value=mock_response)

    with (
        patch("polaris_api.services.chatwoot.client.get_settings", return_value=_mock_settings()),
        patch("polaris_api.services.chatwoot.client.httpx.AsyncClient", return_value=mock_async_client),
    ):
        token = await client.get_channel_token(account_id=_ACCOUNT_ID, inbox_id=_INBOX_ID)

    assert token == _SAMPLE_TOKEN


async def test_200_correct_url_and_header() -> None:
    """The GET is issued to the correct internal URL with the X-Polaris-Internal-Token header."""
    client = _make_client()

    mock_response = _fake_http_response(200, {"token": _SAMPLE_TOKEN})
    mock_async_client = AsyncMock()
    mock_async_client.__aenter__ = AsyncMock(return_value=mock_async_client)
    mock_async_client.__aexit__ = AsyncMock(return_value=None)
    mock_async_client.get = AsyncMock(return_value=mock_response)

    with (
        patch("polaris_api.services.chatwoot.client.get_settings", return_value=_mock_settings()),
        patch("polaris_api.services.chatwoot.client.httpx.AsyncClient", return_value=mock_async_client),
    ):
        await client.get_channel_token(account_id=_ACCOUNT_ID, inbox_id=_INBOX_ID)

    call_args = mock_async_client.get.call_args
    assert call_args is not None
    url_called = call_args.args[0] if call_args.args else call_args.kwargs.get("url")
    assert url_called == f"{_INTERNAL_BASE}/internal/inboxes/{_INBOX_ID}/token?account_id={_ACCOUNT_ID}"
    headers_sent = call_args.kwargs.get("headers", {})
    assert headers_sent.get("X-Polaris-Internal-Token") == _INTERNAL_TOKEN


# =====================================================================
# Non-200 responses → None
# =====================================================================


@pytest.mark.parametrize("status_code", [401, 404, 422, 500])
async def test_non_200_returns_none(status_code: int) -> None:
    """Any non-200 HTTP status code causes get_channel_token to return None."""
    client = _make_client()

    mock_response = _fake_http_response(status_code)
    mock_async_client = AsyncMock()
    mock_async_client.__aenter__ = AsyncMock(return_value=mock_async_client)
    mock_async_client.__aexit__ = AsyncMock(return_value=None)
    mock_async_client.get = AsyncMock(return_value=mock_response)

    with (
        patch("polaris_api.services.chatwoot.client.get_settings", return_value=_mock_settings()),
        patch("polaris_api.services.chatwoot.client.httpx.AsyncClient", return_value=mock_async_client),
    ):
        result = await client.get_channel_token(account_id=_ACCOUNT_ID, inbox_id=_INBOX_ID)

    assert result is None


# =====================================================================
# Network failures → None
# =====================================================================


async def test_timeout_returns_none() -> None:
    """Network timeout causes get_channel_token to return None (never raises)."""
    client = _make_client()

    mock_async_client = AsyncMock()
    mock_async_client.__aenter__ = AsyncMock(return_value=mock_async_client)
    mock_async_client.__aexit__ = AsyncMock(return_value=None)
    mock_async_client.get = AsyncMock(side_effect=httpx.TimeoutException("read timeout"))

    with (
        patch("polaris_api.services.chatwoot.client.get_settings", return_value=_mock_settings()),
        patch("polaris_api.services.chatwoot.client.httpx.AsyncClient", return_value=mock_async_client),
    ):
        result = await client.get_channel_token(account_id=_ACCOUNT_ID, inbox_id=_INBOX_ID)

    assert result is None


async def test_connect_error_returns_none() -> None:
    """Connection error causes get_channel_token to return None (never raises)."""
    client = _make_client()

    mock_async_client = AsyncMock()
    mock_async_client.__aenter__ = AsyncMock(return_value=mock_async_client)
    mock_async_client.__aexit__ = AsyncMock(return_value=None)
    mock_async_client.get = AsyncMock(side_effect=httpx.ConnectError("connection refused"))

    with (
        patch("polaris_api.services.chatwoot.client.get_settings", return_value=_mock_settings()),
        patch("polaris_api.services.chatwoot.client.httpx.AsyncClient", return_value=mock_async_client),
    ):
        result = await client.get_channel_token(account_id=_ACCOUNT_ID, inbox_id=_INBOX_ID)

    assert result is None


# =====================================================================
# Missing config → None
# =====================================================================


async def test_empty_internal_token_returns_none() -> None:
    """When CHATWOOT_INTERNAL_TOKEN is empty, return None without any HTTP call."""
    client = _make_client()

    with patch(
        "polaris_api.services.chatwoot.client.get_settings",
        return_value=_mock_settings(internal_token=""),
    ):
        result = await client.get_channel_token(account_id=_ACCOUNT_ID, inbox_id=_INBOX_ID)

    assert result is None


async def test_empty_internal_base_url_returns_none() -> None:
    """When CHATWOOT_INTERNAL_BASE_URL is empty, return None without any HTTP call."""
    client = _make_client()

    with patch(
        "polaris_api.services.chatwoot.client.get_settings",
        return_value=_mock_settings(internal_base=""),
    ):
        result = await client.get_channel_token(account_id=_ACCOUNT_ID, inbox_id=_INBOX_ID)

    assert result is None


# =====================================================================
# Malformed response bodies → None
# =====================================================================


async def test_invalid_json_returns_none() -> None:
    """Non-JSON response body returns None."""
    client = _make_client()

    mock_response = httpx.Response(200, text="not json {{{{")
    mock_async_client = AsyncMock()
    mock_async_client.__aenter__ = AsyncMock(return_value=mock_async_client)
    mock_async_client.__aexit__ = AsyncMock(return_value=None)
    mock_async_client.get = AsyncMock(return_value=mock_response)

    with (
        patch("polaris_api.services.chatwoot.client.get_settings", return_value=_mock_settings()),
        patch("polaris_api.services.chatwoot.client.httpx.AsyncClient", return_value=mock_async_client),
    ):
        result = await client.get_channel_token(account_id=_ACCOUNT_ID, inbox_id=_INBOX_ID)

    assert result is None


async def test_missing_token_field_returns_none() -> None:
    """Response with no 'token' key returns None."""
    client = _make_client()

    mock_response = _fake_http_response(200, {"channel_type": "Channel::Whatsapp"})
    mock_async_client = AsyncMock()
    mock_async_client.__aenter__ = AsyncMock(return_value=mock_async_client)
    mock_async_client.__aexit__ = AsyncMock(return_value=None)
    mock_async_client.get = AsyncMock(return_value=mock_response)

    with (
        patch("polaris_api.services.chatwoot.client.get_settings", return_value=_mock_settings()),
        patch("polaris_api.services.chatwoot.client.httpx.AsyncClient", return_value=mock_async_client),
    ):
        result = await client.get_channel_token(account_id=_ACCOUNT_ID, inbox_id=_INBOX_ID)

    assert result is None


# =====================================================================
# Token is never logged
# =====================================================================


async def test_token_value_not_logged(caplog: pytest.LogCaptureFixture) -> None:
    """The actual token value must never appear in log output (200 path)."""
    client = _make_client()

    mock_response = _fake_http_response(200, {"token": _SAMPLE_TOKEN})
    mock_async_client = AsyncMock()
    mock_async_client.__aenter__ = AsyncMock(return_value=mock_async_client)
    mock_async_client.__aexit__ = AsyncMock(return_value=None)
    mock_async_client.get = AsyncMock(return_value=mock_response)

    with (
        patch("polaris_api.services.chatwoot.client.get_settings", return_value=_mock_settings()),
        patch("polaris_api.services.chatwoot.client.httpx.AsyncClient", return_value=mock_async_client),
        caplog.at_level(logging.DEBUG, logger="polaris_api.services.chatwoot.client"),
    ):
        await client.get_channel_token(account_id=_ACCOUNT_ID, inbox_id=_INBOX_ID)

    for record in caplog.records:
        assert _SAMPLE_TOKEN not in record.getMessage(), (
            f"Token leaked into log: {record.getMessage()}"
        )
