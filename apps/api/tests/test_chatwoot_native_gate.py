"""Unit tests for the Chatwoot-native consumer helpers (rehaul Stage 1).

Pure-function tests — no DB / Redis. They cover the human-handling gate
(bot runs only when the Chatwoot conversation is 'pending') and the
real-channel recipient resolution that lets pipeline.run reply via Meta to
the right customer (instead of provider='chatwoot' + the undeliverable
Chatwoot contact id).
"""

from __future__ import annotations

import pytest
from polaris_api.webhooks.chatwoot_router import (
    _should_resume,
    channel_kind_to_provider,
    extract_chatwoot_recipient,
    human_agent_forward_inbox_id,
    is_bot_owned,
)


class TestIsBotOwned:
    def test_pending_is_bot_owned(self) -> None:
        assert is_bot_owned({"status": "pending"}) is True

    @pytest.mark.parametrize("conv_status", ["open", "resolved", "snoozed"])
    def test_non_pending_is_human(self, conv_status: str) -> None:
        assert is_bot_owned({"status": conv_status}) is False

    def test_pending_with_human_assignee_still_bot_owned(self) -> None:
        # Status is the sole gate — a pending conversation can carry a human
        # assignee; status wins (verified against Chatwoot's model).
        assert (
            is_bot_owned(
                {"status": "pending", "meta": {"assignee": {"id": 5, "type": "user"}}}
            )
            is True
        )

    @pytest.mark.parametrize("conv", [None, {}, "nope", {"status": None}, 42])
    def test_missing_or_malformed_is_not_bot_owned(self, conv: object) -> None:
        # Fail safe: never reply over a human when the payload is unclear.
        assert is_bot_owned(conv) is False


class TestExtractRecipient:
    def test_whatsapp_from_contact_inbox_strips_plus(self) -> None:
        conv = {"contact_inbox": {"source_id": "+60123456789"}}
        assert extract_chatwoot_recipient(conv, "whatsapp") == "60123456789"

    def test_whatsapp_wa_id_without_plus(self) -> None:
        conv = {"contact_inbox": {"source_id": "60123456789"}}
        assert extract_chatwoot_recipient(conv, "whatsapp") == "60123456789"

    def test_messenger_psid_passthrough(self) -> None:
        conv = {"contact_inbox": {"source_id": "PSID_98765"}}
        assert extract_chatwoot_recipient(conv, "messenger") == "PSID_98765"

    def test_telegram_chat_id(self) -> None:
        conv = {"contact_inbox": {"source_id": "777001"}}
        assert extract_chatwoot_recipient(conv, "telegram") == "777001"

    def test_falls_back_to_meta_sender_phone_for_whatsapp(self) -> None:
        conv = {"meta": {"sender": {"phone_number": "+60111", "identifier": None}}}
        assert extract_chatwoot_recipient(conv, "whatsapp") == "60111"

    def test_falls_back_to_meta_sender_identifier_strips_channel_prefix(self) -> None:
        conv = {"meta": {"sender": {"identifier": "messenger:PSID_5"}}}
        assert extract_chatwoot_recipient(conv, "messenger") == "PSID_5"

    def test_source_id_preferred_over_meta_sender(self) -> None:
        conv = {
            "contact_inbox": {"source_id": "PRIMARY"},
            "meta": {"sender": {"identifier": "messenger:FALLBACK"}},
        }
        assert extract_chatwoot_recipient(conv, "messenger") == "PRIMARY"

    def test_instagram_login_identifier_prefix_stripped(self) -> None:
        # Longest channel prefix — exercises the len(prefix) slice.
        conv = {"meta": {"sender": {"identifier": "instagram_login:12345"}}}
        assert extract_chatwoot_recipient(conv, "instagram_login") == "12345"

    def test_whitespace_only_source_id_falls_through_to_none(self) -> None:
        conv = {"contact_inbox": {"source_id": "   "}}
        assert extract_chatwoot_recipient(conv, "whatsapp") is None

    @pytest.mark.parametrize(
        "conv",
        [None, {}, {"contact_inbox": {}}, {"contact_inbox": {"source_id": ""}}],
    )
    def test_unresolvable_returns_none(self, conv: object) -> None:
        assert extract_chatwoot_recipient(conv, "whatsapp") is None


class TestChannelKindToProvider:
    @pytest.mark.parametrize(
        "kind", ["whatsapp", "messenger", "telegram", "instagram_login"]
    )
    def test_valid_bot_providers(self, kind: str) -> None:
        assert channel_kind_to_provider(kind) == kind

    # Legacy FB-page 'instagram' was dropped in migration 109 — it is no longer
    # a bot provider and must raise like any other non-bot kind.
    @pytest.mark.parametrize("kind", ["email", "chatwoot", "sms", "instagram"])
    def test_non_bot_kinds_raise(self, kind: str) -> None:
        with pytest.raises(ValueError, match="not a bot provider"):
            channel_kind_to_provider(kind)


class TestShouldResume:
    """Bot-resume signals: status resolved, or the human-handling label removed
    via a conversation_updated event (the fork emits no label_updated event)."""

    def test_status_resolved_resumes(self) -> None:
        assert (
            _should_resume(
                "conversation_status_changed", {"conversation": {"status": "resolved"}}
            )
            is True
        )

    @pytest.mark.parametrize("status", ["open", "pending", "snoozed"])
    def test_status_not_resolved_does_not_resume(self, status: str) -> None:
        assert (
            _should_resume(
                "conversation_status_changed", {"conversation": {"status": status}}
            )
            is False
        )

    def test_human_handling_label_removed_resumes(self) -> None:
        payload = {
            "changed_attributes": [
                {
                    "label_list": {
                        "previous_value": ["human-handling", "vip"],
                        "current_value": ["vip"],
                    }
                }
            ]
        }
        assert _should_resume("conversation_updated", payload) is True

    def test_human_handling_label_added_does_not_resume(self) -> None:
        payload = {
            "changed_attributes": [
                {
                    "label_list": {
                        "previous_value": ["bot-handling"],
                        "current_value": ["bot-handling", "human-handling"],
                    }
                }
            ]
        }
        assert _should_resume("conversation_updated", payload) is False

    def test_other_label_change_keeping_human_handling_does_not_resume(self) -> None:
        payload = {
            "changed_attributes": [
                {
                    "label_list": {
                        "previous_value": ["human-handling"],
                        "current_value": ["human-handling", "vip"],
                    }
                }
            ]
        }
        assert _should_resume("conversation_updated", payload) is False

    def test_non_label_attribute_change_does_not_resume(self) -> None:
        payload = {
            "changed_attributes": [
                {"assignee_id": {"previous_value": None, "current_value": 5}}
            ]
        }
        assert _should_resume("conversation_updated", payload) is False

    @pytest.mark.parametrize("payload", [{}, {"changed_attributes": None}, {"changed_attributes": []}])
    def test_missing_or_empty_changed_attributes_does_not_resume(
        self, payload: dict[str, object]
    ) -> None:
        assert _should_resume("conversation_updated", payload) is False

    def test_unknown_event_does_not_resume(self) -> None:
        assert _should_resume("message_created", {"changed_attributes": []}) is False


class TestHumanAgentForwardEchoDrop:
    """Echo-drop invariant (rehaul Stage 2): Polaris must never re-forward or
    re-process its own outgoing ``source_id`` mirror-posts."""

    def test_bot_own_outgoing_is_echo_dropped(self) -> None:
        # The bot's reply mirror-post: outgoing + sender.type='agent_bot'.
        # MUST resolve to None — no forward, no reprocess. The core invariant.
        payload = {
            "message_type": "outgoing",
            "sender": {"type": "agent_bot", "id": 1},
            "inbox": {"id": 7},
        }
        assert human_agent_forward_inbox_id(payload) is None

    def test_human_agent_outgoing_forwards(self) -> None:
        # Behavior preserved from AC-12-6: a human agent's outgoing reply
        # (sender.type='agent') resolves to its inbox id for forwarding.
        # NOTE: the agent-forward path is slated for retirement in the native
        # rehaul (Chatwoot sends human replies itself); the real human
        # sender.type ('agent' vs 'user') is tracked in the retirement inventory.
        payload = {
            "message_type": "outgoing",
            "sender": {"type": "agent", "id": 9},
            "inbox": {"id": 7},
        }
        assert human_agent_forward_inbox_id(payload) == 7

    def test_private_note_activity_is_dropped(self) -> None:
        assert (
            human_agent_forward_inbox_id(
                {
                    "message_type": "activity",
                    "sender": {"type": "agent_bot"},
                    "inbox": {"id": 7},
                }
            )
            is None
        )

    def test_incoming_returns_none(self) -> None:
        # Incoming is the bot-process path, not the agent-forward path.
        assert (
            human_agent_forward_inbox_id(
                {
                    "message_type": "incoming",
                    "sender": {"type": "contact"},
                    "inbox": {"id": 7},
                }
            )
            is None
        )

    @pytest.mark.parametrize(
        "inbox", [None, {}, {"id": "7"}, {"id": None}, {"id": 0}, {"id": -3}, 42]
    )
    def test_outgoing_agent_without_valid_inbox_id_dropped(self, inbox: object) -> None:
        assert (
            human_agent_forward_inbox_id(
                {"message_type": "outgoing", "sender": {"type": "agent"}, "inbox": inbox}
            )
            is None
        )

    @pytest.mark.parametrize(
        "sender", [None, {}, {"type": None}, {"type": "contact"}, "nope"]
    )
    def test_outgoing_without_human_agent_sender_dropped(self, sender: object) -> None:
        assert (
            human_agent_forward_inbox_id(
                {"message_type": "outgoing", "sender": sender, "inbox": {"id": 7}}
            )
            is None
        )

    @pytest.mark.parametrize("payload", [None, {}, "nope", 42, {"message_type": None}])
    def test_malformed_payload_is_dropped(self, payload: object) -> None:
        # Fail safe: never echo on an unclear payload.
        assert human_agent_forward_inbox_id(payload) is None
