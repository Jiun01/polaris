"""Per-channel inbound audio download — unit tests for services/media_download.py.

voice-notes M1. DB-free + network-free: monkeypatches
``media_download.httpx.AsyncClient`` with a stub that returns programmed
responses in order (mirrors the meta_whatsapp / telegram_client test style).

Covers the load-bearing complexity of the feature — per-channel download auth:
  - WhatsApp : Graph 2-step (GET /{media_id} for signed url, then GET url),
               both with the WABA Bearer token.
  - Telegram : getFile → file_path, then GET /file/bot<token>/<path>.
  - IG / Messenger / Chatwoot : direct GET of attachment.url (no token).
  - Failure / missing-handle / oversize → MediaDownloadError.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from polaris_api.config import get_settings
from polaris_api.services import media_download as md_mod
from polaris_api.services.media_download import (
    MediaDownloadError,
    _normalize_chatwoot_url,
    fetch_audio_bytes,
)
from polaris_shared_types import Attachment

pytestmark = pytest.mark.asyncio


class _StubAsyncClient:
    """Async-ctx stub for httpx.AsyncClient returning queued responses in order."""

    def __init__(self, responses: list[httpx.Response], calls: list[dict[str, Any]]) -> None:
        self._responses = list(responses)
        self._calls = calls

    async def __aenter__(self) -> _StubAsyncClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,  # noqa: ARG002, ASYNC109 — B6 shared-
        # client call sites always pass an explicit per-call timeout; the
        # stub doesn't enforce it.
    ) -> httpx.Response:
        self._calls.append({"url": url, "headers": headers, "params": params})
        if not self._responses:
            raise AssertionError(f"unexpected extra GET to {url}")
        return self._responses.pop(0)


@pytest.fixture
def patched(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    """Pin an ordered list of httpx responses + capture the GET calls."""

    def _install(responses: list[httpx.Response]) -> list[dict[str, Any]]:
        calls: list[dict[str, Any]] = []

        class _Httpx:
            AsyncClient = staticmethod(lambda *a, **k: _StubAsyncClient(responses, calls))
            Response = httpx.Response
            RequestError = httpx.RequestError

        monkeypatch.setattr(md_mod, "httpx", _Httpx)
        return calls

    return _install


# --------------------------------------------------------------------------
# WhatsApp — Graph 2-step with Bearer token
# --------------------------------------------------------------------------


async def test_whatsapp_two_step_download_uses_bearer_token(patched) -> None:  # type: ignore[no-untyped-def]
    s = get_settings()
    signed = "https://lookaside.fbsbx.com/whatsapp/MEDIA123.ogg"
    calls = patched([
        httpx.Response(200, json={"url": signed, "mime_type": "audio/ogg"}),
        httpx.Response(200, content=b"OGGS-voice-bytes"),
    ])
    att = Attachment(kind="audio", media_id="MEDIA123", mime_type="audio/ogg")

    data, mime = await fetch_audio_bytes(
        provider="whatsapp", attachment=att, access_token="WABA_T0KEN",
    )

    assert data == b"OGGS-voice-bytes"
    assert mime == "audio/ogg"
    # Step 1: GET /{version}/{media_id} on the graph base, Bearer header.
    assert calls[0]["url"] == (
        f"{s.meta_api_base_url.rstrip('/')}/{s.meta_graph_api_version}/MEDIA123"
    )
    assert calls[0]["headers"]["Authorization"] == "Bearer WABA_T0KEN"
    # Step 2: GET the signed url, ALSO with the Bearer header (Meta requires it).
    assert calls[1]["url"] == signed
    assert calls[1]["headers"]["Authorization"] == "Bearer WABA_T0KEN"


async def test_whatsapp_missing_token_raises(patched) -> None:  # type: ignore[no-untyped-def]
    patched([])
    att = Attachment(kind="audio", media_id="MEDIA123")
    with pytest.raises(MediaDownloadError):
        await fetch_audio_bytes(provider="whatsapp", attachment=att, access_token=None)


async def test_whatsapp_placeholder_token_raises(patched) -> None:  # type: ignore[no-untyped-def]
    patched([])
    att = Attachment(kind="audio", media_id="MEDIA123")
    with pytest.raises(MediaDownloadError):
        await fetch_audio_bytes(
            provider="whatsapp", attachment=att, access_token="placeholder-x",
        )


async def test_whatsapp_metadata_non_2xx_raises(patched) -> None:  # type: ignore[no-untyped-def]
    patched([httpx.Response(401, json={"error": {"message": "bad token"}})])
    att = Attachment(kind="audio", media_id="MEDIA123")
    with pytest.raises(MediaDownloadError) as exc:
        await fetch_audio_bytes(
            provider="whatsapp", attachment=att, access_token="WABA_T0KEN",
        )
    assert exc.value.status_code == 401


# --------------------------------------------------------------------------
# Telegram — getFile → /file/bot<token>/<path>
# --------------------------------------------------------------------------


async def test_telegram_get_file_then_download(patched) -> None:  # type: ignore[no-untyped-def]
    calls = patched([
        httpx.Response(200, json={"ok": True, "result": {"file_path": "voice/file_42.oga"}}),
        httpx.Response(200, content=b"TG-voice-bytes"),
    ])
    att = Attachment(kind="audio", media_id="FILE_ID_42", mime_type="audio/ogg")

    data, mime = await fetch_audio_bytes(
        provider="telegram", attachment=att, access_token="BOT_T0KEN",
    )

    assert data == b"TG-voice-bytes"
    assert mime == "audio/ogg"
    # getFile carries the file_id; download hits the /file/bot<token>/<path> base.
    assert calls[0]["url"].endswith("/botBOT_T0KEN/getFile")
    assert calls[0]["params"] == {"file_id": "FILE_ID_42"}
    assert calls[1]["url"].endswith("/file/botBOT_T0KEN/voice/file_42.oga")


async def test_telegram_ok_false_raises(patched) -> None:  # type: ignore[no-untyped-def]
    patched([httpx.Response(200, json={"ok": False})])
    att = Attachment(kind="audio", media_id="FILE_ID_42")
    with pytest.raises(MediaDownloadError):
        await fetch_audio_bytes(
            provider="telegram", attachment=att, access_token="BOT_T0KEN",
        )


async def test_telegram_error_does_not_leak_token(patched) -> None:  # type: ignore[no-untyped-def]
    patched([httpx.Response(200, json={"ok": True, "result": {}})])  # no file_path
    att = Attachment(kind="audio", media_id="FILE_ID_42")
    with pytest.raises(MediaDownloadError) as exc:
        await fetch_audio_bytes(
            provider="telegram", attachment=att, access_token="SECRET_BOT_T0KEN",
        )
    assert "SECRET_BOT_T0KEN" not in str(exc.value)


# --------------------------------------------------------------------------
# Instagram / Messenger / Chatwoot — direct GET, no token
# --------------------------------------------------------------------------


@pytest.mark.parametrize("provider", ["messenger", "chatwoot"])
async def test_direct_url_download_no_token(patched, provider: str) -> None:  # type: ignore[no-untyped-def]
    calls = patched([
        httpx.Response(200, content=b"direct-bytes", headers={"content-type": "audio/mp4"}),
    ])
    att = Attachment(kind="audio", url="https://cdn.example/voice.m4a")

    data, mime = await fetch_audio_bytes(provider=provider, attachment=att, access_token=None)

    assert data == b"direct-bytes"
    assert mime == "audio/mp4"  # falls back to the response content-type
    assert calls[0]["url"] == "https://cdn.example/voice.m4a"
    assert calls[0]["headers"] is None  # no auth header on a durable CDN url


async def test_direct_url_missing_raises(patched) -> None:  # type: ignore[no-untyped-def]
    patched([])
    att = Attachment(kind="audio", url=None)
    with pytest.raises(MediaDownloadError):
        await fetch_audio_bytes(provider="messenger", attachment=att, access_token=None)


async def test_direct_non_2xx_raises(patched) -> None:  # type: ignore[no-untyped-def]
    patched([httpx.Response(404, content=b"not found")])
    att = Attachment(kind="audio", url="https://cdn.example/gone.m4a")
    with pytest.raises(MediaDownloadError) as exc:
        await fetch_audio_bytes(provider="chatwoot", attachment=att, access_token=None)
    assert exc.value.status_code == 404


# --------------------------------------------------------------------------
# Cross-cutting guards
# --------------------------------------------------------------------------


async def test_oversize_download_raises(patched, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    # Shrink the cap so a tiny body trips it deterministically.
    monkeypatch.setattr(get_settings(), "transcription_max_download_bytes", 4, raising=False)
    patched([
        httpx.Response(200, json={"url": "https://x/y.ogg", "mime_type": "audio/ogg"}),
        httpx.Response(200, content=b"way too many bytes"),
    ])
    att = Attachment(kind="audio", media_id="MEDIA123")
    with pytest.raises(MediaDownloadError):
        await fetch_audio_bytes(
            provider="whatsapp", attachment=att, access_token="WABA_T0KEN",
        )


async def test_unsupported_provider_raises(patched) -> None:  # type: ignore[no-untyped-def]
    patched([])
    att = Attachment(kind="audio", url="https://x/y.ogg")
    with pytest.raises(MediaDownloadError):
        await fetch_audio_bytes(provider="smoke-signals", attachment=att, access_token=None)


async def test_network_error_raises_without_token(patched, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    class _Raising:
        async def __aenter__(self) -> _Raising:
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        async def get(self, url: str, **_: Any) -> httpx.Response:
            raise httpx.ConnectError("boom", request=httpx.Request("GET", url))

    class _Httpx:
        AsyncClient = staticmethod(lambda *a, **k: _Raising())
        Response = httpx.Response
        RequestError = httpx.RequestError

    monkeypatch.setattr(md_mod, "httpx", _Httpx)
    att = Attachment(kind="audio", media_id="FILE_ID_42")
    with pytest.raises(MediaDownloadError) as exc:
        await fetch_audio_bytes(
            provider="telegram", attachment=att, access_token="SECRET_BOT_T0KEN",
        )
    assert "SECRET_BOT_T0KEN" not in str(exc.value)


# --------------------------------------------------------------------------
# Chatwoot-native rehaul — via_chatwoot uses the data_url for EVERY channel
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "provider", ["whatsapp", "telegram", "messenger", "instagram_login"]
)
async def test_via_chatwoot_uses_direct_url_for_all_channels(patched, provider: str) -> None:  # type: ignore[no-untyped-def]
    """Post-rehaul every channel arrives via Chatwoot with a data_url. via_chatwoot
    must short-circuit to a single direct GET — NEVER the Meta Graph 2-step or the
    Telegram getFile path (which would use the Chatwoot attachment id as a handle)."""
    calls = patched([
        httpx.Response(200, content=b"cw-voice-bytes", headers={"content-type": "audio/opus"}),
    ])
    # media_id is the Chatwoot attachment id (NOT a Meta/Telegram handle) + a data_url.
    att = Attachment(kind="audio", media_id="3", url="https://chat.getpolaris.my/rails/blob/x/File.ogg")

    data, mime = await fetch_audio_bytes(
        provider=provider, attachment=att, access_token="UNUSED", via_chatwoot=True,
    )

    assert data == b"cw-voice-bytes"
    assert mime == "audio/opus"
    assert len(calls) == 1  # exactly one GET — no Graph metadata / getFile hop
    assert calls[0]["url"] == "https://chat.getpolaris.my/rails/blob/x/File.ogg"
    assert calls[0]["headers"] is None  # token-free


async def test_via_chatwoot_rewrites_loopback_data_url_host(patched) -> None:  # type: ignore[no-untyped-def]
    """A mis-set Chatwoot FRONTEND_URL bakes http://0.0.0.0:3000 into the data_url;
    via_chatwoot rewrites the host to the reachable chatwoot_internal_base_url."""
    s = get_settings()
    calls = patched([httpx.Response(200, content=b"x", headers={"content-type": "audio/ogg"})])
    att = Attachment(
        kind="audio", media_id="3",
        url="http://0.0.0.0:3000/rails/active_storage/blobs/redirect/abc/File.ogg?disposition=inline",
    )

    await fetch_audio_bytes(
        provider="whatsapp", attachment=att, access_token=None, via_chatwoot=True,
    )

    internal = s.chatwoot_internal_base_url.rstrip("/")
    assert calls[0]["url"] == (
        f"{internal}/rails/active_storage/blobs/redirect/abc/File.ogg?disposition=inline"
    )


async def test_via_chatwoot_missing_url_raises(patched) -> None:  # type: ignore[no-untyped-def]
    patched([])
    att = Attachment(kind="audio", media_id="3", url=None)
    with pytest.raises(MediaDownloadError):
        await fetch_audio_bytes(
            provider="whatsapp", attachment=att, access_token=None, via_chatwoot=True,
        )


async def test_normalize_chatwoot_url_rewrites_only_loopback_hosts() -> None:
    s = get_settings()
    internal = s.chatwoot_internal_base_url.rstrip("/")
    # loopback / non-routable hosts → rewritten to the internal base, path preserved
    assert _normalize_chatwoot_url("http://0.0.0.0:3000/blob/x?d=1") == f"{internal}/blob/x?d=1"
    assert _normalize_chatwoot_url("http://localhost:3000/blob/x") == f"{internal}/blob/x"
    assert _normalize_chatwoot_url("http://127.0.0.1:3000/y") == f"{internal}/y"
    # IPv6 loopback literal — urlsplit().hostname strips the brackets to '::1'
    assert _normalize_chatwoot_url("http://[::1]:3000/blob/z") == f"{internal}/blob/z"
    # path + query + fragment all preserved
    assert (
        _normalize_chatwoot_url("http://0.0.0.0:3000/a/b?q=1#frag")
        == f"{internal}/a/b?q=1#frag"
    )
    # already-routable host → unchanged
    pub = "https://chat.getpolaris.my/rails/blob/x?disposition=inline"
    assert _normalize_chatwoot_url(pub) == pub
    # falsy url (None or "") → None (the fetcher raises on it)
    assert _normalize_chatwoot_url(None) is None
    assert _normalize_chatwoot_url("") is None
