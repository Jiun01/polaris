"""Per-channel authenticated inbound audio download — voice-notes M1.

Pure HTTP — does NOT read the database (mirrors ``meta_whatsapp.py`` /
``telegram_client.py``). Decrypted provider tokens flow in from the
pipeline boundary, which resolves them from ``tenant_secrets`` under the
RLS-scoped connection. This module's job is exclusively wire-level: turn
an inbound audio ``Attachment`` into raw ``bytes`` + a mime type that
``services/transcription.py`` can POST to Groq.

Download auth differs per channel (the load-bearing complexity):

  | Channel              | Handle                | Download                          |
  |----------------------|-----------------------|-----------------------------------|
  | WhatsApp             | ``media_id``          | Graph 2-step: GET /{media_id} for |
  |                      |                       | a signed url, then GET that url — |
  |                      |                       | BOTH with the WABA Bearer token.  |
  | Telegram             | ``media_id``=file_id  | getFile → file_path, then GET     |
  |                      |                       | /file/bot<token>/<file_path>.     |
  | Instagram/Messenger  | ``url``               | direct GET (durable Meta CDN url).|
  | Chatwoot             | ``url`` (data_url)    | direct GET (CDN, no token).       |

**Chatwoot-native rehaul (``via_chatwoot=True``):** post-rehaul ALL inbound —
WhatsApp, Telegram, Instagram, Messenger — arrives through the Chatwoot agent-bot
webhook, so every attachment is a Chatwoot ``data_url`` (NOT a Meta/Telegram
handle). When ``via_chatwoot`` is set the per-channel routing above is bypassed
and the ``data_url`` is fetched directly (token-free), regardless of the channel
kind — otherwise a WhatsApp/Telegram voice note would wrongly hit the Meta/Telegram
media-id path using the Chatwoot attachment id and fail. The url's host is first
normalised (``_normalize_chatwoot_url``): a loopback / non-routable host (e.g. a
mis-set Chatwoot ``FRONTEND_URL=http://0.0.0.0:3000``) is rewritten to the
reachable ``chatwoot_internal_base_url``.

Tokens (WABA / Telegram bot) are never logged and never embedded in a
raised ``MediaDownloadError`` message — Telegram puts the token in the URL
path, so a leaked URL would leak the credential.
"""

from __future__ import annotations

import contextlib
import logging
import os
from collections.abc import AsyncIterator
from typing import Final
from urllib.parse import urlsplit, urlunsplit

import httpx

from polaris_api import http_client
from polaris_api.config import get_settings

logger = logging.getLogger(__name__)

_DOWNLOAD_TIMEOUT_SECONDS: Final[float] = 15.0


@contextlib.asynccontextmanager
async def _get_client(
    *, fallback_timeout: float, follow_redirects: bool = False
) -> AsyncIterator[httpx.AsyncClient]:
    """Shared-or-fresh httpx client (audit Batch B / B6).

    Prefers the lifespan-scoped singleton (prod) — every request through it
    still passes its own explicit ``timeout=`` (see ``_get``), so behavior
    is identical to a fresh per-call client. Falls back to a fresh
    ``httpx.AsyncClient`` — the previous behaviour, verbatim — when the
    singleton isn't initialised (unit tests / scripts).

    IMPORTANT — test compatibility: the fallback branch references
    ``httpx.AsyncClient`` as a MODULE ATTRIBUTE (not an imported symbol),
    so ``tests/test_media_download.py``'s ``monkeypatch.setattr(md_mod,
    "httpx", _Httpx)`` still intercepts it. Do NOT change this to
    ``from httpx import AsyncClient`` — that would break the patch.

    ``follow_redirects`` is a CONSTRUCTION-time httpx.AsyncClient param
    (unlike ``timeout``, it cannot be overridden per-request on
    ``client.get()``), so the shared client — built once at lifespan
    startup with ``follow_redirects`` unset (default False) — cannot honor
    a per-call ``follow_redirects=True`` request. ``_fetch_direct`` (the
    only caller needing redirects) therefore always uses a fresh client;
    see the call site below.
    """
    if not follow_redirects:
        shared = http_client.get_http_client_or_none()
        if shared is not None:
            yield shared
            return
    async with httpx.AsyncClient(
        timeout=fallback_timeout, follow_redirects=follow_redirects
    ) as client:
        yield client


_TELEGRAM_DEFAULT_BASE: Final[str] = "https://api.telegram.org"
_DIRECT_URL_PROVIDERS: Final[frozenset[str]] = frozenset(
    # instagram_login (graph.instagram.com) ships inbound attachments as direct
    # URLs in the messaging[] payload, same as the FB-page 'instagram' channel.
    {"instagram", "messenger", "chatwoot", "instagram_login"}
)
_FALLBACK_MIME: Final[str] = "audio/ogg"


class MediaDownloadError(RuntimeError):
    """Raised when an inbound audio attachment can't be fetched.

    Carries ``status_code`` (HTTP status, or 0 for network/precondition
    failures) so the pipeline can log triage context. The pipeline treats
    any ``MediaDownloadError`` as "fall back to the polite please-type
    reply" — it never propagates to the customer.
    """

    def __init__(self, message: str, *, status_code: int = 0) -> None:
        super().__init__(message)
        self.status_code = status_code


async def fetch_audio_bytes(
    *,
    provider: str,
    attachment: object,
    access_token: str | None = None,
    via_chatwoot: bool = False,
) -> tuple[bytes, str]:
    """Download one inbound audio attachment → ``(audio_bytes, mime_type)``.

    ``attachment`` is a ``polaris_shared_types.Attachment`` (duck-typed here
    to keep the module import-light). ``access_token`` is the decrypted WABA
    Bearer token (WhatsApp) or bot token (Telegram); ``None`` for the
    direct-url channels. Raises ``MediaDownloadError`` on any failure.

    ``via_chatwoot`` — the inbound arrived through the Chatwoot agent-bot webhook
    (the post-rehaul norm for every channel). The attachment then carries a
    Chatwoot ``data_url`` and a Chatwoot attachment id (NOT a Meta/Telegram
    handle), so the per-channel handle routing below is bypassed and the
    (host-normalised) ``data_url`` is fetched directly. Without this, WhatsApp /
    Telegram voice notes would hit the Meta/Telegram media-id path using the
    Chatwoot attachment id and fail.
    """
    media_id = getattr(attachment, "media_id", None)
    url = getattr(attachment, "url", None)
    declared_mime = getattr(attachment, "mime_type", None)

    # Chatwoot-native: every channel's attachment is a Chatwoot data_url. Fetch it
    # directly (token-free), bypassing the Meta/Telegram media-id paths.
    if via_chatwoot:
        return await _fetch_direct(_normalize_chatwoot_url(url), declared_mime)

    if provider == "whatsapp":
        return await _fetch_whatsapp(media_id, access_token, declared_mime)
    if provider == "telegram":
        return await _fetch_telegram(media_id, access_token, declared_mime)
    if provider in _DIRECT_URL_PROVIDERS:
        return await _fetch_direct(url, declared_mime)
    raise MediaDownloadError(f"unsupported provider for audio download: {provider}")


# Loopback / non-routable hosts that a mis-set Chatwoot FRONTEND_URL can bake into
# an attachment data_url (e.g. ``http://0.0.0.0:3000/...``). Unreachable from the
# polaris-api container, so rewrite to the internal Chatwoot base.
_NON_ROUTABLE_HOSTS: Final[frozenset[str]] = frozenset(
    # noqa below: these are host strings to MATCH against an inbound url, not a
    # bind address — S104 (bind-all-interfaces) does not apply. Use bare "::1"
    # (no brackets): urlsplit().hostname strips the RFC-3986 brackets off IPv6
    # literals, so "[::1]" would never match.
    {"0.0.0.0", "127.0.0.1", "localhost", "::1"}  # noqa: S104
)


def _normalize_chatwoot_url(url: str | None) -> str | None:
    """Rewrite a Chatwoot data_url whose host is loopback/non-routable.

    Chatwoot builds attachment ``data_url``s from its (Sidekiq) ``FRONTEND_URL``;
    if that is misconfigured to ``http://0.0.0.0:3000`` the url is unreachable from
    polaris-api. Rewrite scheme+host+port to ``chatwoot_internal_base_url`` (a
    docker-network host that routes to the same Rails app), preserving path+query.
    A url with an already-routable host (the correct public/internal base) is
    returned unchanged. A falsy url (``None`` or ``""``) returns ``None``
    (``_fetch_direct`` raises on it).

    Security: the rewrite ONLY fires for a fixed allow-list of loopback hosts and
    ONLY ever targets the trusted, configured ``chatwoot_internal_base_url`` — it
    cannot be coerced to redirect a fetch at an arbitrary host.
    """
    if not url:
        return None
    parts = urlsplit(url)
    if parts.hostname is None or parts.hostname.lower() not in _NON_ROUTABLE_HOSTS:
        return url
    internal = urlsplit(get_settings().chatwoot_internal_base_url.rstrip("/"))
    return urlunsplit(
        (
            internal.scheme or parts.scheme,
            internal.netloc,  # scheme://host:port from the trusted internal base
            parts.path,
            parts.query,
            parts.fragment,
        )
    )


# ----------------------------------------------------------------------
# Per-channel fetchers
# ----------------------------------------------------------------------


async def _fetch_whatsapp(
    media_id: str | None, token: str | None, declared_mime: str | None,
) -> tuple[bytes, str]:
    """Graph 2-step: resolve the signed media URL, then download it."""
    if not media_id:
        raise MediaDownloadError("whatsapp audio attachment has no media_id")
    if not token or token.startswith("placeholder-"):
        raise MediaDownloadError(
            "whatsapp media download requires a per-tenant WABA token "
            "(empty or placeholder supplied)"
        )
    settings = get_settings()
    base = settings.meta_api_base_url.rstrip("/")
    version = settings.meta_graph_api_version
    meta_url = f"{base}/{version}/{media_id}"
    headers = {"Authorization": f"Bearer {token}"}

    async with _get_client(fallback_timeout=_DOWNLOAD_TIMEOUT_SECONDS) as client:
        meta_resp = await _get(client, meta_url, headers=headers, what="whatsapp media metadata")
        _raise_for_status(meta_resp, "whatsapp media metadata")
        try:
            meta = meta_resp.json()
        except ValueError as exc:
            raise MediaDownloadError("whatsapp media metadata was not JSON") from exc
        signed_url = meta.get("url") if isinstance(meta, dict) else None
        if not signed_url:
            raise MediaDownloadError("whatsapp media metadata had no .url")
        mime = (meta.get("mime_type") if isinstance(meta, dict) else None) or declared_mime

        # Meta requires the Bearer token on the signed-url GET too.
        media_resp = await _get(client, signed_url, headers=headers, what="whatsapp media")
        _raise_for_status(media_resp, "whatsapp media")
        return _guarded_bytes(media_resp), mime or _FALLBACK_MIME


async def _fetch_telegram(
    file_id: str | None, token: str | None, declared_mime: str | None,
) -> tuple[bytes, str]:
    """getFile (file_id → file_path), then GET /file/bot<token>/<file_path>."""
    if not file_id:
        raise MediaDownloadError("telegram audio attachment has no file_id")
    if not token or token.startswith("placeholder-"):
        raise MediaDownloadError(
            "telegram media download requires a per-tenant bot token "
            "(empty or placeholder supplied)"
        )
    base = os.environ.get("TELEGRAM_API_BASE_URL", _TELEGRAM_DEFAULT_BASE).rstrip("/")

    async with _get_client(fallback_timeout=_DOWNLOAD_TIMEOUT_SECONDS) as client:
        get_file_resp = await _get(
            client, f"{base}/bot{token}/getFile",
            params={"file_id": file_id}, what="telegram getFile",
        )
        _raise_for_status(get_file_resp, "telegram getFile")
        try:
            data = get_file_resp.json()
        except ValueError as exc:
            raise MediaDownloadError("telegram getFile response was not JSON") from exc
        if not isinstance(data, dict) or not data.get("ok"):
            raise MediaDownloadError("telegram getFile returned ok=false")
        result = data.get("result") or {}
        file_path = result.get("file_path") if isinstance(result, dict) else None
        if not file_path:
            raise MediaDownloadError("telegram getFile had no file_path")

        dl_resp = await _get(
            client, f"{base}/file/bot{token}/{file_path}", what="telegram media",
        )
        _raise_for_status(dl_resp, "telegram media")
        return _guarded_bytes(dl_resp), declared_mime or _FALLBACK_MIME


async def _fetch_direct(
    url: str | None, declared_mime: str | None,
) -> tuple[bytes, str]:
    """Direct GET of a durable CDN url (IG / Messenger / Chatwoot).

    Security note (SSRF): ``follow_redirects=True`` is required — a Chatwoot
    ActiveStorage ``data_url`` legitimately 302s to the blob. The url itself only
    ever originates from the HMAC-verified Chatwoot agent-bot payload (or a Meta
    CDN url), and ``_extract_chatwoot_attachments`` already rejects non-http(s)
    schemes. A redirect chain terminating at an internal host would require a
    compromised Chatwoot instance or a leaked HMAC secret. Accepted residual risk;
    follow-up = validate redirect-target host against an allow-list (TODO: SSRF-via-redirect).
    """
    if not url:
        raise MediaDownloadError("direct audio attachment has no url")
    async with _get_client(
        fallback_timeout=_DOWNLOAD_TIMEOUT_SECONDS, follow_redirects=True
    ) as client:
        resp = await _get(client, url, timeout=_DOWNLOAD_TIMEOUT_SECONDS, what="direct media")
        _raise_for_status(resp, "direct media")
        mime = declared_mime or resp.headers.get("content-type") or _FALLBACK_MIME
        return _guarded_bytes(resp), mime


# ----------------------------------------------------------------------
# Shared helpers
# ----------------------------------------------------------------------


async def _get(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
    timeout: float = _DOWNLOAD_TIMEOUT_SECONDS,  # noqa: ASYNC109 — httpx's own
    # per-call kwarg name; B6 requires each shared-client GET pass this
    # explicitly rather than rely on the shared client's own default.
    what: str,
) -> httpx.Response:
    """GET wrapper that converts network errors into a tokenless MediaDownloadError.

    httpx embeds the request URL in its exceptions — for Telegram that URL
    carries the bot token in the path — so we raise WITHOUT chaining
    (``from None``) and log only the operation label, never the URL.

    ``timeout`` (audit Batch B / B6) is always passed explicitly per-call so
    the shared client's own default doesn't silently override this module's
    ``_DOWNLOAD_TIMEOUT_SECONDS``.
    """
    try:
        return await client.get(url, headers=headers, params=params, timeout=timeout)
    except httpx.RequestError as exc:
        logger.warning("media_download %s network error: %s", what, type(exc).__name__)
        raise MediaDownloadError(
            f"{what} network error: {type(exc).__name__}", status_code=0
        ) from None


def _raise_for_status(resp: httpx.Response, what: str) -> None:
    if resp.status_code >= 400:
        logger.warning("media_download %s non-2xx: status=%d", what, resp.status_code)
        raise MediaDownloadError(
            f"{what} failed: HTTP {resp.status_code}", status_code=resp.status_code
        )


def _guarded_bytes(resp: httpx.Response) -> bytes:
    """Return the body, raising if it exceeds the configured download cap."""
    max_bytes = get_settings().transcription_max_download_bytes
    declared = resp.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > max_bytes:
                raise MediaDownloadError(
                    f"audio exceeds max download size ({declared} > {max_bytes} bytes)"
                )
        except ValueError:
            pass  # malformed header — fall through to the actual-length check
    body = resp.content
    if len(body) > max_bytes:
        raise MediaDownloadError(
            f"audio exceeds max download size ({len(body)} > {max_bytes} bytes)"
        )
    return body
