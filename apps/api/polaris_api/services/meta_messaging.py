"""Meta Graph messaging client for Instagram DM + Facebook Messenger — Phase 15 PR-5.

Conversational outbound for IG/Messenger, which Phase 6 left unbuilt (only
broadcasts/templates shipped to those channels). Pure HTTP — credentials (the
per-tenant Facebook Page access token) + the recipient id flow in from the
dispatch boundary; this module is wire-level only, mirroring
``services/meta_whatsapp.py``.

Both Instagram DM and Messenger send via the SAME Page-token endpoint
``POST graph.facebook.com/{version}/me/messages?access_token=<page_token>``
("Messenger Platform for Instagram" — a Page-linked IG account is reachable on
the Page's messaging edge). ``recipient.id`` is the PSID (Messenger) or IGSID
(Instagram); both are just the conversation's ``channel_user_id``.
``messaging_type=RESPONSE`` — we only send inside the 24h window (the enquiry
reply case); the caller owns that gate.

Unlike WhatsApp, IG/Messenger have NO native caption — a product reply is two
messages: the text answer, then the image attachment(s). The dispatch layer
sequences them; this client exposes one send per message.

Auth: Page access token lives in ``tenant_secrets`` under kind
``meta_fb_page_access_token`` (same kind ``messenger_profile.py`` uses). The
token is passed as the ``access_token`` query param.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any, Final, Literal

import httpx

from polaris_api.config import get_settings

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT_SECONDS: Final[float] = 10.0

# Platform tag — informational (logging + future endpoint divergence). Both
# currently POST the same /me/messages edge with the Page token.
Platform = Literal["messenger"]


class MetaMessagingError(RuntimeError):
    """Wraps a non-2xx response from the Graph messaging endpoint.

    Carries ``status_code`` so the dispatch layer can decide retry (5xx) vs
    hard-fail (4xx — typically a revoked Page token or a recipient outside the
    24h window).
    """

    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


class MetaMessagingClient:
    """Async HTTP client for IG DM + Messenger conversational sends.

    Constructed via the ``get_meta_messaging_client()`` lru_cache'd singleton.
    Pure wire-level: the per-tenant Page token + recipient id are passed in.
    """

    def __init__(self, *, base_url: str, graph_api_version: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.graph_api_version = graph_api_version

    def _url(self) -> str:
        return f"{self.base_url}/{self.graph_api_version}/me/messages"

    @staticmethod
    def _extract_mid(data: dict[str, Any], *, status_code: int) -> str:
        """Pull ``message_id`` out of the send response.

        Graph returns ``{recipient_id, message_id}`` on 2xx for both IG and
        Messenger sends.
        """
        mid = data.get("message_id")
        if not mid:
            raise MetaMessagingError(
                "Graph messaging send returned no message_id in response body",
                status_code=status_code,
            )
        return str(mid)

    async def _send(
        self,
        *,
        page_access_token: str,
        recipient_id: str,
        message: dict[str, Any],
        platform: Platform,
        human_agent: bool = False,
    ) -> str:
        """POST one message. ``message`` is the inner object — ``{"text": ...}``
        or ``{"attachment": ...}``; ``recipient`` + ``messaging_type`` are added
        at the top level here.

        ``human_agent`` switches the send into the **HUMAN_AGENT message tag**:
        ``messaging_type=MESSAGE_TAG`` + ``tag=HUMAN_AGENT``. This is set ONLY
        for human-agent-originated replies (the Chatwoot agent-forward path),
        never for bot replies. It extends the messaging window to 7 days so a
        human agent can reply outside the standard 24h window (requires the
        app-level Human Agent feature, which is approved). The default
        ``RESPONSE`` is the in-window bot-reply case."""
        if not page_access_token or page_access_token.startswith("placeholder-"):
            raise RuntimeError(
                "Meta messaging requires a per-tenant Page access_token; "
                "decrypt meta_fb_page_access_token from tenant_secrets first."
            )
        payload: dict[str, Any] = {
            "recipient": {"id": recipient_id},
            "messaging_type": "MESSAGE_TAG" if human_agent else "RESPONSE",
            "message": message,
        }
        if human_agent:
            payload["tag"] = "HUMAN_AGENT"
        # Page token is passed as a query param (same as messenger_profile.py).
        params = {"access_token": page_access_token}

        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as client:
            response = await client.post(self._url(), params=params, json=payload)

        if response.status_code >= 400:
            # Log Meta's STRUCTURED error fields (code / subcode / type /
            # fbtrace_id / message) — enough to diagnose (e.g. code=190 = an
            # expired or revoked Page token) without logging the full body, which
            # can echo request content / token fragments via ``error_data``. The
            # ``message`` field is Meta's human-readable reason ("Error validating
            # access token: Session has expired …") and does not carry the token;
            # it's bounded to 300 chars as a belt-and-braces guard.
            err: dict[str, Any] = {}
            try:
                err = (response.json() or {}).get("error", {}) or {}
            except (ValueError, TypeError):
                err = {}
            logger.warning(
                "meta_messaging send non-2xx: status=%d platform=%s code=%s "
                "subcode=%s type=%s fbtrace=%s message=%s",
                response.status_code,
                platform,
                err.get("code"),
                err.get("error_subcode"),
                err.get("type"),
                err.get("fbtrace_id"),
                str(err.get("message"))[:300],
            )
            raise MetaMessagingError(
                f"Meta {platform} send failed: HTTP {response.status_code} "
                f"(code={err.get('code')} subcode={err.get('error_subcode')})",
                status_code=response.status_code,
            )

        return self._extract_mid(
            response.json(), status_code=response.status_code
        )

    async def send_text(
        self,
        *,
        page_access_token: str,
        recipient_id: str,
        text: str,
        platform: Platform,
        human_agent: bool = False,
    ) -> str:
        """Send a text message to an IG/Messenger user; returns the mid.

        ``human_agent=True`` tags the send with the HUMAN_AGENT message tag
        (human-agent-originated replies only — see ``_send``)."""
        return await self._send(
            page_access_token=page_access_token,
            recipient_id=recipient_id,
            message={"text": text},
            platform=platform,
            human_agent=human_agent,
        )

    async def send_image(
        self,
        *,
        page_access_token: str,
        recipient_id: str,
        image_url: str,
        platform: Platform,
        human_agent: bool = False,
    ) -> str:
        """Send an image attachment (hosted URL) to an IG/Messenger user.

        ``image_url`` is a public (signed, short-TTL) URL Meta fetches
        server-side. ``human_agent=True`` tags the send with the HUMAN_AGENT
        message tag (human-agent-originated replies only). Returns the mid.
        """
        return await self._send(
            page_access_token=page_access_token,
            recipient_id=recipient_id,
            message={
                "attachment": {"type": "image", "payload": {"url": image_url}},
            },
            platform=platform,
            human_agent=human_agent,
        )


@lru_cache(maxsize=1)
def get_meta_messaging_client() -> MetaMessagingClient:
    """Lazy singleton — constructed from env on first call. Shares the Graph
    base URL + version with the WhatsApp client."""
    settings = get_settings()
    return MetaMessagingClient(
        base_url=settings.meta_api_base_url,
        graph_api_version=settings.meta_graph_api_version,
    )


class InstagramLoginClient:
    """Async HTTP client for Instagram-API-with-Instagram-Login DM sends.

    The "Continue with Instagram" connection (graph.instagram.com) differs from
    ``MetaMessagingClient`` (the FB-page Messenger edge) on three points:

      1. **Base URL** — ``graph.instagram.com`` instead of ``graph.facebook.com``.
      2. **Token** — the Instagram-Login *user* access token
         (``Channel::Instagram.access_token`` in Chatwoot), stored in
         tenant_secrets under kind ``meta_ig_login_access_token`` — NOT a Page
         token. Passed as the ``access_token`` query param (same convention as
         the Messenger edge; NOT an ``Authorization: Bearer`` header).
      3. **No ``messaging_type``** — the IG-Login messaging API rejects the
         Messenger-only ``messaging_type`` field; the body is just
         ``{"recipient": {"id": IGSID}, "message": {...}}``. The 24h reply
         window still applies; the caller owns that gate.

    Constructed via the ``get_instagram_login_client()`` lru_cache'd singleton.
    Pure wire-level: the per-tenant IG-Login token + recipient id are passed in.
    """

    def __init__(self, *, base_url: str, graph_api_version: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.graph_api_version = graph_api_version

    def _url(self) -> str:
        return f"{self.base_url}/{self.graph_api_version}/me/messages"

    async def _send(
        self,
        *,
        access_token: str,
        recipient_id: str,
        message: dict[str, Any],
        human_agent: bool = False,
    ) -> str:
        """POST one message via graph.instagram.com. Returns the mid.

        Reuses ``MetaMessagingError`` so the dispatch layer's existing
        retry-vs-hard-fail handling (5xx vs 4xx) covers IG-Login sends too.

        ``human_agent`` adds the **HUMAN_AGENT message tag**. Unlike the
        Messenger edge, the IG-Login messaging API takes the tag as a bare
        top-level ``tag`` field with NO ``messaging_type`` (which it rejects);
        this extends the window to 7 days for human-agent-originated replies.
        Set ONLY on the Chatwoot agent-forward path, never for bot replies.
        """
        if not access_token or access_token.startswith("placeholder-"):
            raise RuntimeError(
                "Instagram-Login messaging requires a per-tenant IG-Login user "
                "access_token; decrypt meta_ig_login_access_token from "
                "tenant_secrets first."
            )
        payload: dict[str, Any] = {
            "recipient": {"id": recipient_id},
            "message": message,
        }
        if human_agent:
            payload["tag"] = "HUMAN_AGENT"
        params = {"access_token": access_token}

        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as client:
            response = await client.post(self._url(), params=params, json=payload)

        if response.status_code >= 400:
            # Don't log the body — Meta error envelopes can echo token fragments.
            logger.warning(
                "instagram_login send non-2xx: status=%d", response.status_code
            )
            raise MetaMessagingError(
                f"Instagram-Login send failed: HTTP {response.status_code}",
                status_code=response.status_code,
            )

        data = response.json()
        mid = data.get("message_id")
        if not mid:
            raise MetaMessagingError(
                "Instagram-Login send returned no message_id in response body",
                status_code=response.status_code,
            )
        return str(mid)

    async def send_text(
        self,
        *,
        access_token: str,
        recipient_id: str,
        text: str,
        human_agent: bool = False,
    ) -> str:
        """Send a text DM to an Instagram-Login user; returns the mid.

        ``human_agent=True`` adds the HUMAN_AGENT message tag (human-agent-
        originated replies only — see ``_send``)."""
        return await self._send(
            access_token=access_token,
            recipient_id=recipient_id,
            message={"text": text},
            human_agent=human_agent,
        )

    async def send_image(
        self,
        *,
        access_token: str,
        recipient_id: str,
        image_url: str,
        human_agent: bool = False,
    ) -> str:
        """Send an image attachment (hosted URL) to an Instagram-Login user.

        ``image_url`` is a public (signed, short-TTL) URL Meta fetches
        server-side. ``human_agent=True`` adds the HUMAN_AGENT message tag
        (human-agent-originated replies only). Returns the mid.
        """
        return await self._send(
            access_token=access_token,
            recipient_id=recipient_id,
            message={
                "attachment": {"type": "image", "payload": {"url": image_url}},
            },
            human_agent=human_agent,
        )


@lru_cache(maxsize=1)
def get_instagram_login_client() -> InstagramLoginClient:
    """Lazy singleton for IG-Login sends — constructed from env on first call.

    Uses ``meta_instagram_api_base_url`` (graph.instagram.com) but shares the
    Graph API version pin with the other Meta clients.
    """
    settings = get_settings()
    return InstagramLoginClient(
        base_url=settings.meta_instagram_api_base_url,
        graph_api_version=settings.meta_graph_api_version,
    )


def reset_client_for_testing() -> None:
    """Clear the lru_cache so tests can monkeypatch a fresh factory."""
    for factory in (get_meta_messaging_client, get_instagram_login_client):
        clear = getattr(factory, "cache_clear", None)
        if clear is not None:
            clear()
