"""Messenger Profile push tests (Phase 7 PR-5) — TDD-RED stage.

Written BEFORE services/messenger_profile.py exists. Initial run must
fail with ModuleNotFoundError to confirm RED per the local TDD workflow.

Targets Meta Graph API v25.0 ``/v25.0/me/messenger_profile`` contract
verified 2026-05 against developers.facebook.com:
  - POST is merge-only (only properties in body get overwritten)
  - DELETE with ``fields=`` query string clears specific properties
  - GET reads current Meta-side state
  - URL uses ``me`` (resolved via page access token), not ``{page_id}``

Coverage per Phase 7 PR-5 checklist:
  - push_profile happy: full bundle POSTed to /v25.0/me/messenger_profile
  - re-GET diff captured when Meta returns 200 but state didn't fully take
  - Meta 401 → last_push_error stamped (token revoked classifier)
  - Meta 400 → structural error
  - Meta 5xx → transient error
  - delete_profile_fields hits DELETE with comma-separated fields
  - get_profile reads back Meta state
  - Channel with missing page access token → config error
  - write-back stamps messenger_profile_last_pushed_at on success
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from polaris_api.services.messenger_profile import (
    MessengerProfileConfigError,
    MessengerProfileStructuralError,
    MessengerProfileTransientError,
    delete_profile_fields,
    get_profile,
    push_profile,
)

_BASE = "https://graph.facebook.com/v25.0"


@pytest.fixture(autouse=True)
def _pin_meta_version() -> Any:
    """Pin META_GRAPH_API_VERSION to v25.0 for these tests regardless of
    the operator's .env (which may still hold a pre-PR-5 v21.0 value).
    Mirrors the ``configured_settings`` pattern in
    test_e2e_webhook_to_messages.py — tests own their config invariants.
    """
    from polaris_api.config import get_settings

    get_settings.cache_clear()
    s = get_settings()
    prev = s.meta_graph_api_version
    s.meta_graph_api_version = "v25.0"
    try:
        yield
    finally:
        s.meta_graph_api_version = prev
        get_settings.cache_clear()


@pytest.fixture
def fake_conn() -> Any:
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=None)
    conn.execute = AsyncMock(return_value=None)
    return conn


@pytest.fixture
def fake_secrets_get(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Stub services.secrets.get to return a non-placeholder page token."""
    import polaris_api.services.secrets as secrets_mod

    async def _get(conn: Any, tenant_id: Any, kind: str) -> bytes:
        return b"EAA-page-access-token"

    monkeypatch.setattr(secrets_mod, "get", _get)


@pytest.fixture
def fake_http(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Patch httpx.AsyncClient inside services.messenger_profile.

    Mirror the _Httpx-shim pattern from tests/chatwoot/conftest.py.
    """
    captured: dict[str, Any] = {"calls": [], "responses": []}

    class _StubClient:
        async def __aenter__(self) -> _StubClient:
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def request(
            self,
            method: str,
            url: str,
            *,
            json: dict[str, Any] | None = None,
            headers: dict[str, str] | None = None,
            params: dict[str, str] | None = None,
        ) -> httpx.Response:
            captured["calls"].append(
                {
                    "method": method,
                    "url": url,
                    "json": json,
                    "headers": headers or {},
                    "params": params or {},
                }
            )
            if not captured["responses"]:
                raise AssertionError(
                    f"stub: no response programmed for {method} {url}"
                )
            programmed = captured["responses"].pop(0)
            if isinstance(programmed, BaseException):
                raise programmed
            return programmed

    class _HttpxShim:
        AsyncClient = staticmethod(lambda *a, **kw: _StubClient())
        Response = httpx.Response
        TimeoutException = httpx.TimeoutException
        ConnectError = httpx.ConnectError

    import polaris_api.services.messenger_profile as mp_mod

    monkeypatch.setattr(mp_mod, "httpx", _HttpxShim())
    return captured


def _channel_row(
    *,
    fb_page_id: str | None = "PAGE_123",
    ice_breakers: list | None = None,
    persistent_menu: list | None = None,
    get_started: dict | None = None,
) -> dict[str, Any]:
    return {
        "fb_page_id": fb_page_id,
        "messenger_icebreakers": ice_breakers if ice_breakers is not None else [
            {"question": "What are your hours?", "payload": "icebreaker:hours"}
        ],
        "messenger_persistent_menu": persistent_menu
        if persistent_menu is not None
        else [],
        "messenger_get_started": get_started,
    }


# =====================================================================
# push_profile
# =====================================================================


@pytest.mark.asyncio
async def test_push_profile_happy_full_bundle(
    fake_conn: Any, fake_http: Any, fake_secrets_get: Any
) -> None:
    fake_conn.fetchrow.return_value = _channel_row()
    # POST 200 + GET re-read 200 with matching state.
    fake_http["responses"] = [
        httpx.Response(200, json={"result": "success"}),
        httpx.Response(
            200,
            json={
                "data": [
                    {
                        "ice_breakers": [
                            {
                                "question": "What are your hours?",
                                "payload": "icebreaker:hours",
                            }
                        ],
                        "persistent_menu": [],
                    }
                ]
            },
        ),
    ]

    result = await push_profile(
        tenant_id=uuid4(),
        tenant_channel_id=uuid4(),
        conn=fake_conn,
    )

    assert result.success is True
    assert "ice_breakers" in result.fields_pushed
    # POST request should hit /v25.0/me/messenger_profile.
    post_call = fake_http["calls"][0]
    assert post_call["method"] == "POST"
    assert post_call["url"].endswith("/v25.0/me/messenger_profile")
    # Body carries the full bundle (Q-PHASE7-06).
    assert "ice_breakers" in post_call["json"]
    # Auth via access_token query param OR Bearer header — accept either.
    auth_in_param = "access_token" in post_call["params"]
    auth_in_header = "Authorization" in post_call["headers"]
    assert auth_in_param or auth_in_header
    # Last-pushed-at write-back happened.
    update_calls = [
        c for c in fake_conn.execute.await_args_list
        if "messenger_profile_last_pushed_at" in str(c.args[0])
    ]
    assert len(update_calls) == 1


@pytest.mark.asyncio
async def test_push_profile_meta_401_token_revoked(
    fake_conn: Any, fake_http: Any, fake_secrets_get: Any
) -> None:
    fake_conn.fetchrow.return_value = _channel_row()
    fake_http["responses"] = [
        httpx.Response(
            401,
            json={
                "error": {
                    "message": "Error validating access token",
                    "code": 190,
                }
            },
        ),
    ]

    with pytest.raises(MessengerProfileStructuralError) as exc:
        await push_profile(
            tenant_id=uuid4(),
            tenant_channel_id=uuid4(),
            conn=fake_conn,
        )
    assert exc.value.status_code == 401
    # last_push_error stamped with classified marker.
    update_calls = [
        c for c in fake_conn.execute.await_args_list
        if "messenger_profile_last_push_error" in str(c.args[0])
    ]
    assert len(update_calls) == 1


@pytest.mark.asyncio
async def test_push_profile_meta_400_invalid_payload(
    fake_conn: Any, fake_http: Any, fake_secrets_get: Any
) -> None:
    fake_conn.fetchrow.return_value = _channel_row()
    fake_http["responses"] = [
        httpx.Response(
            400,
            json={
                "error": {
                    "message": "(#100) Param ice_breakers must contain valid objects",
                    "code": 100,
                }
            },
        ),
    ]

    with pytest.raises(MessengerProfileStructuralError):
        await push_profile(
            tenant_id=uuid4(),
            tenant_channel_id=uuid4(),
            conn=fake_conn,
        )


@pytest.mark.asyncio
async def test_push_profile_meta_5xx_classified_transient(
    fake_conn: Any, fake_http: Any, fake_secrets_get: Any
) -> None:
    fake_conn.fetchrow.return_value = _channel_row()
    fake_http["responses"] = [
        httpx.Response(503, text="Service Unavailable"),
    ]

    with pytest.raises(MessengerProfileTransientError) as exc:
        await push_profile(
            tenant_id=uuid4(),
            tenant_channel_id=uuid4(),
            conn=fake_conn,
        )
    assert exc.value.status_code == 503


@pytest.mark.asyncio
async def test_push_profile_timeout_classified_transient(
    fake_conn: Any, fake_http: Any, fake_secrets_get: Any
) -> None:
    fake_conn.fetchrow.return_value = _channel_row()
    fake_http["responses"] = [httpx.TimeoutException("read timeout")]

    with pytest.raises(MessengerProfileTransientError):
        await push_profile(
            tenant_id=uuid4(),
            tenant_channel_id=uuid4(),
            conn=fake_conn,
        )


@pytest.mark.asyncio
async def test_push_profile_partial_success_diff_captured(
    fake_conn: Any, fake_http: Any, fake_secrets_get: Any
) -> None:
    """Meta returns 200 from POST but the re-GET shows our send didn't
    fully apply (e.g. silently rejected one icebreaker for an emoji
    locale rule). Surface diff to the portal via push_error."""
    fake_conn.fetchrow.return_value = _channel_row(
        ice_breakers=[
            {"question": "A", "payload": "icebreaker:a"},
            {"question": "B", "payload": "icebreaker:b"},
        ]
    )
    fake_http["responses"] = [
        httpx.Response(200, json={"result": "success"}),
        httpx.Response(
            200,
            json={
                "data": [
                    {
                        "ice_breakers": [
                            {"question": "A", "payload": "icebreaker:a"}
                            # Second icebreaker silently dropped Meta-side.
                        ],
                        "persistent_menu": [],
                    }
                ]
            },
        ),
    ]

    result = await push_profile(
        tenant_id=uuid4(),
        tenant_channel_id=uuid4(),
        conn=fake_conn,
    )

    assert result.success is True  # Meta said 200
    assert result.diff_against_remote is not None
    assert "ice_breakers" in result.diff_against_remote


@pytest.mark.asyncio
async def test_push_profile_channel_not_found_config_error(
    fake_conn: Any, fake_http: Any, fake_secrets_get: Any
) -> None:
    fake_conn.fetchrow.return_value = None  # No tenant_channels row

    with pytest.raises(MessengerProfileConfigError):
        await push_profile(
            tenant_id=uuid4(),
            tenant_channel_id=uuid4(),
            conn=fake_conn,
        )


@pytest.mark.asyncio
async def test_push_profile_channel_missing_page_id_config_error(
    fake_conn: Any, fake_http: Any, fake_secrets_get: Any
) -> None:
    fake_conn.fetchrow.return_value = _channel_row(fb_page_id=None)

    with pytest.raises(MessengerProfileConfigError):
        await push_profile(
            tenant_id=uuid4(),
            tenant_channel_id=uuid4(),
            conn=fake_conn,
        )


@pytest.mark.asyncio
async def test_push_profile_token_missing_config_error(
    fake_conn: Any,
    fake_http: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Channel exists, fb_page_id set, but tenant_secrets has no token row."""
    import polaris_api.services.secrets as secrets_mod

    async def _get_none(conn: Any, tenant_id: Any, kind: str) -> None:
        return None

    monkeypatch.setattr(secrets_mod, "get", _get_none)
    fake_conn.fetchrow.return_value = _channel_row()

    with pytest.raises(MessengerProfileConfigError):
        await push_profile(
            tenant_id=uuid4(),
            tenant_channel_id=uuid4(),
            conn=fake_conn,
        )


# =====================================================================
# delete_profile_fields
# =====================================================================


@pytest.mark.asyncio
async def test_delete_profile_fields_sends_field_query(
    fake_conn: Any, fake_http: Any, fake_secrets_get: Any
) -> None:
    fake_conn.fetchrow.return_value = _channel_row()
    fake_http["responses"] = [httpx.Response(200, json={"result": "success"})]

    await delete_profile_fields(
        tenant_id=uuid4(),
        tenant_channel_id=uuid4(),
        fields=["ice_breakers", "persistent_menu"],
        conn=fake_conn,
    )

    call = fake_http["calls"][0]
    assert call["method"] == "DELETE"
    assert call["url"].endswith("/v25.0/me/messenger_profile")
    # fields query param carries comma-separated list.
    assert call["params"].get("fields") == "ice_breakers,persistent_menu"


# =====================================================================
# get_profile
# =====================================================================


@pytest.mark.asyncio
async def test_get_profile_reads_meta_state(
    fake_conn: Any, fake_http: Any, fake_secrets_get: Any
) -> None:
    fake_conn.fetchrow.return_value = _channel_row()
    fake_http["responses"] = [
        httpx.Response(
            200,
            json={
                "data": [
                    {
                        "ice_breakers": [{"question": "Q1", "payload": "p1"}],
                        "persistent_menu": [],
                    }
                ]
            },
        ),
    ]

    state = await get_profile(
        tenant_id=uuid4(),
        tenant_channel_id=uuid4(),
        fields=["ice_breakers", "persistent_menu"],
        conn=fake_conn,
    )

    assert "ice_breakers" in state
    assert state["ice_breakers"][0]["question"] == "Q1"
    call = fake_http["calls"][0]
    assert call["method"] == "GET"
    assert call["url"].endswith("/v25.0/me/messenger_profile")
    assert call["params"].get("fields") == "ice_breakers,persistent_menu"
