"""Off-Meta channels M1 — unit tests for services/telegram_client.py.

DB-free + network-free: monkeypatches ``telegram_client.httpx.AsyncClient`` with
a stub that returns programmed responses, mirroring the meta_whatsapp test
approach (this module's URL shape differs, so it uses its own minimal stub).

Covers:
  - send_message → POSTs sendMessage, returns result.message_id (stringified)
  - send_photo → POSTs sendPhoto with chat_id/photo/caption
  - non-2xx → TelegramError carrying status_code
  - 2xx ok=false → TelegramError
  - 2xx missing result.message_id → TelegramError
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from polaris_api.services import telegram_client as tg_mod
from polaris_api.services.telegram_client import TelegramClient, TelegramError


class _StubAsyncClient:
    """Async-context-manager stub for httpx.AsyncClient."""

    def __init__(self, response: httpx.Response, calls: list[dict[str, Any]]) -> None:
        self._response = response
        self._calls = calls

    async def __aenter__(self) -> _StubAsyncClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def post(self, url: str, *, json: dict[str, Any] | None = None) -> httpx.Response:
        self._calls.append({"url": url, "json": json})
        return self._response


@pytest.fixture
def patched(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    """Return a helper that pins the next httpx response + captures calls."""
    calls: list[dict[str, Any]] = []

    def _install(response: httpx.Response) -> list[dict[str, Any]]:
        class _Httpx:
            AsyncClient = staticmethod(lambda *a, **k: _StubAsyncClient(response, calls))
            Response = httpx.Response

        monkeypatch.setattr(tg_mod, "httpx", _Httpx)
        return calls

    return _install


@pytest.mark.asyncio
async def test_send_message_returns_message_id(patched) -> None:  # type: ignore[no-untyped-def]
    calls = patched(httpx.Response(200, json={"ok": True, "result": {"message_id": 9001}}))
    client = TelegramClient(base_url="https://api.telegram.org")
    out = await client.send_message(token="T0KEN", chat_id="4242", text="hi")
    assert out == "9001"
    assert calls[0]["url"] == "https://api.telegram.org/botT0KEN/sendMessage"
    assert calls[0]["json"] == {"chat_id": "4242", "text": "hi"}


@pytest.mark.asyncio
async def test_send_photo_includes_caption(patched) -> None:  # type: ignore[no-untyped-def]
    calls = patched(httpx.Response(200, json={"ok": True, "result": {"message_id": 5}}))
    client = TelegramClient()
    out = await client.send_photo(
        token="T0KEN", chat_id="4242", photo_url="https://img/x.png", caption="cap"
    )
    assert out == "5"
    assert calls[0]["url"].endswith("/botT0KEN/sendPhoto")
    assert calls[0]["json"] == {"chat_id": "4242", "photo": "https://img/x.png", "caption": "cap"}


@pytest.mark.asyncio
async def test_non_2xx_raises_with_status_code(patched) -> None:  # type: ignore[no-untyped-def]
    patched(httpx.Response(403, json={"ok": False, "error_code": 403, "description": "blocked"}))
    client = TelegramClient()
    with pytest.raises(TelegramError) as exc:
        await client.send_message(token="T0KEN", chat_id="4242", text="hi")
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_2xx_ok_false_raises(patched) -> None:  # type: ignore[no-untyped-def]
    patched(httpx.Response(200, json={"ok": False}))
    client = TelegramClient()
    with pytest.raises(TelegramError):
        await client.send_message(token="T0KEN", chat_id="4242", text="hi")


@pytest.mark.asyncio
async def test_2xx_missing_message_id_raises(patched) -> None:  # type: ignore[no-untyped-def]
    patched(httpx.Response(200, json={"ok": True, "result": {}}))
    client = TelegramClient()
    with pytest.raises(TelegramError):
        await client.send_message(token="T0KEN", chat_id="4242", text="hi")


@pytest.mark.asyncio
async def test_get_me_returns_bot_object(patched) -> None:  # type: ignore[no-untyped-def]
    patched(httpx.Response(200, json={"ok": True, "result": {"id": 42, "username": "b"}}))
    client = TelegramClient()
    me = await client.get_me(token="T0KEN")
    assert me["id"] == 42
    assert me["username"] == "b"


@pytest.mark.asyncio
async def test_get_me_bad_token_raises(patched) -> None:  # type: ignore[no-untyped-def]
    patched(httpx.Response(401, json={"ok": False, "error_code": 401}))
    client = TelegramClient()
    with pytest.raises(TelegramError) as exc:
        await client.get_me(token="BAD")
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_set_webhook_posts_url_and_secret(patched) -> None:  # type: ignore[no-untyped-def]
    calls = patched(httpx.Response(200, json={"ok": True, "result": True}))
    client = TelegramClient()
    ok = await client.set_webhook(
        token="T0KEN", url="https://api.x/v1/webhooks/telegram/sec", secret_token="sec"
    )
    assert ok is True
    assert calls[0]["url"].endswith("/botT0KEN/setWebhook")
    assert calls[0]["json"] == {
        "url": "https://api.x/v1/webhooks/telegram/sec",
        "secret_token": "sec",
    }


@pytest.mark.asyncio
async def test_network_error_raises_telegramerror_without_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A network-level httpx.RequestError must surface as TelegramError(status=0)
    and must NOT leak the token-bearing URL via the exception chain."""

    class _Raising:
        async def __aenter__(self) -> _Raising:
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        async def post(self, url: str, *, json: dict | None = None) -> httpx.Response:
            raise httpx.ConnectError("boom", request=httpx.Request("POST", url))

    class _Httpx:
        AsyncClient = staticmethod(lambda *a, **k: _Raising())
        Response = httpx.Response
        RequestError = httpx.RequestError

    monkeypatch.setattr(tg_mod, "httpx", _Httpx)
    client = TelegramClient()
    with pytest.raises(TelegramError) as exc:
        await client.send_message(token="SECRET_T0KEN", chat_id="4242", text="hi")
    assert exc.value.status_code == 0
    # `from None` suppression: no __cause__ carrying the token URL.
    assert exc.value.__cause__ is None
    assert "SECRET_T0KEN" not in str(exc.value)
