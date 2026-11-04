"""Meta WhatsApp Graph API client — Phase 6 PR-2.

Adapter over the WhatsApp Cloud API v21.0. Pure HTTP — does NOT read
the database. Per-tenant access tokens + phone_number_ids + waba_ids
flow in from the dispatch boundary (broadcast worker / reply_dispatch
/ templates service); this module's job is exclusively wire-level.

Phase 6 added the template lifecycle + endpoint-switching surface needed by
the broadcast pipeline. ADR-066 removed the legacy demo-WABA fallback: every
send now requires an explicit per-channel ``access_token``.

  - ``send_text``       — requires an explicit ``access_token`` (decrypted at
                          the caller from ``tenant_secrets``); a None/placeholder
                          token raises rather than guessing a sender.
  - ``submit_template`` — POST /{waba_id}/message_templates. Returns the
                          assigned ``meta_template_id`` for persistence
                          into ``tenant_templates.meta_template_id``.
  - ``delete_template`` — DELETE /{waba_id}/message_templates?name=...,
                          idempotent on 404 (template-never-existed is
                          indistinguishable from already-deleted, and the
                          caller's intent is "make sure it's gone").
  - ``send_template``   — POST /{phone_number_id}/{messages|marketing_messages}.
                          Endpoint switch is driven by a boolean flag set
                          by the caller (broadcast/service computes it
                          from tenant_channels.mm_lite_status + template
                          category per 00a §6.10 + ADR-026 Amendment 3rd).
                          NO DB lookup here.

MetaTemplateError extends MetaWhatsAppError with the structured error
fields Meta returns on the templates surface (``error.code``,
``error.error_subcode``, ``error.error_data.details``). The lifecycle
handler surfaces ``details`` verbatim to the tenant ("body text
references {{1}} but no example provided") — Meta's error string is
already user-friendly.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Callable, Coroutine
from functools import lru_cache
from typing import Any, Final
from urllib.parse import quote

import httpx

from polaris_api import http_client
from polaris_api.config import get_settings

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT_SECONDS: Final[float] = 10.0


@contextlib.asynccontextmanager
async def _get_client() -> AsyncIterator[httpx.AsyncClient]:
    """Shared-or-fresh httpx client (audit Batch B / B6).

    Prefers the lifespan-scoped singleton (prod). Falls back to a fresh
    per-call ``httpx.AsyncClient`` — the previous behaviour — when the
    singleton isn't initialised (unit tests / scripts). Every call site
    still passes its own explicit ``timeout=`` on the request, so behavior
    is identical either way. Never closes the shared client (only the
    lifespan does); the fresh fallback closes itself via ``async with``.
    """
    shared = http_client.get_http_client_or_none()
    if shared is not None:
        yield shared
    else:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as client:
            yield client


# ---------------------------------------------------------------------------
# Optional usage-telemetry sink. This module stays pure-HTTP (no DB); the app
# may inject a sink at startup (main.lifespan → services.meta_usage.record_usage)
# that persists the X-Business-Use-Case-Usage header for the System Health Meta
# tile. Default None = no-op, so unit tests and library use stay DB-free.
# ---------------------------------------------------------------------------
_UsageSink = Callable[[str], Coroutine[Any, Any, None]]
_usage_sink: _UsageSink | None = None


def set_usage_sink(sink: _UsageSink | None) -> None:
    """Register (or clear) the best-effort Meta-usage telemetry sink."""
    global _usage_sink  # noqa: PLW0603
    _usage_sink = sink


def _emit_usage(response: httpx.Response) -> None:
    """Fire-and-forget the rate-limit header to the sink. Never raises."""
    sink = _usage_sink
    if sink is None:
        return
    raw = response.headers.get("x-business-use-case-usage")
    if not raw:
        return
    try:
        asyncio.get_running_loop().create_task(sink(raw))
    except RuntimeError:
        # No running loop (sync context) — drop the sample; telemetry only.
        pass


class MetaWhatsAppError(RuntimeError):
    """Wraps non-2xx responses from the Graph API.

    Carries ``status_code`` for the dispatch layer to decide whether to
    retry (5xx) or surface as a hard failure (4xx — likely a token /
    permissions bug, not a transient blip).
    """

    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


class MetaTemplateError(MetaWhatsAppError):
    """Error wrapping for the template-lifecycle endpoints.

    Carries Meta's structured error envelope so the portal can surface
    tenant-actionable failure reasons. Per the deep-dive §5.1: "Meta's
    error string is already user-friendly".

    ``details`` is the contents of ``error.error_data.details`` — a
    free-text field Meta uses to explain template rejections inline
    (e.g. "Variable parameter {{1}} not present in your template
    components"). Stored verbatim into ``tenant_templates.rejection_reason``.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        code: int | None,
        error_subcode: int | None,
        details: str | None,
    ) -> None:
        super().__init__(message, status_code=status_code)
        self.code = code
        self.error_subcode = error_subcode
        self.details = details


def _parse_meta_error(
    response: httpx.Response, *, default_message: str
) -> tuple[int | None, int | None, str | None, str]:
    """Extract Meta's structured error envelope from a non-2xx response.

    Meta returns ``{error: {message, code, error_subcode,
    error_data: {messaging_product, details}}}`` for 4xx; on 5xx the
    envelope shape is less reliable and we may get HTML — fall back to
    a generic message in that case.

    Returns ``(code, error_subcode, details, message)`` — the four
    pieces MetaTemplateError needs.
    """
    code: int | None = None
    error_subcode: int | None = None
    details: str | None = None
    message: str = default_message
    try:
        body = response.json()
    except ValueError:
        return code, error_subcode, details, message
    err = body.get("error") if isinstance(body, dict) else None
    if not isinstance(err, dict):
        return code, error_subcode, details, message
    if isinstance(err.get("code"), int):
        code = err["code"]
    if isinstance(err.get("error_subcode"), int):
        error_subcode = err["error_subcode"]
    err_data = err.get("error_data")
    if isinstance(err_data, dict) and isinstance(err_data.get("details"), str):
        details = err_data["details"]
    if isinstance(err.get("message"), str):
        message = err["message"]
    return code, error_subcode, details, message


class MetaWhatsAppClient:
    """Async HTTP client for the WhatsApp Cloud API.

    Constructed lazily via ``get_meta_whatsapp_client()`` (lru_cache'd
    singleton). Tests can monkeypatch the factory after calling
    ``reset_client_for_testing``.
    """

    def __init__(
        self,
        *,
        base_url: str,
        graph_api_version: str,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.graph_api_version = graph_api_version

    # -----------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------

    def _resolve_token(self, *, access_token: str | None) -> str:
        """Validate the per-send WhatsApp access token.

        Callers MUST pass the decrypted per-channel token (resolved from
        ``tenant_secrets`` before dispatch — for a demo turn these are the
        gateway channel's creds, resolved by channel owner). There is no shared
        fallback: a missing/placeholder token is a misconfig we surface rather
        than guess a sender (ADR-066 removed the demo-WABA fallback).
        """
        if (
            access_token is None
            or not access_token
            or access_token.startswith("placeholder-")
        ):
            raise RuntimeError(
                "WhatsApp access_token is empty/placeholder/missing; refusing "
                "to send. Caller must decrypt the per-channel token from "
                "tenant_secrets before invoking."
            )
        return access_token

    def _url(self, suffix: str) -> str:
        return f"{self.base_url}/{self.graph_api_version}/{suffix.lstrip('/')}"

    @staticmethod
    def _extract_wamid(data: dict[str, Any], *, status_code: int) -> str:
        """Pull ``messages[0].id`` out of the send response.

        Same shape across /messages and /marketing_messages — both
        return ``{messages: [{id: 'wamid.XXX'}], ...}`` on 2xx.
        """
        messages_field = data.get("messages") or []
        if not messages_field:
            raise MetaWhatsAppError(
                "WhatsApp send returned no messages[] in response body",
                status_code=status_code,
            )
        wamid = messages_field[0].get("id")
        if not wamid:
            raise MetaWhatsAppError(
                "WhatsApp send response had messages[0] but no .id",
                status_code=status_code,
            )
        return str(wamid)

    # -----------------------------------------------------------------
    # Send surfaces
    # -----------------------------------------------------------------

    async def send_text(
        self,
        *,
        phone_number_id: str,
        to: str,
        body: str,
        access_token: str | None = None,
    ) -> str:
        """Send a freeform text message; returns the wamid.

        ``access_token`` MUST be the explicit per-channel token decrypted from
        ``tenant_secrets`` by the caller. A None/placeholder token raises.

        Raises:
          - RuntimeError when the token is missing/placeholder (see
            ``_resolve_token``).
          - MetaWhatsAppError on non-2xx Graph API response.
        """
        token = self._resolve_token(access_token=access_token)
        url = self._url(f"{phone_number_id}/messages")
        payload: dict[str, Any] = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "text",
            "text": {"body": body},
        }
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

        async with _get_client() as client:
            response = await client.post(
                url, json=payload, headers=headers, timeout=_HTTP_TIMEOUT_SECONDS,
            )

        _emit_usage(response)  # best-effort rate-limit telemetry (incl. on 4xx)

        if response.status_code >= 400:
            # Don't log the body — Meta error responses sometimes echo
            # request payload back, which could include user content
            # we'd rather not surface in ops logs.
            logger.warning(
                "meta_whatsapp send_text non-2xx: status=%d phone_number_id=%s",
                response.status_code,
                phone_number_id,
            )
            raise MetaWhatsAppError(
                f"WhatsApp send failed: HTTP {response.status_code}",
                status_code=response.status_code,
            )

        return self._extract_wamid(response.json(), status_code=response.status_code)

    async def send_image(
        self,
        *,
        phone_number_id: str,
        to: str,
        image_link: str,
        caption: str | None = None,
        access_token: str | None = None,
    ) -> str:
        """Send an image message (hosted-URL link); returns the wamid.

        Phase 15 PR-5. WhatsApp natively supports a ``caption`` so the RAG
        answer rides on the image itself — one message, not two (unlike
        IG/Messenger which need text-then-image). Only sendable inside the
        24h customer-service window (the enquiry case); the caller is
        responsible for that gate.

        ``image_link`` is a public (signed, short-TTL) URL the Cloud API
        fetches server-side. ``access_token`` is required (per-channel token
        from ``tenant_secrets``; see ``_resolve_token``).
        """
        token = self._resolve_token(access_token=access_token)
        url = self._url(f"{phone_number_id}/messages")
        image_obj: dict[str, Any] = {"link": image_link}
        if caption:
            image_obj["caption"] = caption
        payload: dict[str, Any] = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "image",
            "image": image_obj,
        }
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

        async with _get_client() as client:
            response = await client.post(
                url, json=payload, headers=headers, timeout=_HTTP_TIMEOUT_SECONDS,
            )

        _emit_usage(response)

        if response.status_code >= 400:
            logger.warning(
                "meta_whatsapp send_image non-2xx: status=%d phone_number_id=%s",
                response.status_code,
                phone_number_id,
            )
            raise MetaWhatsAppError(
                f"WhatsApp image send failed: HTTP {response.status_code}",
                status_code=response.status_code,
            )

        return self._extract_wamid(response.json(), status_code=response.status_code)

    async def send_template(
        self,
        *,
        phone_number_id: str,
        to: str,
        template_name: str,
        language: str,
        variables: dict[str, str],
        access_token: str,
        use_marketing_endpoint: bool,
    ) -> str:
        """Send an approved template; returns the wamid.

        ``use_marketing_endpoint`` is set by the caller (typically
        ``services/broadcast/service._resolve_send_path``) based on
        ``(tenant_channels.mm_lite_status == 'ONBOARDED') AND
        (tenant_templates.category == 'MARKETING')``. This module does
        NOT read the database — the flag is the entire decision surface
        for the endpoint switch. Keeps the adapter pure (same posture
        as the hex-runtime ports in Phase 5).

        ``variables`` is a flat ``{position: value}`` map; we render it
        into Meta's positional ``components[0].parameters[]`` shape on
        send. Position is the dict key as a string ("1", "2", ...);
        callers that pre-resolve named variables to positions get this
        mapping from the tenant_templates.components blob.
        """
        token = self._resolve_token(access_token=access_token)
        endpoint = "marketing_messages" if use_marketing_endpoint else "messages"
        url = self._url(f"{phone_number_id}/{endpoint}")

        # Build positional parameters in numeric-key order so a missing
        # position fails Meta-side validation cleanly (rather than us
        # silently dropping the variable). dict comprehension preserves
        # insertion order; we sort explicitly for determinism.
        parameters: list[dict[str, str]] = [
            {"type": "text", "text": variables[key]}
            for key in sorted(variables.keys(), key=lambda k: int(k))
        ]

        payload: dict[str, Any] = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "template",
            "template": {
                "name": template_name,
                "language": {"code": language},
            },
        }
        if parameters:
            payload["template"]["components"] = [
                {"type": "body", "parameters": parameters}
            ]

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

        async with _get_client() as client:
            response = await client.post(
                url, json=payload, headers=headers, timeout=_HTTP_TIMEOUT_SECONDS,
            )

        _emit_usage(response)  # best-effort rate-limit telemetry (incl. on 4xx)

        if response.status_code >= 400:
            logger.warning(
                "meta_whatsapp send_template non-2xx: status=%d "
                "phone_number_id=%s template=%s marketing=%s",
                response.status_code,
                phone_number_id,
                template_name,
                use_marketing_endpoint,
            )
            raise MetaWhatsAppError(
                f"WhatsApp template send failed: HTTP {response.status_code}",
                status_code=response.status_code,
            )

        return self._extract_wamid(response.json(), status_code=response.status_code)

    # -----------------------------------------------------------------
    # Template lifecycle
    # -----------------------------------------------------------------

    async def submit_template(
        self,
        *,
        waba_id: str,
        name: str,
        language: str,
        category: str,
        components: list[dict[str, Any]],
        access_token: str,
    ) -> str:
        """Submit a template for Meta review; returns meta_template_id.

        Meta accepts the POST synchronously and assigns an id even
        before approval completes. ``status`` in the response is
        ``PENDING``; approval transitions arrive via the
        ``meta-template-status`` webhook handler (PR-3) which UPDATEs
        ``tenant_templates.status`` + ``meta_template_id`` (if the id
        wasn't captured here).

        Raises MetaTemplateError on 4xx with the structured envelope;
        plain MetaWhatsAppError on 5xx (no useful per-error detail in
        Meta's 5xx responses).
        """
        if not access_token or access_token.startswith("placeholder-"):
            raise RuntimeError(
                "submit_template requires a per-tenant access_token; "
                "decrypt from tenant_secrets before calling."
            )
        url = self._url(f"{waba_id}/message_templates")
        payload: dict[str, Any] = {
            "name": name,
            "language": language,
            "category": category,
            "components": components,
        }
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }

        async with _get_client() as client:
            response = await client.post(
                url, json=payload, headers=headers, timeout=_HTTP_TIMEOUT_SECONDS,
            )

        if response.status_code >= 400:
            code, subcode, details, message = _parse_meta_error(
                response,
                default_message=(
                    f"Template submit failed: HTTP {response.status_code}"
                ),
            )
            logger.warning(
                "meta_whatsapp submit_template non-2xx: status=%d waba_id=%s "
                "name=%s code=%s subcode=%s",
                response.status_code,
                waba_id,
                name,
                code,
                subcode,
            )
            raise MetaTemplateError(
                message,
                status_code=response.status_code,
                code=code,
                error_subcode=subcode,
                details=details,
            )

        data = response.json()
        template_id = data.get("id")
        if not template_id:
            raise MetaTemplateError(
                "Template submit returned no .id in response body",
                status_code=response.status_code,
                code=None,
                error_subcode=None,
                details=None,
            )
        return str(template_id)

    async def create_from_library(
        self,
        *,
        waba_id: str,
        name: str,
        language: str,
        category: str,
        library_template_name: str,
        access_token: str,
        button_inputs: list[dict[str, Any]] | None = None,
    ) -> tuple[str, str]:
        """Create a template from Meta's Template Library — instant-approved.

        Unlike ``submit_template`` (custom authoring → PENDING → review),
        library templates carry Meta-owned fixed content: we pass
        ``library_template_name`` instead of ``components`` and Meta returns
        ``status=APPROVED`` synchronously (no status webhook). Only UTILITY +
        AUTHENTICATION templates exist in the library.

        Returns ``(meta_template_id, status)`` — ``status`` is normally
        ``"APPROVED"``; the caller persists it directly rather than waiting on
        the lifecycle webhook.

        NOTE: payload shape per Meta's Template Library docs
        (``library_template_name`` + optional
        ``library_template_button_inputs``). Unit tests assert request
        construction only — a live WABA smoke is required before GA.

        Raises ``MetaTemplateError`` on 4xx, ``MetaWhatsAppError`` on 5xx.
        """
        if not access_token or access_token.startswith("placeholder-"):
            raise RuntimeError(
                "create_from_library requires a per-tenant access_token; "
                "decrypt from tenant_secrets before calling."
            )
        url = self._url(f"{waba_id}/message_templates")
        payload: dict[str, Any] = {
            "name": name,
            "language": language,
            "category": category,
            "library_template_name": library_template_name,
        }
        if button_inputs:
            payload["library_template_button_inputs"] = button_inputs
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }

        async with _get_client() as client:
            response = await client.post(
                url, json=payload, headers=headers, timeout=_HTTP_TIMEOUT_SECONDS,
            )

        if response.status_code >= 400:
            code, subcode, details, message = _parse_meta_error(
                response,
                default_message=(
                    f"Library template create failed: HTTP {response.status_code}"
                ),
            )
            logger.warning(
                "meta_whatsapp create_from_library non-2xx: status=%d waba_id=%s "
                "name=%s library=%s code=%s subcode=%s",
                response.status_code,
                waba_id,
                name,
                library_template_name,
                code,
                subcode,
            )
            raise MetaTemplateError(
                message,
                status_code=response.status_code,
                code=code,
                error_subcode=subcode,
                details=details,
            )

        data = response.json()
        template_id = data.get("id")
        if not template_id:
            raise MetaTemplateError(
                "Library template create returned no .id in response body",
                status_code=response.status_code,
                code=None,
                error_subcode=None,
                details=None,
            )
        status = str(data.get("status") or "APPROVED")
        return str(template_id), status

    async def delete_template(
        self, *, waba_id: str, name: str, access_token: str
    ) -> None:
        """Delete a template by name; idempotent on 404.

        Meta returns 404 when the template name has never existed under
        the given waba_id — indistinguishable from "already deleted",
        and the caller's intent ("make sure it's gone") is satisfied
        either way. Treat as success.

        Other non-2xx responses raise MetaTemplateError.
        """
        if not access_token or access_token.startswith("placeholder-"):
            raise RuntimeError(
                "delete_template requires a per-tenant access_token; "
                "decrypt from tenant_secrets before calling."
            )
        # quote() guards against tenants picking a name that round-trips
        # through URL parsing oddly. Our own CHECK constraint on
        # tenant_templates.name is stricter than Meta's so collisions
        # would be caught earlier, but the URL-encoding is cheap and
        # defence-in-depth.
        url = self._url(
            f"{waba_id}/message_templates?name={quote(name, safe='')}"
        )
        headers = {"Authorization": f"Bearer {access_token}"}

        async with _get_client() as client:
            response = await client.delete(
                url, headers=headers, timeout=_HTTP_TIMEOUT_SECONDS,
            )

        if response.status_code == 404:
            logger.info(
                "meta_whatsapp delete_template 404 (treating as already-deleted): "
                "waba_id=%s name=%s",
                waba_id,
                name,
            )
            return
        if response.status_code >= 400:
            code, subcode, details, message = _parse_meta_error(
                response,
                default_message=(
                    f"Template delete failed: HTTP {response.status_code}"
                ),
            )
            logger.warning(
                "meta_whatsapp delete_template non-2xx: status=%d waba_id=%s "
                "name=%s code=%s",
                response.status_code,
                waba_id,
                name,
                code,
            )
            raise MetaTemplateError(
                message,
                status_code=response.status_code,
                code=code,
                error_subcode=subcode,
                details=details,
            )


@lru_cache(maxsize=1)
def get_meta_whatsapp_client() -> MetaWhatsAppClient:
    """Lazy singleton — constructed from env on first call.

    base_url defaults to the public Graph API. graph_api_version
    pinned to META_GRAPH_API_VERSION (default v21.0 per
    .env.example). Tests monkeypatch via the factory after
    ``reset_client_for_testing``.
    """
    settings = get_settings()
    return MetaWhatsAppClient(
        base_url=settings.meta_api_base_url,
        graph_api_version=settings.meta_graph_api_version,
    )


def reset_client_for_testing() -> None:
    """Clear the lru_cache so tests can monkeypatch a fresh factory."""
    clear = getattr(get_meta_whatsapp_client, "cache_clear", None)
    if clear is not None:
        clear()
