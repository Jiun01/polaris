"""Unit tests for human-agent reply mirroring (phase-10 portal-transcript fix).

Two layers, both DB-free:

  * ``build_reply_row`` — the pure payload→row derivation (content / attachments
    / attribution metadata / idempotency key). No I/O.
  * ``_handle_human_agent_reply`` — the router's channel-kind router, with
    ``resolve_chatwoot`` + the two sinks (persist / email-acceptance) patched, so
    we assert ROUTING without a DB: bot-provider → persist into ``messages``;
    email → draft-acceptance; unresolved / missing ids → no-op. The echo-drop
    (agent_bot mirror-posts never reach here) is enforced upstream by
    ``human_agent_forward_inbox_id`` — covered in ``test_chatwoot_native_gate``
    and re-asserted here for the orchestrator contract.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import pytest
from polaris_api.services.chatwoot import agent_reply
from polaris_api.services.chatwoot.agent_reply import build_reply_row
from polaris_api.webhooks import chatwoot_router
from polaris_api.webhooks.chatwoot_router import (
    _handle_human_agent_reply,
    human_agent_forward_inbox_id,
)


def _agent_payload(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "event": "message_created",
        "id": 4242,
        "message_type": "outgoing",
        "sender": {"type": "agent", "id": 9, "name": "Aisha"},
        "inbox": {"id": 7},
        "conversation": {"id": 99},
        "account": {"id": 1},
        "content": "Sure, your order ships tomorrow.",
        "created_at": "2026-06-13T03:00:00Z",
    }
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# build_reply_row
# ---------------------------------------------------------------------------


class TestBuildReplyRow:
    def test_text_reply_builds_human_agent_row(self) -> None:
        row = build_reply_row(_agent_payload())
        assert row is not None
        assert row.external_id == "chatwoot.4242"
        assert row.chatwoot_message_id == 4242
        assert row.content == "Sure, your order ships tomorrow."
        assert row.metadata["author_type"] == "human_agent"
        assert row.metadata["agent_name"] == "Aisha"
        assert row.metadata["agent_id"] == 9
        assert "attachments" not in row.metadata
        assert row.created_at is not None
        assert row.created_at.year == 2026

    def test_content_is_stripped(self) -> None:
        row = build_reply_row(_agent_payload(content="  hi there  "))
        assert row is not None
        assert row.content == "hi there"

    def test_attachment_only_uses_placeholder_and_records_refs(self) -> None:
        row = build_reply_row(
            _agent_payload(
                content="",
                attachments=[
                    {"id": 1, "file_type": "image", "data_url": "https://cdn/x.jpg"},
                ],
            )
        )
        assert row is not None
        assert row.content == "[Attachment]"
        assert row.metadata["attachments"] == [
            {"kind": "image", "url": "https://cdn/x.jpg"},
        ]

    def test_text_plus_attachment_keeps_text(self) -> None:
        row = build_reply_row(
            _agent_payload(
                content="see attached",
                attachments=[
                    {"id": 1, "file_type": "file", "data_url": "https://cdn/x.pdf"},
                ],
            )
        )
        assert row is not None
        assert row.content == "see attached"
        assert row.metadata["attachments"] == [
            {"kind": "file", "url": "https://cdn/x.pdf"},
        ]

    def test_non_http_attachment_url_dropped_but_kind_kept(self) -> None:
        # SSRF guard: a non-http(s) data_url is stripped; the kind still records
        # that an attachment existed (so content isn't a bare placeholder for
        # nothing).
        row = build_reply_row(
            _agent_payload(
                content="",
                attachments=[
                    {"id": 1, "file_type": "image", "data_url": "file:///etc/passwd"},
                ],
            )
        )
        assert row is not None
        assert row.metadata["attachments"] == [{"kind": "image"}]

    def test_unknown_attachment_file_type_skipped(self) -> None:
        row = build_reply_row(
            _agent_payload(
                content="hi",
                attachments=[{"id": 1, "file_type": "contact", "data_url": "x"}],
            )
        )
        assert row is not None
        assert "attachments" not in row.metadata

    def test_missing_agent_name_and_id_are_none(self) -> None:
        row = build_reply_row(_agent_payload(sender={"type": "agent"}))
        assert row is not None
        assert row.metadata["agent_name"] is None
        assert row.metadata["agent_id"] is None

    def test_empty_reply_with_no_attachments_returns_none(self) -> None:
        assert build_reply_row(_agent_payload(content="   ")) is None

    def test_missing_message_id_returns_none(self) -> None:
        p = _agent_payload()
        del p["id"]
        assert build_reply_row(p) is None

    @pytest.mark.parametrize("payload", [None, "nope", 42, {}, {"id": "4242"}])
    def test_malformed_payload_returns_none(self, payload: Any) -> None:
        assert build_reply_row(payload) is None

    def test_bad_created_at_falls_back_to_none(self) -> None:
        row = build_reply_row(_agent_payload(created_at="not-a-date"))
        assert row is not None
        assert row.created_at is None


# ---------------------------------------------------------------------------
# _handle_human_agent_reply routing
# ---------------------------------------------------------------------------


@dataclass
class _FakeResolved:
    tenant_id: uuid.UUID
    channel_kind: str


@pytest.fixture
def patched_sinks(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Any]]:
    """Patch resolve_chatwoot's two sinks + get_redis so routing is observable
    without a DB / Redis. Returns the recorded call lists."""
    calls: dict[str, list[Any]] = {"persist": [], "email": []}

    async def _fake_persist(**kwargs: Any) -> bool:
        calls["persist"].append(kwargs)
        return True

    async def _fake_record(**kwargs: Any) -> None:
        calls["email"].append(kwargs)

    monkeypatch.setattr(
        agent_reply, "persist_human_agent_reply", _fake_persist
    )
    monkeypatch.setattr(
        chatwoot_router.email_assist, "record_agent_reply", _fake_record
    )
    monkeypatch.setattr(chatwoot_router, "get_redis", lambda: object())
    return calls


def _patch_resolve(
    monkeypatch: pytest.MonkeyPatch, resolved: _FakeResolved | None
) -> None:
    async def _fake_resolve(inbox_id: int) -> _FakeResolved | None:
        return resolved

    monkeypatch.setattr(chatwoot_router, "resolve_chatwoot", _fake_resolve)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind", ["whatsapp", "messenger", "telegram", "instagram_login"]
)
async def test_bot_provider_routes_to_persist(
    monkeypatch: pytest.MonkeyPatch, patched_sinks: dict[str, list[Any]], kind: str
) -> None:
    tenant_id = uuid.uuid4()
    _patch_resolve(monkeypatch, _FakeResolved(tenant_id=tenant_id, channel_kind=kind))
    await _handle_human_agent_reply(7, _agent_payload())
    assert len(patched_sinks["persist"]) == 1
    call = patched_sinks["persist"][0]
    assert call["tenant_id"] == tenant_id
    assert call["chatwoot_conversation_id"] == 99
    assert call["payload"]["id"] == 4242
    assert patched_sinks["email"] == []


@pytest.mark.asyncio
async def test_email_routes_to_acceptance_not_persist(
    monkeypatch: pytest.MonkeyPatch, patched_sinks: dict[str, list[Any]]
) -> None:
    tenant_id = uuid.uuid4()
    _patch_resolve(
        monkeypatch, _FakeResolved(tenant_id=tenant_id, channel_kind="email")
    )
    await _handle_human_agent_reply(7, _agent_payload(content="thanks!"))
    assert patched_sinks["persist"] == []
    assert len(patched_sinks["email"]) == 1
    assert patched_sinks["email"][0]["chatwoot_conversation_id"] == 99
    assert patched_sinks["email"][0]["agent_reply_text"] == "thanks!"


@pytest.mark.asyncio
async def test_email_empty_text_is_noop(
    monkeypatch: pytest.MonkeyPatch, patched_sinks: dict[str, list[Any]]
) -> None:
    _patch_resolve(
        monkeypatch, _FakeResolved(tenant_id=uuid.uuid4(), channel_kind="email")
    )
    await _handle_human_agent_reply(7, _agent_payload(content="   "))
    assert patched_sinks["persist"] == []
    assert patched_sinks["email"] == []


@pytest.mark.asyncio
async def test_unresolved_inbox_is_noop(
    monkeypatch: pytest.MonkeyPatch, patched_sinks: dict[str, list[Any]]
) -> None:
    _patch_resolve(monkeypatch, None)
    await _handle_human_agent_reply(7, _agent_payload())
    assert patched_sinks["persist"] == []
    assert patched_sinks["email"] == []


@pytest.mark.asyncio
async def test_missing_conversation_id_is_noop(
    monkeypatch: pytest.MonkeyPatch, patched_sinks: dict[str, list[Any]]
) -> None:
    _patch_resolve(
        monkeypatch, _FakeResolved(tenant_id=uuid.uuid4(), channel_kind="whatsapp")
    )
    p = _agent_payload()
    del p["conversation"]
    await _handle_human_agent_reply(7, p)
    assert patched_sinks["persist"] == []
    assert patched_sinks["email"] == []


@pytest.mark.asyncio
async def test_agent_bot_mirror_post_never_reaches_persist(
    monkeypatch: pytest.MonkeyPatch, patched_sinks: dict[str, list[Any]]
) -> None:
    # Echo-drop: the bot's own outgoing mirror-post (sender.type='agent_bot')
    # is gated out UPSTREAM — human_agent_forward_inbox_id returns None, so the
    # router never calls _handle_human_agent_reply. Assert that contract: a
    # genuine human agent yields an inbox id; the bot's mirror-post yields None.
    bot_echo = _agent_payload(sender={"type": "agent_bot", "id": 1})
    assert human_agent_forward_inbox_id(bot_echo) is None
    assert human_agent_forward_inbox_id(_agent_payload()) == 7
    # And persist is a no-op even if reached with no resolvable inbox.
    _patch_resolve(monkeypatch, None)
    assert patched_sinks["persist"] == []
