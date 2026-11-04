"""Tests for ``meta_whatsapp.send_text`` + ``send_template`` (Phase 6 PR-2).

Covers:
  - ``send_text`` works when an explicit access_token is provided.
  - ``send_text`` refuses a send with no token (ADR-066 removed the
    demo-WABA fallback — a missing token raises rather than guessing).
  - ``send_template`` routes to ``/messages`` when use_marketing_endpoint
    is False, and ``/marketing_messages`` when True. The flag is the
    SOLE decision surface — no DB lookup inside the client.
  - ``send_text`` 4xx raises MetaWhatsAppError carrying status_code so
    the dispatch layer can classify retryable vs hard-fail.
"""

from __future__ import annotations

import pytest
from polaris_api.services.meta_whatsapp import (
    MetaWhatsAppClient,
    MetaWhatsAppError,
)

from .conftest import MetaResponse


@pytest.mark.asyncio
async def test_send_text_non_demo_pid_now_works(
    meta_client: MetaWhatsAppClient,
    stub_httpx_client,  # noqa: ARG001 — pytest fixture wires the patch
    meta_route_map: dict,
) -> None:
    """Phase 4 raised NotImplementedError for non-demo PIDs; PR-2
    lifts that restriction when an explicit access_token is passed."""
    meta_route_map[("POST", "TENANT_PID/messages")] = MetaResponse(
        status_code=200,
        json_body={"messages": [{"id": "wamid.tenant.001"}]},
    )

    wamid = await meta_client.send_text(
        phone_number_id="TENANT_PID",
        to="60123456789",
        body="hi from a tenant WABA",
        access_token="EAA-tenant-token",
    )

    assert wamid == "wamid.tenant.001"


@pytest.mark.asyncio
async def test_send_text_without_token_raises(
    meta_client: MetaWhatsAppClient,
) -> None:
    """Every send MUST carry an explicit access_token (per-channel, decrypted
    from tenant_secrets). ADR-066 removed the demo-WABA fallback, so a missing
    token raises rather than guessing a sender."""
    with pytest.raises(
        RuntimeError, match="access_token is empty/placeholder/missing"
    ):
        await meta_client.send_text(
            phone_number_id="TENANT_PID",
            to="60123456789",
            body="no-token",
        )


@pytest.mark.asyncio
async def test_send_text_4xx_raises_with_status_code(
    meta_client: MetaWhatsAppClient,
    stub_httpx_client,  # noqa: ARG001
    meta_route_map: dict,
) -> None:
    """A 4xx propagates as MetaWhatsAppError with the status_code
    preserved — dispatch layer needs it to decide retry policy."""
    meta_route_map[("POST", "TENANT_PID/messages")] = MetaResponse(
        status_code=403,
        json_body={"error": {"message": "Permission denied", "code": 200}},
    )

    with pytest.raises(MetaWhatsAppError) as excinfo:
        await meta_client.send_text(
            phone_number_id="TENANT_PID",
            to="60123456789",
            body="forbidden",
            access_token="EAA-tenant-token",
        )

    assert excinfo.value.status_code == 403


@pytest.mark.asyncio
async def test_send_template_uses_messages_when_marketing_false(
    meta_client: MetaWhatsAppClient,
    stub_httpx_client,
    meta_route_map: dict,
) -> None:
    """use_marketing_endpoint=False routes to /messages regardless
    of template category — the flag IS the decision surface."""
    meta_route_map[("POST", "TENANT_PID/messages")] = MetaResponse(
        status_code=200,
        json_body={"messages": [{"id": "wamid.tpl.001"}]},
    )

    wamid = await meta_client.send_template(
        phone_number_id="TENANT_PID",
        to="60123456789",
        template_name="welcome_v1",
        language="en_US",
        variables={"1": "Jane"},
        access_token="EAA-tenant-token",
        use_marketing_endpoint=False,
    )

    assert wamid == "wamid.tpl.001"
    assert stub_httpx_client.calls[0].suffix == "TENANT_PID/messages"


@pytest.mark.asyncio
async def test_send_template_uses_marketing_when_flag_true(
    meta_client: MetaWhatsAppClient,
    stub_httpx_client,
    meta_route_map: dict,
) -> None:
    """use_marketing_endpoint=True routes to /marketing_messages.
    Pre-condition for MM Lite ONBOARDED + MARKETING category sends."""
    meta_route_map[("POST", "TENANT_PID/marketing_messages")] = MetaResponse(
        status_code=200,
        json_body={"messages": [{"id": "wamid.mkt.001"}]},
    )

    wamid = await meta_client.send_template(
        phone_number_id="TENANT_PID",
        to="60123456789",
        template_name="promo_v1",
        language="ms_MY",
        variables={"1": "Tan", "2": "RM 50"},
        access_token="EAA-tenant-token",
        use_marketing_endpoint=True,
    )

    assert wamid == "wamid.mkt.001"
    assert stub_httpx_client.calls[0].suffix == "TENANT_PID/marketing_messages"


@pytest.mark.asyncio
async def test_send_template_renders_parameters_in_position_order(
    meta_client: MetaWhatsAppClient,
    stub_httpx_client,
    meta_route_map: dict,
) -> None:
    """Positional parameters render in numeric order regardless of
    dict insertion order — Meta's body template references {{1}},
    {{2}}, ... by position."""
    meta_route_map[("POST", "TENANT_PID/messages")] = MetaResponse(
        status_code=200,
        json_body={"messages": [{"id": "wamid.tpl.002"}]},
    )

    await meta_client.send_template(
        phone_number_id="TENANT_PID",
        to="60123456789",
        template_name="multi_var",
        language="en_US",
        variables={"3": "third", "1": "first", "2": "second"},
        access_token="EAA-tenant-token",
        use_marketing_endpoint=False,
    )

    payload = stub_httpx_client.calls[0].json
    assert payload is not None
    params = payload["template"]["components"][0]["parameters"]
    assert [p["text"] for p in params] == ["first", "second", "third"]
