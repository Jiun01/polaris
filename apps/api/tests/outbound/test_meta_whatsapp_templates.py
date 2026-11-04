"""Tests for ``meta_whatsapp.submit_template`` + ``delete_template``
(Phase 6 PR-2).

Covers:
  - ``submit_template`` returns the Meta-assigned template id on 2xx.
  - ``submit_template`` 409 duplicate-name raises MetaTemplateError
    with Meta's error envelope fields populated.
  - ``submit_template`` 400 bad-components raises MetaTemplateError
    with error_data.details preserved verbatim (the portal surfaces
    this text directly to the tenant).
  - ``delete_template`` happy 200 returns None.
  - ``delete_template`` 404 is idempotent (no exception raised) —
    template-never-existed is indistinguishable from already-deleted.
"""

from __future__ import annotations

import pytest
from polaris_api.services.meta_whatsapp import (
    MetaTemplateError,
    MetaWhatsAppClient,
)

from .conftest import MetaResponse


@pytest.mark.asyncio
async def test_submit_returns_meta_template_id(
    meta_client: MetaWhatsAppClient,
    stub_httpx_client,  # noqa: ARG001
    meta_route_map: dict,
) -> None:
    meta_route_map[("POST", "WABA123/message_templates")] = MetaResponse(
        status_code=200,
        json_body={"id": "TPL_456789", "status": "PENDING", "category": "UTILITY"},
    )

    template_id = await meta_client.submit_template(
        waba_id="WABA123",
        name="phase6_smoke",
        language="en_MY",
        category="UTILITY",
        components=[{"type": "BODY", "text": "Hello {{1}}"}],
        access_token="EAA-tenant-token",
    )

    assert template_id == "TPL_456789"


@pytest.mark.asyncio
async def test_submit_409_duplicate_raises_template_error(
    meta_client: MetaWhatsAppClient,
    stub_httpx_client,  # noqa: ARG001
    meta_route_map: dict,
) -> None:
    meta_route_map[("POST", "WABA123/message_templates")] = MetaResponse(
        status_code=409,
        json_body={
            "error": {
                "message": "Template name already exists",
                "code": 100,
                "error_subcode": 2388023,
                "error_data": {
                    "messaging_product": "whatsapp",
                    "details": "Template named phase6_smoke already exists in en_US",
                },
            }
        },
    )

    with pytest.raises(MetaTemplateError) as excinfo:
        await meta_client.submit_template(
            waba_id="WABA123",
            name="phase6_smoke",
            language="en_US",
            category="UTILITY",
            components=[{"type": "BODY", "text": "Hi"}],
            access_token="EAA-tenant-token",
        )

    err = excinfo.value
    assert err.status_code == 409
    assert err.code == 100
    assert err.error_subcode == 2388023
    assert err.details is not None
    assert "already exists" in err.details


@pytest.mark.asyncio
async def test_submit_400_bad_components_raises_with_details(
    meta_client: MetaWhatsAppClient,
    stub_httpx_client,  # noqa: ARG001
    meta_route_map: dict,
) -> None:
    """Meta's error_data.details is the tenant-actionable string —
    the lifecycle handler stores it verbatim into rejection_reason."""
    meta_route_map[("POST", "WABA123/message_templates")] = MetaResponse(
        status_code=400,
        json_body={
            "error": {
                "message": "Invalid parameter",
                "code": 100,
                "error_subcode": 2388043,
                "error_data": {
                    "messaging_product": "whatsapp",
                    "details": (
                        "Body text references {{1}} but no example provided in "
                        "components.example.body_text"
                    ),
                },
            }
        },
    )

    with pytest.raises(MetaTemplateError) as excinfo:
        await meta_client.submit_template(
            waba_id="WABA123",
            name="bad_template",
            language="en_US",
            category="MARKETING",
            components=[{"type": "BODY", "text": "Hello {{1}}"}],
            access_token="EAA-tenant-token",
        )

    err = excinfo.value
    assert err.status_code == 400
    assert err.details is not None
    assert "no example provided" in err.details


@pytest.mark.asyncio
async def test_delete_template_happy_returns_none(
    meta_client: MetaWhatsAppClient,
    stub_httpx_client,  # noqa: ARG001
    meta_route_map: dict,
) -> None:
    meta_route_map[("DELETE", "WABA123/message_templates")] = MetaResponse(
        status_code=200,
        json_body={"success": True},
    )

    result = await meta_client.delete_template(
        waba_id="WABA123",
        name="phase6_smoke",
        access_token="EAA-tenant-token",
    )

    assert result is None


@pytest.mark.asyncio
async def test_delete_template_idempotent_on_404(
    meta_client: MetaWhatsAppClient,
    stub_httpx_client,  # noqa: ARG001
    meta_route_map: dict,
) -> None:
    """Meta returns 404 when the template name has never existed —
    same outward result as "already deleted". Treat as success so the
    caller doesn't have to special-case both 200 and 404."""
    meta_route_map[("DELETE", "WABA123/message_templates")] = MetaResponse(
        status_code=404,
        json_body={
            "error": {
                "message": "Template not found",
                "code": 100,
                "type": "GraphMethodException",
            }
        },
    )

    result = await meta_client.delete_template(
        waba_id="WABA123",
        name="never_existed",
        access_token="EAA-tenant-token",
    )

    assert result is None


@pytest.mark.asyncio
async def test_submit_without_token_refuses(
    meta_client: MetaWhatsAppClient,
) -> None:
    """submit_template ALWAYS requires a per-tenant token — there is
    no demo-token fallback because templates submit under the tenant's
    own WABA + access_token. Refusing is the safer default."""
    with pytest.raises(RuntimeError, match="access_token"):
        await meta_client.submit_template(
            waba_id="WABA123",
            name="x",
            language="en_US",
            category="UTILITY",
            components=[{"type": "BODY", "text": "Hi"}],
            access_token="",
        )


# ---------------------------------------------------------------------------
# create_from_library (Meta Template Library — instant-approved)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_from_library_returns_id_and_status(
    meta_client: MetaWhatsAppClient,
    stub_httpx_client,  # noqa: ARG001
    meta_route_map: dict,
) -> None:
    """Happy path: returns (id, status) and sends library_template_name
    (NOT components — Meta owns the content)."""
    meta_route_map[("POST", "WABA123/message_templates")] = MetaResponse(
        status_code=200,
        json_body={"id": "TPL_LIB_1", "status": "APPROVED", "category": "UTILITY"},
    )

    template_id, status = await meta_client.create_from_library(
        waba_id="WABA123",
        name="appointment_confirmation",
        language="ms_MY",
        category="UTILITY",
        library_template_name="appointment_confirmation",
        access_token="EAA-tenant-token",
    )

    assert template_id == "TPL_LIB_1"
    assert status == "APPROVED"
    sent = stub_httpx_client.calls[-1].json
    assert sent["library_template_name"] == "appointment_confirmation"
    assert sent["name"] == "appointment_confirmation"
    assert sent["language"] == "ms_MY"
    assert "components" not in sent  # library content is Meta-owned


@pytest.mark.asyncio
async def test_create_from_library_defaults_status_when_absent(
    meta_client: MetaWhatsAppClient,
    stub_httpx_client,  # noqa: ARG001
    meta_route_map: dict,
) -> None:
    meta_route_map[("POST", "WABA123/message_templates")] = MetaResponse(
        status_code=200,
        json_body={"id": "TPL_LIB_2"},  # no status field
    )

    _, status = await meta_client.create_from_library(
        waba_id="WABA123",
        name="appointment_reminder",
        language="en",
        category="UTILITY",
        library_template_name="appointment_reminder",
        access_token="EAA-tenant-token",
    )

    assert status == "APPROVED"


@pytest.mark.asyncio
async def test_create_from_library_includes_button_inputs(
    meta_client: MetaWhatsAppClient,
    stub_httpx_client,  # noqa: ARG001
    meta_route_map: dict,
) -> None:
    meta_route_map[("POST", "WABA123/message_templates")] = MetaResponse(
        status_code=200,
        json_body={"id": "TPL_LIB_3", "status": "APPROVED"},
    )
    buttons = [{"type": "URL", "url": {"base_url": "https://x.test"}}]

    await meta_client.create_from_library(
        waba_id="WABA123",
        name="appointment_confirmation",
        language="en",
        category="UTILITY",
        library_template_name="appointment_confirmation",
        access_token="EAA-tenant-token",
        button_inputs=buttons,
    )

    sent = stub_httpx_client.calls[-1].json
    assert sent["library_template_button_inputs"] == buttons


@pytest.mark.asyncio
async def test_create_from_library_4xx_raises_template_error(
    meta_client: MetaWhatsAppClient,
    stub_httpx_client,  # noqa: ARG001
    meta_route_map: dict,
) -> None:
    meta_route_map[("POST", "WABA123/message_templates")] = MetaResponse(
        status_code=400,
        json_body={
            "error": {
                "message": "Unknown library template",
                "code": 100,
                "error_subcode": 2388043,
                "error_data": {"details": "library_template_name not recognized"},
            }
        },
    )

    with pytest.raises(MetaTemplateError) as excinfo:
        await meta_client.create_from_library(
            waba_id="WABA123",
            name="bogus",
            language="en",
            category="UTILITY",
            library_template_name="does_not_exist",
            access_token="EAA-tenant-token",
        )
    assert excinfo.value.status_code == 400


@pytest.mark.asyncio
async def test_create_from_library_without_token_refuses(
    meta_client: MetaWhatsAppClient,
) -> None:
    with pytest.raises(RuntimeError, match="access_token"):
        await meta_client.create_from_library(
            waba_id="WABA123",
            name="x",
            language="en",
            category="UTILITY",
            library_template_name="appointment_confirmation",
            access_token="",
        )
