"""ChatwootClient tests (Phase 7 PR-2).

Covers the 6 public verbs + retry budget + error split + config gate.
"""

from __future__ import annotations

import httpx
import pytest
from polaris_api.services.chatwoot.client import (
    ChatwootClient,
    ChatwootConfigError,
    ChatwootStructuralError,
    ChatwootTransientError,
)

from tests.chatwoot.conftest import CWRegister, CWResponse, StubChatwootClient

# =====================================================================
# Config / fail-closed
# =====================================================================


@pytest.mark.asyncio
async def test_placeholder_token_raises_config_error(
    stub_chatwoot_httpx: StubChatwootClient,
) -> None:
    client = ChatwootClient(
        base_url="https://chat.getpolaris.my",
        bot_access_token="placeholder-chatwoot-bot-token",
        timeout_s=1.0,
        max_retries=0,
        label_auto_provision=False,
    )
    with pytest.raises(ChatwootConfigError):
        await client.flip_conversation_status(account_id=1, conversation_id=42)


@pytest.mark.asyncio
async def test_empty_base_url_raises_config_error(
    stub_chatwoot_httpx: StubChatwootClient,
) -> None:
    client = ChatwootClient(
        base_url="",
        bot_access_token="real-token",
        timeout_s=1.0,
        max_retries=0,
        label_auto_provision=False,
    )
    with pytest.raises(ChatwootConfigError):
        await client.flip_conversation_status(account_id=1, conversation_id=42)


# =====================================================================
# Happy paths — one per verb
# =====================================================================


@pytest.mark.asyncio
async def test_flip_conversation_status_happy(
    chatwoot_client: ChatwootClient,
    stub_chatwoot_httpx: StubChatwootClient,
    cw_register: CWRegister,
) -> None:
    cw_register(
        "POST",
        "api/v1/accounts/7/conversations/42/toggle_status",
        CWResponse(status_code=200, json_body={"id": 42, "status": "open"}),
    )
    await chatwoot_client.flip_conversation_status(
        account_id=7, conversation_id=42
    )
    assert len(stub_chatwoot_httpx.calls) == 1
    call = stub_chatwoot_httpx.calls[0]
    assert call.method == "POST"
    assert call.path == "api/v1/accounts/7/conversations/42/toggle_status"
    assert call.json == {"status": "open"}
    assert call.headers["api_access_token"] == "test-token-not-placeholder"


@pytest.mark.asyncio
async def test_assign_team_happy(
    chatwoot_client: ChatwootClient,
    stub_chatwoot_httpx: StubChatwootClient,
    cw_register: CWRegister,
) -> None:
    cw_register(
        "POST",
        "api/v1/accounts/7/conversations/42/assignments",
        CWResponse(status_code=200, json_body={"team_id": 3}),
    )
    await chatwoot_client.assign_team(
        account_id=7, conversation_id=42, team_id=3
    )
    assert stub_chatwoot_httpx.calls[0].json == {"team_id": 3}


@pytest.mark.asyncio
async def test_post_private_note_returns_id(
    chatwoot_client: ChatwootClient,
    stub_chatwoot_httpx: StubChatwootClient,
    cw_register: CWRegister,
) -> None:
    cw_register(
        "POST",
        "api/v1/accounts/7/conversations/42/messages",
        CWResponse(
            status_code=200,
            json_body={"id": 999, "content": "AI summary: refund Q"},
        ),
    )
    msg_id = await chatwoot_client.post_private_note(
        account_id=7, conversation_id=42, content="AI summary: refund Q"
    )
    assert msg_id == 999
    call = stub_chatwoot_httpx.calls[0]
    assert call.json is not None
    assert call.json["private"] is True
    assert call.json["message_type"] == "outgoing"


@pytest.mark.asyncio
async def test_apply_contact_label_happy_with_ensure(
    chatwoot_client: ChatwootClient,
    stub_chatwoot_httpx: StubChatwootClient,
    cw_register: CWRegister,
) -> None:
    # ensure_label discovery — label already exists
    cw_register(
        "GET",
        "api/v1/accounts/7/labels",
        CWResponse(
            status_code=200,
            json_body={"payload": [{"id": 1, "title": "pol_opted_out"}]},
        ),
    )
    cw_register(
        "POST",
        "api/v1/accounts/7/contacts/123/labels",
        CWResponse(status_code=200, json_body={"payload": ["pol_opted_out"]}),
    )
    await chatwoot_client.apply_contact_label(
        account_id=7, contact_id=123, label="pol_opted_out"
    )
    # Two calls: list_labels, then apply
    paths = [c.path for c in stub_chatwoot_httpx.calls]
    assert paths == [
        "api/v1/accounts/7/labels",
        "api/v1/accounts/7/contacts/123/labels",
    ]


# =====================================================================
# Retry budget — 5xx, then 5xx, then 200
# =====================================================================


@pytest.mark.asyncio
async def test_5xx_retries_then_succeeds(
    chatwoot_client: ChatwootClient,
    stub_chatwoot_httpx: StubChatwootClient,
    cw_register: CWRegister,
) -> None:
    # max_retries=2 means: 1 initial + 2 retries = 3 attempts.
    cw_register(
        "POST",
        "api/v1/accounts/7/conversations/42/toggle_status",
        CWResponse(status_code=502, text_body="bad gateway"),
        CWResponse(status_code=503, text_body="upstream timeout"),
        CWResponse(status_code=200, json_body={"id": 42, "status": "open"}),
    )
    await chatwoot_client.flip_conversation_status(
        account_id=7, conversation_id=42
    )
    assert len(stub_chatwoot_httpx.calls) == 3


@pytest.mark.asyncio
async def test_5xx_exhausts_retry_budget_raises_transient(
    chatwoot_client: ChatwootClient,
    stub_chatwoot_httpx: StubChatwootClient,
    cw_register: CWRegister,
) -> None:
    cw_register(
        "POST",
        "api/v1/accounts/7/conversations/42/toggle_status",
        CWResponse(status_code=500, text_body="boom"),
        CWResponse(status_code=502, text_body="boom"),
        CWResponse(status_code=503, text_body="boom"),
    )
    with pytest.raises(ChatwootTransientError):
        await chatwoot_client.flip_conversation_status(
            account_id=7, conversation_id=42
        )
    assert len(stub_chatwoot_httpx.calls) == 3


@pytest.mark.asyncio
async def test_timeout_classified_as_transient_after_budget(
    chatwoot_client: ChatwootClient,
    stub_chatwoot_httpx: StubChatwootClient,
    cw_register: CWRegister,
) -> None:
    cw_register(
        "POST",
        "api/v1/accounts/7/conversations/42/toggle_status",
        CWResponse(raise_exc=httpx.TimeoutException("read timeout")),
        CWResponse(raise_exc=httpx.TimeoutException("read timeout")),
        CWResponse(raise_exc=httpx.ConnectError("conn reset")),
    )
    with pytest.raises(ChatwootTransientError):
        await chatwoot_client.flip_conversation_status(
            account_id=7, conversation_id=42
        )
    assert len(stub_chatwoot_httpx.calls) == 3


# =====================================================================
# Structural — 4xx, no retry
# =====================================================================


@pytest.mark.asyncio
async def test_4xx_raises_structural_no_retry(
    chatwoot_client: ChatwootClient,
    stub_chatwoot_httpx: StubChatwootClient,
    cw_register: CWRegister,
) -> None:
    cw_register(
        "POST",
        "api/v1/accounts/7/conversations/42/assignments",
        CWResponse(
            status_code=404,
            json_body={"message": "Team not found in account"},
        ),
    )
    with pytest.raises(ChatwootStructuralError) as excinfo:
        await chatwoot_client.assign_team(
            account_id=7, conversation_id=42, team_id=999
        )
    # No retry — exactly 1 attempt.
    assert len(stub_chatwoot_httpx.calls) == 1
    assert excinfo.value.status_code == 404
    assert "Team not found" in str(excinfo.value)


@pytest.mark.asyncio
async def test_4xx_error_envelope_fallback_to_default_message(
    chatwoot_client: ChatwootClient,
    stub_chatwoot_httpx: StubChatwootClient,
    cw_register: CWRegister,
) -> None:
    cw_register(
        "POST",
        "api/v1/accounts/7/conversations/42/messages",
        CWResponse(status_code=403, text_body="<html>forbidden</html>"),
    )
    with pytest.raises(ChatwootStructuralError) as excinfo:
        await chatwoot_client.post_private_note(
            account_id=7, conversation_id=42, content="note"
        )
    # HTML body — _parse_chatwoot_error falls back to default.
    assert "private_note failed: HTTP 403" in str(excinfo.value)


# =====================================================================
# ensure_label
# =====================================================================


@pytest.mark.asyncio
async def test_get_contact_details_happy(
    chatwoot_client: ChatwootClient,
    stub_chatwoot_httpx: StubChatwootClient,
    cw_register: CWRegister,
) -> None:
    """meta.sender is mapped to name/email/phone (phone_number → phone)."""
    cw_register(
        "GET",
        "api/v1/accounts/7/conversations/42",
        CWResponse(
            status_code=200,
            json_body={
                "id": 42,
                "meta": {
                    "sender": {
                        "name": "John Tan",
                        "email": "john@example.com",
                        "phone_number": "+60123456789",
                    }
                },
            },
        ),
    )
    details = await chatwoot_client.get_contact_details(
        account_id=7, conversation_id=42
    )
    assert details == {
        "name": "John Tan",
        "email": "john@example.com",
        "phone": "+60123456789",
    }


@pytest.mark.asyncio
async def test_get_contact_details_partial_sender(
    chatwoot_client: ChatwootClient,
    stub_chatwoot_httpx: StubChatwootClient,
    cw_register: CWRegister,
) -> None:
    """Only non-empty fields are returned (a blank email is dropped)."""
    cw_register(
        "GET",
        "api/v1/accounts/7/conversations/42",
        CWResponse(
            status_code=200,
            json_body={
                "meta": {"sender": {"name": "Jane", "email": "", "phone_number": None}}
            },
        ),
    )
    details = await chatwoot_client.get_contact_details(
        account_id=7, conversation_id=42
    )
    assert details == {"name": "Jane"}


@pytest.mark.asyncio
async def test_get_contact_details_4xx_returns_empty(
    chatwoot_client: ChatwootClient,
    stub_chatwoot_httpx: StubChatwootClient,
    cw_register: CWRegister,
) -> None:
    """A 4xx (e.g. conversation gone) is swallowed → {} (prefill is best-effort)."""
    cw_register(
        "GET",
        "api/v1/accounts/7/conversations/42",
        CWResponse(status_code=404, json_body={"error": "not found"}),
    )
    details = await chatwoot_client.get_contact_details(
        account_id=7, conversation_id=42
    )
    assert details == {}


@pytest.mark.asyncio
async def test_ensure_label_already_exists_returns_false(
    chatwoot_client: ChatwootClient,
    stub_chatwoot_httpx: StubChatwootClient,
    cw_register: CWRegister,
) -> None:
    cw_register(
        "GET",
        "api/v1/accounts/7/labels",
        CWResponse(
            status_code=200,
            json_body={"payload": [{"id": 1, "title": "pol_opted_out"}]},
        ),
    )
    created = await chatwoot_client.ensure_label(
        account_id=7, label="pol_opted_out"
    )
    assert created is False
    # Only one call — discovery only, no POST.
    assert len(stub_chatwoot_httpx.calls) == 1


@pytest.mark.asyncio
async def test_ensure_label_creates_when_missing(
    chatwoot_client: ChatwootClient,
    stub_chatwoot_httpx: StubChatwootClient,
    cw_register: CWRegister,
) -> None:
    cw_register(
        "GET",
        "api/v1/accounts/7/labels",
        CWResponse(status_code=200, json_body={"payload": []}),
    )
    cw_register(
        "POST",
        "api/v1/accounts/7/labels",
        CWResponse(status_code=200, json_body={"id": 99, "title": "pol_opted_out"}),
    )
    created = await chatwoot_client.ensure_label(
        account_id=7, label="pol_opted_out"
    )
    assert created is True
    paths = [c.path for c in stub_chatwoot_httpx.calls]
    assert paths == [
        "api/v1/accounts/7/labels",
        "api/v1/accounts/7/labels",
    ]
    # Verify create payload carries title + color + sidebar flag.
    create_call = stub_chatwoot_httpx.calls[1]
    assert create_call.json is not None
    assert create_call.json["title"] == "pol_opted_out"
    assert "color" in create_call.json
    assert create_call.json["show_on_sidebar"] is True


@pytest.mark.asyncio
async def test_ensure_label_422_race_treated_as_success(
    chatwoot_client: ChatwootClient,
    stub_chatwoot_httpx: StubChatwootClient,
    cw_register: CWRegister,
) -> None:
    cw_register(
        "GET",
        "api/v1/accounts/7/labels",
        CWResponse(status_code=200, json_body={"payload": []}),
    )
    cw_register(
        "POST",
        "api/v1/accounts/7/labels",
        CWResponse(
            status_code=422,
            json_body={"message": "Title has already been taken"},
        ),
    )
    created = await chatwoot_client.ensure_label(
        account_id=7, label="pol_opted_out"
    )
    # Race lost — but the label exists either way, so we return success-as-false.
    assert created is False


@pytest.mark.asyncio
async def test_ensure_label_422_non_race_raises_structural(
    chatwoot_client: ChatwootClient,
    stub_chatwoot_httpx: StubChatwootClient,
    cw_register: CWRegister,
) -> None:
    # 422 with a non-race message (e.g. label name violates a Chatwoot
    # validation rule) must NOT be silently swallowed — N-3 security nit.
    cw_register(
        "GET",
        "api/v1/accounts/7/labels",
        CWResponse(status_code=200, json_body={"payload": []}),
    )
    cw_register(
        "POST",
        "api/v1/accounts/7/labels",
        CWResponse(
            status_code=422,
            json_body={"message": "Title is too short (minimum 2 characters)"},
        ),
    )
    with pytest.raises(ChatwootStructuralError) as excinfo:
        await chatwoot_client.ensure_label(account_id=7, label="x")
    assert excinfo.value.status_code == 422


@pytest.mark.asyncio
async def test_ensure_label_skipped_when_auto_provision_disabled(
    stub_chatwoot_httpx: StubChatwootClient,
) -> None:
    client = ChatwootClient(
        base_url="https://chat.getpolaris.my",
        bot_access_token="test-token-not-placeholder",
        # ensure_label() runs _check_user_config() before the
        # auto-provision short-circuit, so a valid user token is needed
        # to reach the `if not self.label_auto_provision: return False`
        # branch this test asserts on.
        agent_api_token="test-user-token-not-placeholder",
        timeout_s=1.0,
        max_retries=0,
        label_auto_provision=False,
    )
    created = await client.ensure_label(account_id=7, label="pol_opted_out")
    assert created is False
    # No HTTP calls — short-circuit.
    assert stub_chatwoot_httpx.calls == []


# =====================================================================
# ensure_contact — identifier path (IG / Messenger / Telegram)
# =====================================================================


@pytest.mark.asyncio
async def test_ensure_contact_requires_exactly_one_key(
    chatwoot_client: ChatwootClient,
    stub_chatwoot_httpx: StubChatwootClient,
) -> None:
    # Neither key.
    with pytest.raises(ValueError):
        await chatwoot_client.ensure_contact(account_id=7)
    # Both keys.
    with pytest.raises(ValueError):
        await chatwoot_client.ensure_contact(
            account_id=7, phone_number="+60123", identifier="instagram:1"
        )
    assert stub_chatwoot_httpx.calls == []


@pytest.mark.asyncio
async def test_ensure_contact_identifier_search_hit_returns_id(
    chatwoot_client: ChatwootClient,
    stub_chatwoot_httpx: StubChatwootClient,
    cw_register: CWRegister,
) -> None:
    cw_register(
        "POST",
        "api/v1/accounts/7/contacts/filter",
        CWResponse(
            status_code=200,
            json_body={
                "payload": [
                    {"id": 555, "identifier": "instagram:psid-1", "name": "Jane"}
                ]
            },
        ),
    )
    cid = await chatwoot_client.ensure_contact(
        account_id=7, identifier="instagram:psid-1", name="Jane"
    )
    assert cid == 555
    # Filter lookup only — no contact-create POST when the identifier exists.
    assert [c.path for c in stub_chatwoot_httpx.calls] == [
        "api/v1/accounts/7/contacts/filter"
    ]
    # Exact-match filter body, not a fuzzy search.
    filter_call = stub_chatwoot_httpx.calls[0]
    assert filter_call.json == {
        "payload": [
            {
                "attribute_key": "identifier",
                "filter_operator": "equal_to",
                "values": ["instagram:psid-1"],
                "query_operator": None,
            }
        ]
    }


@pytest.mark.asyncio
async def test_ensure_contact_identifier_search_miss_creates_no_phone(
    chatwoot_client: ChatwootClient,
    stub_chatwoot_httpx: StubChatwootClient,
    cw_register: CWRegister,
) -> None:
    cw_register(
        "POST",
        "api/v1/accounts/7/contacts/filter",
        CWResponse(status_code=200, json_body={"payload": []}),
    )
    cw_register(
        "POST",
        "api/v1/accounts/7/contacts",
        CWResponse(
            status_code=200,
            json_body={"payload": {"contact": {"id": 777}}},
        ),
    )
    cid = await chatwoot_client.ensure_contact(
        account_id=7, identifier="messenger:psid-2", name="Bob"
    )
    assert cid == 777
    create_call = stub_chatwoot_httpx.calls[1]
    assert create_call.json == {"identifier": "messenger:psid-2", "name": "Bob"}
    # Critical: no phone_number key — that is what triggered the 422.
    assert "phone_number" not in (create_call.json or {})


@pytest.mark.asyncio
async def test_ensure_contact_identifier_422_race_research(
    chatwoot_client: ChatwootClient,
    stub_chatwoot_httpx: StubChatwootClient,
    cw_register: CWRegister,
) -> None:
    # filter miss → POST 422 (unique race) → re-filter hit.
    cw_register(
        "POST",
        "api/v1/accounts/7/contacts/filter",
        CWResponse(status_code=200, json_body={"payload": []}),
        CWResponse(
            status_code=200,
            json_body={
                "payload": [{"id": 888, "identifier": "telegram:chat-9"}]
            },
        ),
    )
    cw_register(
        "POST",
        "api/v1/accounts/7/contacts",
        CWResponse(
            status_code=422,
            json_body={"message": "Identifier has already been taken"},
        ),
    )
    cid = await chatwoot_client.ensure_contact(
        account_id=7, identifier="telegram:chat-9"
    )
    assert cid == 888
    paths = [(c.method, c.path) for c in stub_chatwoot_httpx.calls]
    assert paths == [
        ("POST", "api/v1/accounts/7/contacts/filter"),
        ("POST", "api/v1/accounts/7/contacts"),
        ("POST", "api/v1/accounts/7/contacts/filter"),
    ]
    # The create POST sent identifier with NO phone_number key (that is what
    # triggered the 422 race in the first place).
    create_call = stub_chatwoot_httpx.calls[1]
    assert create_call.json == {"identifier": "telegram:chat-9"}
    assert "phone_number" not in (create_call.json or {})


@pytest.mark.asyncio
async def test_ensure_contact_phone_path_unchanged(
    chatwoot_client: ChatwootClient,
    stub_chatwoot_httpx: StubChatwootClient,
    cw_register: CWRegister,
) -> None:
    # WhatsApp path still searches + creates with phone_number (E.164).
    cw_register(
        "GET",
        "api/v1/accounts/7/contacts/search",
        CWResponse(status_code=200, json_body={"payload": []}),
    )
    cw_register(
        "POST",
        "api/v1/accounts/7/contacts",
        CWResponse(
            status_code=200, json_body={"payload": {"contact": {"id": 111}}}
        ),
    )
    cid = await chatwoot_client.ensure_contact(
        account_id=7, phone_number="60123456789", name="Wa"
    )
    assert cid == 111
    create_call = stub_chatwoot_httpx.calls[1]
    assert create_call.json == {"phone_number": "+60123456789", "name": "Wa"}


# =====================================================================
# create_conversation — source_id
# =====================================================================


@pytest.mark.asyncio
async def test_create_conversation_includes_source_id(
    chatwoot_client: ChatwootClient,
    stub_chatwoot_httpx: StubChatwootClient,
    cw_register: CWRegister,
) -> None:
    cw_register(
        "POST",
        "api/v1/accounts/7/conversations",
        CWResponse(status_code=200, json_body={"id": 4242}),
    )
    conv_id = await chatwoot_client.create_conversation(
        account_id=7, inbox_id=3, contact_id=111, source_id="psid-1"
    )
    assert conv_id == 4242
    assert stub_chatwoot_httpx.calls[0].json == {
        "inbox_id": 3,
        "contact_id": 111,
        "source_id": "psid-1",
    }


@pytest.mark.asyncio
async def test_create_conversation_omits_source_id_when_none(
    chatwoot_client: ChatwootClient,
    stub_chatwoot_httpx: StubChatwootClient,
    cw_register: CWRegister,
) -> None:
    cw_register(
        "POST",
        "api/v1/accounts/7/conversations",
        CWResponse(status_code=200, json_body={"id": 4243}),
    )
    await chatwoot_client.create_conversation(
        account_id=7, inbox_id=3, contact_id=111
    )
    assert stub_chatwoot_httpx.calls[0].json == {"inbox_id": 3, "contact_id": 111}


# =====================================================================
# Cross-tenant URL scoping
# =====================================================================


@pytest.mark.asyncio
async def test_account_id_baked_into_url_per_call(
    chatwoot_client: ChatwootClient,
    stub_chatwoot_httpx: StubChatwootClient,
    cw_register: CWRegister,
) -> None:
    # Same client, two different tenants — URLs must carry distinct account_ids.
    cw_register(
        "POST",
        "api/v1/accounts/7/conversations/42/toggle_status",
        CWResponse(status_code=200, json_body={"id": 42}),
    )
    cw_register(
        "POST",
        "api/v1/accounts/99/conversations/42/toggle_status",
        CWResponse(status_code=200, json_body={"id": 42}),
    )
    await chatwoot_client.flip_conversation_status(
        account_id=7, conversation_id=42
    )
    await chatwoot_client.flip_conversation_status(
        account_id=99, conversation_id=42
    )
    paths = [c.path for c in stub_chatwoot_httpx.calls]
    assert "api/v1/accounts/7/conversations/42/toggle_status" in paths


# =====================================================================
# post_outbound_message — source_id dedup fix
# =====================================================================


@pytest.mark.asyncio
async def test_post_outbound_message_with_source_id(
    chatwoot_client: ChatwootClient,
    stub_chatwoot_httpx: StubChatwootClient,
    cw_register: CWRegister,
) -> None:
    """source_id is included in the POST body when provided.

    Chatwoot's send_on_channel_service skips re-delivering the message when
    source_id is present, preventing the double-send bug on Meta channels.
    """
    cw_register(
        "POST",
        "api/v1/accounts/7/conversations/42/messages",
        CWResponse(status_code=200, json_body={"id": 1, "content": "hello"}),
    )
    await chatwoot_client.post_outbound_message(
        account_id=7,
        conversation_id=42,
        content="hello",
        source_id="m_abc123",
    )
    call = stub_chatwoot_httpx.calls[0]
    assert call.json is not None
    assert call.json["source_id"] == "m_abc123"
    assert call.json["content"] == "hello"
    assert call.json["private"] is False
    assert call.json["message_type"] == "outgoing"


@pytest.mark.asyncio
async def test_post_outbound_message_without_source_id_omits_key(
    chatwoot_client: ChatwootClient,
    stub_chatwoot_httpx: StubChatwootClient,
    cw_register: CWRegister,
) -> None:
    """source_id is absent from the POST body when not supplied (default None)."""
    cw_register(
        "POST",
        "api/v1/accounts/7/conversations/42/messages",
        CWResponse(status_code=200, json_body={"id": 2, "content": "hi"}),
    )
    await chatwoot_client.post_outbound_message(
        account_id=7,
        conversation_id=42,
        content="hi",
    )
    call = stub_chatwoot_httpx.calls[0]
    assert call.json is not None
    assert "source_id" not in call.json


@pytest.mark.asyncio
async def test_post_outbound_message_explicit_none_omits_key(
    chatwoot_client: ChatwootClient,
    stub_chatwoot_httpx: StubChatwootClient,
    cw_register: CWRegister,
) -> None:
    """Passing source_id=None explicitly also omits the key (send failed/skipped)."""
    cw_register(
        "POST",
        "api/v1/accounts/7/conversations/42/messages",
        CWResponse(status_code=200, json_body={"id": 3, "content": "hi"}),
    )
    await chatwoot_client.post_outbound_message(
        account_id=7,
        conversation_id=42,
        content="hi",
        source_id=None,
    )
    call = stub_chatwoot_httpx.calls[0]
    assert call.json is not None
    assert "source_id" not in call.json
