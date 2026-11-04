"""Telegram Bot API client — off-Meta channels M1.

Adapter over the Telegram Bot API (https://core.telegram.org/bots/api). Pure
HTTP — does NOT read the database. The per-tenant bot token flows in from the
dispatch boundary (``pipeline._dispatch_reply`` decrypts it from
``tenant_secrets`` under kind ``telegram_bot_token``); this module's job is
exclusively wire-level, mirroring ``meta_whatsapp.MetaWhatsAppClient``.

Build-vs-buy (search-first verified): raw httpx, NOT python-telegram-bot /
aiogram. Those are receive-side frameworks (Dispatcher/Updater own the event
loop) that we'd 95% disable for a webhook-mode send-only client; aiogram also
drags in a second HTTP stack (aiohttp). A thin httpx adapter matches the house
style and is trivially mocked.

Bot API shape (context7-verified):
  - Method call:  POST https://api.telegram.org/bot<token>/<method>  (JSON body)
  - sendMessage:  {chat_id, text}            text 1-4096 chars
  - sendPhoto:    {chat_id, photo, caption?} photo = HTTP URL (or file_id);
                  caption 0-1024 chars
  - Success body: {"ok": true,  "result": {"message_id": <int>, ...}}
  - Error body:   {"ok": false, "error_code": <int>, "description": "<str>"}
    delivered with the matching HTTP status (4xx/5xx).
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from typing import Any, Final

import httpx

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT_SECONDS: Final[float] = 10.0
_DEFAULT_BASE_URL: Final[str] = "https://api.telegram.org"


class TelegramError(RuntimeError):
    """Wraps non-OK responses from the Bot API.

    Carries ``status_code`` for the dispatch layer to decide whether to retry
    (5xx) or surface as a hard failure (4xx — likely a bad token / blocked-by-
    user / chat-not-found, not a transient blip). ``description`` is Telegram's
    human-readable error string when present.
    """

    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


class TelegramClient:
    """Async HTTP client for the Telegram Bot API.

    Stateless beyond ``base_url`` — the bot token is passed per call (Telegram
    embeds it in the URL path, unlike Meta's Bearer header). Constructed via
    ``get_telegram_client()`` (lru_cache'd singleton); tests can pass an
    explicit ``base_url`` or monkeypatch the factory.
    """

    def __init__(self, *, base_url: str = _DEFAULT_BASE_URL) -> None:
        self.base_url = base_url.rstrip("/")

    def _url(self, token: str, method: str) -> str:
        return f"{self.base_url}/bot{token}/{method}"

    async def _request(
        self, *, token: str, method: str, payload: dict[str, Any]
    ) -> Any:
        """POST a Bot API method and return its ``result`` field.

        Raises ``TelegramError`` on network failure, non-2xx, or ok=false. The
        error body is NOT logged verbatim — Telegram echoes request content in
        some 4xx descriptions, which could surface user text in ops logs.
        """
        url = self._url(token, method)
        try:
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as client:
                response = await client.post(url, json=payload)
        except httpx.RequestError as exc:
            # Network-level failure (connect/timeout/protocol). httpx exceptions
            # embed the request URL — which carries the bot token in the path —
            # so we re-raise WITHOUT chaining (`from None`) and log only the
            # method, never the URL. status_code=0 = "no HTTP response". This
            # also keeps the error inside the pipeline's `except TelegramError`
            # handler instead of escaping uncaught to the worker.
            logger.warning("telegram %s network error: %s", method, type(exc).__name__)
            raise TelegramError(
                f"Telegram {method} network error: {type(exc).__name__}",
                status_code=0,
            ) from None

        if response.status_code >= 400:
            logger.warning(
                "telegram %s non-2xx: status=%d", method, response.status_code
            )
            raise TelegramError(
                f"Telegram {method} failed: HTTP {response.status_code}",
                status_code=response.status_code,
            )

        data = response.json()
        if not isinstance(data, dict) or not data.get("ok"):
            raise TelegramError(
                f"Telegram {method} returned ok=false on a 2xx response",
                status_code=response.status_code,
            )
        return data.get("result")

    async def send_message(self, *, token: str, chat_id: str, text: str) -> str:
        """Send a text message; returns the Telegram message_id (stringified)."""
        result = await self._request(
            token=token,
            method="sendMessage",
            payload={"chat_id": chat_id, "text": text},
        )
        return self._message_id(result)

    async def send_photo(
        self, *, token: str, chat_id: str, photo_url: str, caption: str | None = None
    ) -> str:
        """Send a photo by URL (+ optional caption); returns the message_id.

        ``photo_url`` is a public (signed, short-TTL) URL Telegram fetches
        server-side — same contract as WhatsApp's image link.
        """
        payload: dict[str, Any] = {"chat_id": chat_id, "photo": photo_url}
        if caption:
            payload["caption"] = caption
        result = await self._request(token=token, method="sendPhoto", payload=payload)
        return self._message_id(result)

    async def get_me(self, *, token: str) -> dict[str, Any]:
        """Call getMe to validate a token; returns the ``User`` (bot) object.

        Used at provisioning (M2) to confirm the pasted token is live and to
        capture the bot's id + username. Raises ``TelegramError`` on a bad
        token (Telegram returns 401).
        """
        result = await self._request(token=token, method="getMe", payload={})
        if not isinstance(result, dict) or result.get("id") is None:
            raise TelegramError("getMe returned no bot id", status_code=0)
        return result

    async def set_webhook(self, *, token: str, url: str, secret_token: str) -> bool:
        """Register the webhook URL + secret_token with Telegram.

        ``secret_token`` is echoed in the ``X-Telegram-Bot-Api-Secret-Token``
        header on every delivery (charset [A-Za-z0-9_-], 1-256). Returns True on
        success; raises ``TelegramError`` otherwise.
        """
        result = await self._request(
            token=token,
            method="setWebhook",
            payload={"url": url, "secret_token": secret_token},
        )
        return result is True

    @staticmethod
    def _message_id(result: Any) -> str:
        """Extract ``message_id`` from a send result, stringified."""
        if not isinstance(result, dict) or result.get("message_id") is None:
            raise TelegramError("Telegram send response had no message_id", status_code=0)
        return str(result["message_id"])


@lru_cache(maxsize=1)
def get_telegram_client() -> TelegramClient:
    """Process-wide singleton. ``TELEGRAM_API_BASE_URL`` env overrides the base
    (used by e2e to point at a Bot API mock); defaults to api.telegram.org."""
    return TelegramClient(base_url=os.environ.get("TELEGRAM_API_BASE_URL", _DEFAULT_BASE_URL))


def reset_client_for_testing() -> None:
    """Clear the cached singleton so a test can re-pin the base URL."""
    get_telegram_client.cache_clear()
