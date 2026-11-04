"""Agent-bot client verbs + auto-attach helper (Chatwoot-native rehaul Stage 3).

Uses the real ``ChatwootClient`` against the stubbed httpx surface so the tests
exercise the actual request shapes + the ``_check_4xx`` error split.
"""

from __future__ import annotations

import pytest
from polaris_api.services.chatwoot.agent_bot import (
    attach_inbox_agent_bot,
    resolve_platform_bot_id,
)
from polaris_api.services.chatwoot.client import ChatwootClient

from tests.chatwoot.conftest import CWRegister, CWResponse, StubChatwootClient

# =====================================================================
# Client verbs
# =====================================================================


@pytest.mark.asyncio
async def test_list_agent_bots_happy(
    chatwoot_client: ChatwootClient,
    stub_chatwoot_httpx: StubChatwootClient,
    cw_register: CWRegister,
) -> None:
    cw_register(
        "GET",
        "api/v1/accounts/7/agent_bots",
        CWResponse(status_code=200, json_body=[{"id": 3, "name": "Polaris Bot"}]),
    )
    bots = await chatwoot_client.list_agent_bots(account_id=7)
    assert bots == [{"id": 3, "name": "Polaris Bot"}]


@pytest.mark.asyncio
async def test_list_agent_bots_tolerates_payload_wrap(
    chatwoot_client: ChatwootClient,
    stub_chatwoot_httpx: StubChatwootClient,
    cw_register: CWRegister,
) -> None:
    cw_register(
        "GET",
        "api/v1/accounts/7/agent_bots",
        CWResponse(
            status_code=200,
            json_body={"payload": [{"id": 3, "name": "Polaris Bot"}]},
        ),
    )
    bots = await chatwoot_client.list_agent_bots(account_id=7)
    assert bots == [{"id": 3, "name": "Polaris Bot"}]


@pytest.mark.asyncio
async def test_create_agent_bot_returns_id(
    chatwoot_client: ChatwootClient,
    stub_chatwoot_httpx: StubChatwootClient,
    cw_register: CWRegister,
) -> None:
    cw_register(
        "POST",
        "api/v1/accounts/7/agent_bots",
        CWResponse(status_code=200, json_body={"id": 9, "name": "Polaris Bot"}),
    )
    bot_id = await chatwoot_client.create_agent_bot(
        account_id=7,
        name="Polaris Bot",
        outgoing_url="https://api.example/v1/webhooks/chatwoot",
    )
    assert bot_id == 9
    assert stub_chatwoot_httpx.calls[0].json == {
        "name": "Polaris Bot",
        "outgoing_url": "https://api.example/v1/webhooks/chatwoot",
    }


@pytest.mark.asyncio
async def test_set_inbox_agent_bot_attach(
    chatwoot_client: ChatwootClient,
    stub_chatwoot_httpx: StubChatwootClient,
    cw_register: CWRegister,
) -> None:
    cw_register(
        "POST",
        "api/v1/accounts/7/inboxes/2/set_agent_bot",
        CWResponse(status_code=200, text_body=""),
    )
    await chatwoot_client.set_inbox_agent_bot(
        account_id=7, inbox_id=2, agent_bot_id=3
    )
    assert stub_chatwoot_httpx.calls[0].json == {"agent_bot": 3}


@pytest.mark.asyncio
async def test_set_inbox_agent_bot_detach_sends_null(
    chatwoot_client: ChatwootClient,
    stub_chatwoot_httpx: StubChatwootClient,
    cw_register: CWRegister,
) -> None:
    cw_register(
        "POST",
        "api/v1/accounts/7/inboxes/2/set_agent_bot",
        CWResponse(status_code=200, text_body=""),
    )
    await chatwoot_client.set_inbox_agent_bot(
        account_id=7, inbox_id=2, agent_bot_id=None
    )
    assert stub_chatwoot_httpx.calls[0].json == {"agent_bot": None}


# =====================================================================
# Attach helper
# =====================================================================


@pytest.mark.asyncio
async def test_resolve_platform_bot_id_by_name(
    chatwoot_client: ChatwootClient,
    stub_chatwoot_httpx: StubChatwootClient,
    cw_register: CWRegister,
) -> None:
    cw_register(
        "GET",
        "api/v1/accounts/2/agent_bots",
        CWResponse(
            status_code=200,
            json_body=[
                {"id": 5, "name": "Other"},
                {"id": 3, "name": "Polaris Bot"},
            ],
        ),
    )
    assert await resolve_platform_bot_id(chatwoot_client, account_id=2) == 3


@pytest.mark.asyncio
async def test_attach_happy_links_bot(
    chatwoot_client: ChatwootClient,
    stub_chatwoot_httpx: StubChatwootClient,
    cw_register: CWRegister,
) -> None:
    cw_register(
        "GET",
        "api/v1/accounts/2/agent_bots",
        CWResponse(status_code=200, json_body=[{"id": 3, "name": "Polaris Bot"}]),
    )
    cw_register(
        "POST",
        "api/v1/accounts/2/inboxes/9/set_agent_bot",
        CWResponse(status_code=200, text_body=""),
    )
    ok = await attach_inbox_agent_bot(chatwoot_client, account_id=2, inbox_id=9)
    assert ok is True
    assert [(c.method, c.path) for c in stub_chatwoot_httpx.calls] == [
        ("GET", "api/v1/accounts/2/agent_bots"),
        ("POST", "api/v1/accounts/2/inboxes/9/set_agent_bot"),
    ]


@pytest.mark.asyncio
async def test_attach_false_when_bot_missing(
    chatwoot_client: ChatwootClient,
    stub_chatwoot_httpx: StubChatwootClient,
    cw_register: CWRegister,
) -> None:
    cw_register(
        "GET",
        "api/v1/accounts/2/agent_bots",
        CWResponse(status_code=200, json_body=[]),
    )
    ok = await attach_inbox_agent_bot(chatwoot_client, account_id=2, inbox_id=9)
    assert ok is False
    # No attach attempted when the bot can't be resolved.
    assert all(
        c.path != "api/v1/accounts/2/inboxes/9/set_agent_bot"
        for c in stub_chatwoot_httpx.calls
    )


@pytest.mark.asyncio
async def test_attach_is_nonfatal_on_4xx(
    chatwoot_client: ChatwootClient,
    stub_chatwoot_httpx: StubChatwootClient,
    cw_register: CWRegister,
) -> None:
    cw_register(
        "GET",
        "api/v1/accounts/2/agent_bots",
        CWResponse(status_code=200, json_body=[{"id": 3, "name": "Polaris Bot"}]),
    )
    cw_register(
        "POST",
        "api/v1/accounts/2/inboxes/9/set_agent_bot",
        CWResponse(status_code=422, json_body={"error": "nope"}),
    )
    # set_agent_bot 422 -> ChatwootStructuralError (a ChatwootError subclass) ->
    # caught by `except ChatwootError` -> returns False, never raises.
    ok = await attach_inbox_agent_bot(chatwoot_client, account_id=2, inbox_id=9)
    assert ok is False


@pytest.mark.asyncio
async def test_attach_nonfatal_on_missing_agent_token() -> None:
    # CHATWOOT_AGENT_API_TOKEN unset -> _check_user_config raises
    # ChatwootConfigError before any HTTP -> caught + returns False (non-fatal,
    # but logged at ERROR so it's distinguishable from a transient outage).
    client = ChatwootClient(
        base_url="https://chat.getpolaris.my",
        bot_access_token="test-token-not-placeholder",
        agent_api_token="",
        timeout_s=1.0,
        max_retries=0,
        label_auto_provision=False,
    )
    ok = await attach_inbox_agent_bot(client, account_id=2, inbox_id=9)
    assert ok is False
