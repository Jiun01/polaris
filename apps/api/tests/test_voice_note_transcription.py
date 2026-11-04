"""Inbound voice notes — unit + pipeline integration tests (voice-notes M1).

Two test groups in one file (per the plan):
  * ``transcribe`` — Groq Whisper client (services/transcription.py).
  * pipeline integration — services/pipeline_voice.py voice-note branch
    (extracted from pipeline.py in Batch E, task E1; tier gate,
    transcribe→reply, failure fallback, Chatwoot private note).

DB-free + network-free: the Groq HTTP call is monkeypatched the same way
the meta_whatsapp / telegram_client tests stub httpx; the pipeline tests
reuse the ``_make_pool_with_conn`` harness shape from
``test_pipeline_end_to_end.py`` with AsyncMock service doubles.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from polaris_api.config import get_settings
from polaris_api.services import transcription as tx_mod
from polaris_api.services.transcription import (
    TranscriptionError,
    TranscriptionResult,
    transcribe,
)

pytestmark = pytest.mark.asyncio


# ==========================================================================
# Group 1: transcribe() — Groq Whisper client
# ==========================================================================


class _StubAsyncClient:
    """Async-ctx stub for httpx.AsyncClient capturing the multipart POST."""

    def __init__(self, response: httpx.Response, calls: list[dict[str, Any]]) -> None:
        self._response = response
        self._calls = calls

    async def __aenter__(self) -> _StubAsyncClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def post(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        data: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
    ) -> httpx.Response:
        self._calls.append({"url": url, "headers": headers, "data": data, "files": files})
        return self._response


@pytest.fixture
def patched_groq(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    """Pin the next Groq httpx response + a real-looking API key; capture calls."""
    monkeypatch.setattr(get_settings(), "groq_api_key", "gsk_realkey", raising=False)
    monkeypatch.setattr(get_settings(), "transcription_stub_enabled", False, raising=False)

    def _install(response: httpx.Response) -> list[dict[str, Any]]:
        calls: list[dict[str, Any]] = []

        class _Httpx:
            AsyncClient = staticmethod(lambda *a, **k: _StubAsyncClient(response, calls))
            Response = httpx.Response
            RequestError = httpx.RequestError

        monkeypatch.setattr(tx_mod, "httpx", _Httpx)
        return calls

    return _install


async def test_transcribe_happy_path_parses_text_and_language(patched_groq) -> None:  # type: ignore[no-untyped-def]
    calls = patched_groq(
        httpx.Response(200, json={"text": "  apa khabar boss  ", "language": "malay"})
    )
    result = await transcribe(b"OGGS-bytes", "audio/ogg", filename="voice.ogg")

    assert isinstance(result, TranscriptionResult)
    assert result.text == "apa khabar boss"  # stripped
    assert result.language == "malay"
    # Multipart POST: model + verbose_json (no language param → auto-detect),
    # Bearer auth, the audio under "file".
    sent = calls[0]
    assert sent["url"].endswith("/openai/v1/audio/transcriptions")
    assert sent["headers"]["Authorization"] == "Bearer gsk_realkey"
    assert sent["data"]["model"] == get_settings().transcription_model
    assert sent["data"]["response_format"] == "verbose_json"
    assert "language" not in sent["data"]
    assert sent["files"]["file"] == ("voice.ogg", b"OGGS-bytes", "audio/ogg")


async def test_transcribe_stub_mode_returns_canned(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "transcription_stub_enabled", True, raising=False)
    result = await transcribe(b"anything", "audio/ogg", filename="voice.ogg")
    assert result.text == "hello this is a test"
    assert result.language == "en"


async def test_transcribe_placeholder_key_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "transcription_stub_enabled", False, raising=False)
    monkeypatch.setattr(get_settings(), "groq_api_key", "placeholder-groq-key", raising=False)
    with pytest.raises(TranscriptionError):
        await transcribe(b"x", "audio/ogg", filename="voice.ogg")


async def test_transcribe_non_2xx_raises_with_status(patched_groq) -> None:  # type: ignore[no-untyped-def]
    patched_groq(httpx.Response(429, json={"error": "rate limited"}))
    with pytest.raises(TranscriptionError) as exc:
        await transcribe(b"x", "audio/ogg", filename="voice.ogg")
    assert exc.value.status_code == 429


async def test_transcribe_empty_text_is_failure(patched_groq) -> None:  # type: ignore[no-untyped-def]
    patched_groq(httpx.Response(200, json={"text": "   ", "language": "en"}))
    with pytest.raises(TranscriptionError):
        await transcribe(b"x", "audio/ogg", filename="voice.ogg")


async def test_transcribe_missing_language_defaults(patched_groq) -> None:  # type: ignore[no-untyped-def]
    patched_groq(httpx.Response(200, json={"text": "hello there"}))
    result = await transcribe(b"x", "audio/ogg", filename="voice.ogg")
    assert result.text == "hello there"
    assert result.language == "unknown"


async def test_transcribe_network_error_does_not_leak_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "transcription_stub_enabled", False, raising=False)
    monkeypatch.setattr(get_settings(), "groq_api_key", "gsk_SECRETKEY", raising=False)

    class _Raising:
        async def __aenter__(self) -> _Raising:
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        async def post(self, url: str, **_: Any) -> httpx.Response:
            raise httpx.ConnectError("boom", request=httpx.Request("POST", url))

    class _Httpx:
        AsyncClient = staticmethod(lambda *a, **k: _Raising())
        Response = httpx.Response
        RequestError = httpx.RequestError

    monkeypatch.setattr(tx_mod, "httpx", _Httpx)
    with pytest.raises(TranscriptionError) as exc:
        await transcribe(b"x", "audio/ogg", filename="voice.ogg")
    assert exc.value.status_code == 0
    assert "gsk_SECRETKEY" not in str(exc.value)
    assert exc.value.__cause__ is None


# ==========================================================================
# Group 2: pipeline voice-note branch (tier gate, transcribe→reply, fallback)
# ==========================================================================

from datetime import UTC, datetime  # noqa: E402
from unittest.mock import AsyncMock, MagicMock  # noqa: E402
from uuid import uuid4  # noqa: E402

from polaris_api.services.media_download import MediaDownloadError  # noqa: E402

from tests.conftest import make_mock_anthropic_message  # noqa: E402
from tests.test_pipeline_end_to_end import (  # noqa: E402
    _FakeAcquire,
    _FakeTransaction,
    _make_incoming,
    _make_meta_client,
)

_GATED_NOTE = "Voice Note is a Pro+ Feature"


def _audio_incoming(*, provider: str = "whatsapp", **overrides: Any) -> Any:
    """An inbound voice note: no body, one audio attachment."""
    from polaris_shared_types import Attachment

    kind_handle = (
        {"media_id": "MEDIA123"}
        if provider in ("whatsapp", "telegram")
        else {"url": "https://cdn.example/voice.m4a"}
    )
    att = Attachment(kind="audio", mime_type="audio/ogg", **kind_handle)
    return _make_incoming(provider=provider, body=None, attachments=[att], **overrides)


def _make_voice_pool(
    *,
    plan_tier: str = "pro",
    cw_row: dict[str, Any] | None = None,
) -> tuple[MagicMock, AsyncMock]:
    """Pool/conn double whose fetchrow routes voice-relevant SQL."""
    conn = AsyncMock()
    conn.transaction = MagicMock(return_value=_FakeTransaction())

    ids = {
        "conv": uuid4(),
        "user_msg": uuid4(),
        "assistant_msg": uuid4(),
    }

    async def fetchrow(sql: str, *args: Any, **kwargs: Any) -> Any:
        s = " ".join(sql.split())
        if "INSERT INTO conversations" in s:
            return {"id": ids["conv"], "tenant_branch_id": None}
        if "chatwoot_conversation_id" in s and "JOIN tenant_channels" in s:
            return cw_row
        if "FROM tenants" in s and "plan_tier" in s:
            return {"plan_tier": plan_tier, "cohere_rerank_enabled": None}
        if "FROM tenant_secrets" in s:
            return None  # no token row; download is mocked anyway
        if "FROM knowledge_chunks" in s:
            return {"has": False}
        if "INSERT INTO messages" in s and "'assistant'" in s:
            return {"id": ids["assistant_msg"], "created_at": datetime.now(UTC)}
        if "INSERT INTO messages" in s and "RETURNING id, created_at" in s:
            return {"id": ids["user_msg"], "created_at": datetime.now(UTC)}
        return None

    conn.fetchrow = AsyncMock(side_effect=fetchrow)
    conn.fetch = AsyncMock(return_value=[])
    conn.execute = AsyncMock(return_value=None)

    pool = MagicMock()
    pool.acquire = MagicMock(side_effect=lambda: _FakeAcquire(conn))
    pool._ids = ids
    return pool, conn


def _patch_audio_pipeline(
    monkeypatch: pytest.MonkeyPatch,
    *,
    transcribe_result: Any = None,
    transcribe_error: Exception | None = None,
) -> tuple[AsyncMock, AsyncMock]:
    """Stub pipeline_voice.media_download + pipeline_voice.transcription.

    Extracted from pipeline.py into pipeline_voice.py in Batch E (task E1) —
    the voice-note branch (and its media_download/transcription module
    references) now lives there. Returns the mocks.
    """
    from polaris_api.services import pipeline_voice as pipeline_voice_mod

    download = AsyncMock(return_value=(b"OGGS-bytes", "audio/ogg"))
    monkeypatch.setattr(pipeline_voice_mod.media_download, "fetch_audio_bytes", download)

    if transcribe_error is not None:
        tx = AsyncMock(side_effect=transcribe_error)
    else:
        tx = AsyncMock(
            return_value=transcribe_result
            or TranscriptionResult(text="what time do you open", language="english")
        )
    monkeypatch.setattr(pipeline_voice_mod.transcription, "transcribe", tx)
    return download, tx


def _make_cw_client_mock() -> MagicMock:
    cw = MagicMock()
    cw.post_private_note = AsyncMock(return_value=1)
    cw.post_outbound_message = AsyncMock(return_value=2)
    cw.apply_conversation_label = AsyncMock(return_value=None)
    cw.ensure_contact = AsyncMock(return_value=10)
    cw.create_conversation = AsyncMock(return_value=555)
    return cw


def _patch_cw_client(monkeypatch: pytest.MonkeyPatch, cw: MagicMock) -> None:
    from polaris_api.services.chatwoot import client as cw_client_mod

    monkeypatch.setattr(cw_client_mod, "get_chatwoot_client", lambda: cw)


async def test_starter_tenant_voice_note_is_gated(
    monkeypatch: pytest.MonkeyPatch,
    fake_redis: Any,
) -> None:
    """Starter tenant → no transcription, customer gets 'please type' reply."""
    from polaris_api.services.pipeline import PipelineDeps, run

    download, tx = _patch_audio_pipeline(monkeypatch)
    pool, _conn = _make_voice_pool(plan_tier="starter")
    deps = PipelineDeps(anon_pool=pool, redis=fake_redis, meta_client=_make_meta_client())

    decision = await run(incoming=_audio_incoming(), deps=deps)

    tx.assert_not_awaited()  # never transcribed
    download.assert_not_awaited()  # never downloaded
    assert decision.handoff_emitted is False
    assert "voice note" in decision.response.content.lower()


async def test_starter_gate_posts_pro_plus_private_note(
    monkeypatch: pytest.MonkeyPatch,
    fake_redis: Any,
) -> None:
    """Starter gate → 'Voice Note is a Pro+ Feature' private note in Chatwoot."""
    from polaris_api.services.pipeline import PipelineDeps, run

    _patch_audio_pipeline(monkeypatch)
    cw = _make_cw_client_mock()
    _patch_cw_client(monkeypatch, cw)
    pool, _conn = _make_voice_pool(plan_tier="starter")
    deps = PipelineDeps(anon_pool=pool, redis=fake_redis, meta_client=_make_meta_client())

    # Chatwoot-native: the CW conversation/account ids ride on the inbound
    # (agent-bot webhook); _link_cw_conversation reads them off the message, not
    # a JOIN lookup. Set them so the gated-feature private note targets a CW conv.
    await run(
        incoming=_audio_incoming(chatwoot_conversation_id=555, chatwoot_account_id=1),
        deps=deps,
    )

    notes = [
        c for c in cw.post_private_note.await_args_list if c.kwargs.get("content") == _GATED_NOTE
    ]
    assert notes, (
        f"expected a '{_GATED_NOTE}' private note; got {cw.post_private_note.await_args_list}"
    )


async def test_pro_tenant_voice_note_transcribes_and_replies(
    monkeypatch: pytest.MonkeyPatch,
    mock_anthropic: MagicMock,
    fake_redis: Any,
) -> None:
    """Pro+ tenant → transcript substituted as user_text → normal LLM reply."""
    from polaris_api.services.pipeline import PipelineDeps, run

    download, tx = _patch_audio_pipeline(monkeypatch)
    mock_anthropic.messages.create.side_effect = [
        make_mock_anthropic_message(text="simple"),  # classify
        make_mock_anthropic_message(text="We open at 9am."),  # generate
    ]
    pool, _conn = _make_voice_pool(plan_tier="pro")
    deps = PipelineDeps(anon_pool=pool, redis=fake_redis, meta_client=_make_meta_client())

    decision = await run(incoming=_audio_incoming(), deps=deps)

    download.assert_awaited_once()
    tx.assert_awaited_once()
    # Reply is the generated one — proves the transcript was substituted
    # (without substitution body=None would short-circuit to handoff).
    assert decision.response.content == "We open at 9am."
    assert decision.handoff_emitted is False


async def test_pro_voice_note_posts_voice_note_private_note(
    monkeypatch: pytest.MonkeyPatch,
    mock_anthropic: MagicMock,
    fake_redis: Any,
) -> None:
    """Successful transcription → 'Voice Note: <transcript>' private note."""
    from polaris_api.services.pipeline import PipelineDeps, run

    _patch_audio_pipeline(
        monkeypatch,
        transcribe_result=TranscriptionResult(text="apa khabar", language="malay"),
    )
    mock_anthropic.messages.create.side_effect = [
        make_mock_anthropic_message(text="simple"),
        make_mock_anthropic_message(text="Khabar baik!"),
    ]
    cw = _make_cw_client_mock()
    _patch_cw_client(monkeypatch, cw)
    pool, _conn = _make_voice_pool(plan_tier="pro")
    deps = PipelineDeps(anon_pool=pool, redis=fake_redis, meta_client=_make_meta_client())

    # Chatwoot-native: CW ids ride on the inbound (see _link_cw_conversation).
    await run(
        incoming=_audio_incoming(chatwoot_conversation_id=555, chatwoot_account_id=1),
        deps=deps,
    )

    notes = [
        c
        for c in cw.post_private_note.await_args_list
        if str(c.kwargs.get("content", "")).startswith("Voice Note: apa khabar")
    ]
    assert notes, (
        f"expected 'Voice Note: apa khabar' note; got {cw.post_private_note.await_args_list}"
    )


async def test_transcription_failure_falls_back_no_llm(
    monkeypatch: pytest.MonkeyPatch,
    mock_anthropic: MagicMock,
    fake_redis: Any,
) -> None:
    """Download/transcription failure → polite fallback, no LLM classify/generate."""
    from polaris_api.services.pipeline import PipelineDeps, run

    _patch_audio_pipeline(monkeypatch, transcribe_error=TranscriptionError("boom"))
    pool, _conn = _make_voice_pool(plan_tier="pro")
    deps = PipelineDeps(anon_pool=pool, redis=fake_redis, meta_client=_make_meta_client())

    decision = await run(incoming=_audio_incoming(), deps=deps)

    assert mock_anthropic.messages.create.await_count == 0  # never reached classify
    assert decision.handoff_emitted is False
    assert "voice note" in decision.response.content.lower()


async def test_download_failure_falls_back(
    monkeypatch: pytest.MonkeyPatch,
    mock_anthropic: MagicMock,
    fake_redis: Any,
) -> None:
    """A MediaDownloadError is handled by the same graceful fallback path."""
    from polaris_api.services import pipeline_voice as pipeline_voice_mod
    from polaris_api.services.pipeline import PipelineDeps, run

    monkeypatch.setattr(
        pipeline_voice_mod.media_download,
        "fetch_audio_bytes",
        AsyncMock(side_effect=MediaDownloadError("nope", status_code=404)),
    )
    tx = AsyncMock()
    monkeypatch.setattr(pipeline_voice_mod.transcription, "transcribe", tx)
    pool, _conn = _make_voice_pool(plan_tier="pro")
    deps = PipelineDeps(anon_pool=pool, redis=fake_redis, meta_client=_make_meta_client())

    decision = await run(incoming=_audio_incoming(provider="messenger"), deps=deps)

    tx.assert_not_awaited()  # download failed before transcription
    assert mock_anthropic.messages.create.await_count == 0
    assert "voice note" in decision.response.content.lower()


async def test_voice_note_disabled_falls_through_to_handoff(
    monkeypatch: pytest.MonkeyPatch,
    fake_redis: Any,
) -> None:
    """TRANSCRIPTION_ENABLED=false → voice note falls through to media-only handoff."""
    from polaris_api.services.pipeline import PipelineDeps, run

    monkeypatch.setattr(get_settings(), "transcription_enabled", False, raising=False)
    download, tx = _patch_audio_pipeline(monkeypatch)
    pool, _conn = _make_voice_pool(plan_tier="pro")
    deps = PipelineDeps(anon_pool=pool, redis=fake_redis, meta_client=_make_meta_client())

    decision = await run(incoming=_audio_incoming(), deps=deps)

    tx.assert_not_awaited()
    download.assert_not_awaited()
    assert decision.handoff_emitted is True  # existing media-only behavior preserved


@pytest.mark.parametrize("provider", ["whatsapp", "instagram_login", "messenger"])
async def test_voice_note_via_chatwoot_flag_set_for_chatwoot_inbound(
    monkeypatch: pytest.MonkeyPatch,
    mock_anthropic: MagicMock,
    fake_redis: Any,
    provider: str,
) -> None:
    """Post-rehaul every channel arrives via Chatwoot (chatwoot_account_id set), so the
    download must be invoked with via_chatwoot=True — i.e. fetched from the data_url, NOT
    the Meta/Telegram media-id path. Covers WhatsApp + IG + Messenger."""
    from polaris_api.services.pipeline import PipelineDeps, run

    download, _tx = _patch_audio_pipeline(monkeypatch)
    mock_anthropic.messages.create.side_effect = [
        make_mock_anthropic_message(text="simple"),
        make_mock_anthropic_message(text="We open at 9am."),
    ]
    pool, _conn = _make_voice_pool(plan_tier="pro")
    deps = PipelineDeps(anon_pool=pool, redis=fake_redis, meta_client=_make_meta_client())

    await run(
        incoming=_audio_incoming(
            provider=provider,
            chatwoot_conversation_id=555,
            chatwoot_account_id=1,
        ),
        deps=deps,
    )

    download.assert_awaited_once()
    assert download.await_args.kwargs["via_chatwoot"] is True


async def test_voice_note_via_chatwoot_flag_false_without_chatwoot_ids(
    monkeypatch: pytest.MonkeyPatch,
    mock_anthropic: MagicMock,
    fake_redis: Any,
) -> None:
    """Defensive: an inbound with no chatwoot_account_id → via_chatwoot=False (falls back
    to provider routing). No live ingress does this today, but the branch must be correct."""
    from polaris_api.services.pipeline import PipelineDeps, run

    download, _tx = _patch_audio_pipeline(monkeypatch)
    mock_anthropic.messages.create.side_effect = [
        make_mock_anthropic_message(text="simple"),
        make_mock_anthropic_message(text="We open at 9am."),
    ]
    pool, _conn = _make_voice_pool(plan_tier="pro")
    deps = PipelineDeps(anon_pool=pool, redis=fake_redis, meta_client=_make_meta_client())

    await run(incoming=_audio_incoming(), deps=deps)  # no chatwoot_* ids

    download.assert_awaited_once()
    assert download.await_args.kwargs["via_chatwoot"] is False
