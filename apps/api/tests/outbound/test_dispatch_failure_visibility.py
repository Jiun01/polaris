"""Outbound-dispatch silent-loss fixes — DB-free unit tests.

Covers the visibility surfaces added so a reply that never reaches the customer
is no longer silently dropped:

  * sub-fix C — ``pipeline._dispatch_reply`` classifies every send failure
    (DispatchSkipped / provider 4xx / provider 5xx / network) and records it via
    ``_record_dispatch_failure`` with the right (kind, reason); a success records
    nothing.
  * sub-fix C — ``pipeline._record_dispatch_failure`` always emits the
    ``polaris_outbound_dispatch_failures`` metric (and skips the message-row
    stamp when no row id is known) without touching the DB.
  * sub-fix E — ``pipeline._mint_product_image_urls`` counts dropped images and
    emits a partial- vs total-drop signal, while one bad image never kills the
    reply.

All DB-free: the credential resolvers, ``send_reply``, the recorder, and the
metrics module are monkeypatched, so no pool / network is required.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import httpx
import pytest
from polaris_api.services import pipeline
from polaris_api.services.meta_whatsapp import MetaWhatsAppError
from polaris_api.services.reply_dispatch import DispatchSkipped
from polaris_shared_types import OutboundImage, ResponseEnvelope, UsageMetrics


def _env(content: str = "hi", images: list[OutboundImage] | None = None) -> ResponseEnvelope:
    return ResponseEnvelope(
        content=content,
        intent="simple",
        model_used="haiku",
        language_detected="en",
        usage=UsageMetrics(input_tokens=1, output_tokens=1),
        latency_ms=1,
        images=images or [],
    )


# --------------------------------------------------------------------------- C
# _dispatch_reply failure classification


@pytest.fixture
def _patched_dispatch(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Patch _dispatch_reply's collaborators so only classification is exercised.

    Returns the AsyncMock standing in for ``_record_dispatch_failure`` so each
    test can assert the (kind, reason, channel) it recorded.
    """
    recorder = AsyncMock()
    monkeypatch.setattr(pipeline, "_record_dispatch_failure", recorder)
    monkeypatch.setattr(
        pipeline, "_resolve_whatsapp_credentials",
        AsyncMock(return_value=("PID", "tok")),
    )
    monkeypatch.setattr(pipeline, "get_meta_messaging_client", lambda: MagicMock())
    return recorder


async def _run_dispatch(deps_meta: MagicMock | None = None) -> str | None:
    deps = SimpleNamespace(
        meta_client=deps_meta or MagicMock(),
        anon_pool=MagicMock(),
        redis=MagicMock(),
    )
    return await pipeline._dispatch_reply(
        deps=deps,  # type: ignore[arg-type]
        tenant_id=uuid4(),
        channel="whatsapp",
        channel_user_id="60123",
        response=_env(),
        channel_id=uuid4(),
        assistant_msg_id=uuid4(),
    )


@pytest.mark.asyncio
async def test_dispatch_skipped_records_permanent(
    monkeypatch: pytest.MonkeyPatch, _patched_dispatch: AsyncMock
) -> None:
    monkeypatch.setattr(
        pipeline.reply_dispatch, "send_reply",
        AsyncMock(side_effect=DispatchSkipped("whatsapp", "missing_credentials")),
    )
    out = await _run_dispatch()
    assert out is None
    _patched_dispatch.assert_awaited_once()
    kw = _patched_dispatch.await_args.kwargs
    assert kw["kind"] == "permanent"
    assert kw["reason"] == "missing_credentials"
    assert kw["channel"] == "whatsapp"


@pytest.mark.asyncio
async def test_dispatch_provider_4xx_is_permanent(
    monkeypatch: pytest.MonkeyPatch, _patched_dispatch: AsyncMock
) -> None:
    monkeypatch.setattr(
        pipeline.reply_dispatch, "send_reply",
        AsyncMock(side_effect=MetaWhatsAppError("bad", status_code=400)),
    )
    await _run_dispatch()
    kw = _patched_dispatch.await_args.kwargs
    assert kw["kind"] == "permanent"
    assert kw["reason"] == "http_400"


@pytest.mark.asyncio
async def test_dispatch_provider_5xx_is_transient(
    monkeypatch: pytest.MonkeyPatch, _patched_dispatch: AsyncMock
) -> None:
    monkeypatch.setattr(
        pipeline.reply_dispatch, "send_reply",
        AsyncMock(side_effect=MetaWhatsAppError("down", status_code=503)),
    )
    await _run_dispatch()
    kw = _patched_dispatch.await_args.kwargs
    assert kw["kind"] == "transient"
    assert kw["reason"] == "http_503"


@pytest.mark.asyncio
async def test_dispatch_network_error_is_transient(
    monkeypatch: pytest.MonkeyPatch, _patched_dispatch: AsyncMock
) -> None:
    monkeypatch.setattr(
        pipeline.reply_dispatch, "send_reply",
        AsyncMock(side_effect=httpx.ConnectError("no route")),
    )
    await _run_dispatch()
    kw = _patched_dispatch.await_args.kwargs
    assert kw["kind"] == "transient"
    assert kw["reason"] == "network"


@pytest.mark.asyncio
async def test_dispatch_success_records_nothing(
    monkeypatch: pytest.MonkeyPatch, _patched_dispatch: AsyncMock
) -> None:
    monkeypatch.setattr(
        pipeline.reply_dispatch, "send_reply",
        AsyncMock(return_value="wamid.ok"),
    )
    out = await _run_dispatch()
    assert out == "wamid.ok"
    _patched_dispatch.assert_not_awaited()


# --------------------------------------------------------------------------- C
# _record_dispatch_failure metric emission (no row id → no DB)


@pytest.mark.asyncio
async def test_record_dispatch_failure_emits_metric_without_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metric = MagicMock()
    monkeypatch.setattr(pipeline.metrics, "record_dispatch_failure", metric)
    # anon_pool.acquire must never be touched when assistant_msg_id is None.
    pool = MagicMock()
    pool.acquire.side_effect = AssertionError("DB must not be touched")
    deps = SimpleNamespace(anon_pool=pool, redis=MagicMock(), meta_client=MagicMock())
    tenant = uuid4()
    await pipeline._record_dispatch_failure(
        deps=deps,  # type: ignore[arg-type]
        tenant_id=tenant,
        channel="telegram",
        reason="no_bot_token",
        kind="permanent",
        assistant_msg_id=None,
    )
    metric.assert_called_once_with(
        tenant_id=tenant, channel="telegram", reason="no_bot_token", kind="permanent"
    )


# --------------------------------------------------------------------------- E
# _mint_product_image_urls drop signals


def _img(path: str) -> OutboundImage:
    return OutboundImage(storage_path=path)


@pytest.fixture
def _storage_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        pipeline, "get_settings",
        lambda: SimpleNamespace(
            supabase_url="https://x.supabase.co", supabase_service_key="key"
        ),
    )


@pytest.mark.asyncio
async def test_mint_partial_drop_records_partial(
    monkeypatch: pytest.MonkeyPatch, _storage_configured: None
) -> None:
    drops = MagicMock()
    monkeypatch.setattr(pipeline.metrics, "record_image_mint_drops", drops)

    async def _signed(path: str, _settings: object) -> str:
        if path == "bad":
            raise RuntimeError("mint failed")
        return f"https://signed/{path}"

    import polaris_api.services.products.storage as storage_mod
    monkeypatch.setattr(storage_mod, "signed_read_url", _signed)

    urls = await pipeline._mint_product_image_urls(
        [_img("ok1"), _img("bad"), _img("ok2")], tenant_id=uuid4()
    )
    assert urls == ["https://signed/ok1", "https://signed/ok2"]
    drops.assert_called_once()
    assert drops.call_args.kwargs["dropped"] == 1
    assert drops.call_args.kwargs["scope"] == "partial"


@pytest.mark.asyncio
async def test_mint_total_drop_records_total(
    monkeypatch: pytest.MonkeyPatch, _storage_configured: None
) -> None:
    drops = MagicMock()
    monkeypatch.setattr(pipeline.metrics, "record_image_mint_drops", drops)

    async def _signed(_path: str, _settings: object) -> str:
        raise RuntimeError("mint failed")

    import polaris_api.services.products.storage as storage_mod
    monkeypatch.setattr(storage_mod, "signed_read_url", _signed)

    urls = await pipeline._mint_product_image_urls(
        [_img("a"), _img("b")], tenant_id=uuid4()
    )
    assert urls == []
    drops.assert_called_once()
    assert drops.call_args.kwargs["dropped"] == 2
    assert drops.call_args.kwargs["scope"] == "total"


@pytest.mark.asyncio
async def test_mint_storage_unconfigured_is_total_drop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    drops = MagicMock()
    monkeypatch.setattr(pipeline.metrics, "record_image_mint_drops", drops)
    monkeypatch.setattr(
        pipeline, "get_settings",
        lambda: SimpleNamespace(supabase_url="", supabase_service_key=""),
    )
    urls = await pipeline._mint_product_image_urls(
        [_img("a"), _img("b"), _img("c")], tenant_id=uuid4()
    )
    assert urls == []
    drops.assert_called_once()
    assert drops.call_args.kwargs["dropped"] == 3
    assert drops.call_args.kwargs["scope"] == "total"


@pytest.mark.asyncio
async def test_mint_all_ok_records_nothing(
    monkeypatch: pytest.MonkeyPatch, _storage_configured: None
) -> None:
    drops = MagicMock()
    monkeypatch.setattr(pipeline.metrics, "record_image_mint_drops", drops)

    async def _signed(path: str, _settings: object) -> str:
        return f"https://signed/{path}"

    import polaris_api.services.products.storage as storage_mod
    monkeypatch.setattr(storage_mod, "signed_read_url", _signed)

    urls = await pipeline._mint_product_image_urls(
        [_img("a"), _img("b")], tenant_id=uuid4()
    )
    assert urls == ["https://signed/a", "https://signed/b"]
    drops.assert_not_called()
