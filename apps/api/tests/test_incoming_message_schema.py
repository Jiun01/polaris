"""Phase 2 PR-4 — IncomingMessage Pydantic SSOT schema tests.

DB-free. Pins required-field shape + rejects out-of-bound inputs so a
future refactor of ``polaris_shared_types.incoming_message`` can't
silently break consumers.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from polaris_shared_types import Attachment, IncomingMessage, SenderRef
from pydantic import ValidationError


def _base_kwargs() -> dict[str, object]:
    return {
        "tenant_id": uuid4(),
        "channel_id": uuid4(),
        "provider": "whatsapp",
        "provider_message_id": f"wamid.{uuid4().hex[:8]}",
        "sender": SenderRef(channel_user_id="60123456789"),
        "body": "hello",
        "received_at": datetime.now(UTC),
    }


def test_minimal_roundtrip() -> None:
    msg = IncomingMessage(**_base_kwargs())  # type: ignore[arg-type]
    dumped = msg.model_dump()
    rehydrated = IncomingMessage.model_validate(dumped)
    assert rehydrated == msg


def test_attachments_default_to_empty_list() -> None:
    msg = IncomingMessage(**_base_kwargs())  # type: ignore[arg-type]
    assert msg.attachments == []


def test_body_can_be_none_for_media_only_messages() -> None:
    kwargs = _base_kwargs()
    kwargs["body"] = None
    kwargs["attachments"] = [
        Attachment(kind="image", media_id="img.123", mime_type="image/jpeg")
    ]
    msg = IncomingMessage(**kwargs)  # type: ignore[arg-type]
    assert msg.body is None
    assert len(msg.attachments) == 1
    assert msg.attachments[0].kind == "image"


def test_rejects_empty_provider_message_id() -> None:
    kwargs = _base_kwargs()
    kwargs["provider_message_id"] = ""
    with pytest.raises(ValidationError):
        IncomingMessage(**kwargs)  # type: ignore[arg-type]


def test_rejects_body_over_10k_chars() -> None:
    kwargs = _base_kwargs()
    kwargs["body"] = "x" * 10_001
    with pytest.raises(ValidationError):
        IncomingMessage(**kwargs)  # type: ignore[arg-type]


def test_rejects_invalid_provider() -> None:
    kwargs = _base_kwargs()
    # 'telegram' joined the Provider literal in off-Meta channels M1 (migration
    # 094); use a still-unsupported provider to assert the literal rejects.
    kwargs["provider"] = "discord"
    with pytest.raises(ValidationError):
        IncomingMessage(**kwargs)  # type: ignore[arg-type]


def test_sender_requires_channel_user_id() -> None:
    with pytest.raises(ValidationError):
        SenderRef(channel_user_id="")


def test_sender_display_name_optional() -> None:
    s = SenderRef(channel_user_id="60123456789")
    assert s.display_name is None


def test_attachment_kind_constrained() -> None:
    with pytest.raises(ValidationError):
        Attachment(kind="weirdtype")  # type: ignore[arg-type]


def test_provider_sent_at_optional() -> None:
    """``received_at`` is required (server-side); ``provider_sent_at``
    is optional because some Meta event shapes (status updates) lack
    a timestamp at the message level."""
    msg = IncomingMessage(**_base_kwargs())  # type: ignore[arg-type]
    assert msg.provider_sent_at is None


def test_in_reply_to_optional() -> None:
    """PR-7: in_reply_to defaults to None; common case is a new message."""
    msg = IncomingMessage(**_base_kwargs())  # type: ignore[arg-type]
    assert msg.in_reply_to is None


def test_in_reply_to_roundtrips() -> None:
    kwargs = _base_kwargs()
    kwargs["in_reply_to"] = "wamid.original"
    msg = IncomingMessage(**kwargs)  # type: ignore[arg-type]
    dumped = msg.model_dump()
    assert dumped["in_reply_to"] == "wamid.original"
    rehydrated = IncomingMessage.model_validate(dumped)
    assert rehydrated.in_reply_to == "wamid.original"


def test_in_reply_to_over_200_chars_rejected() -> None:
    kwargs = _base_kwargs()
    kwargs["in_reply_to"] = "x" * 201
    with pytest.raises(ValidationError):
        IncomingMessage(**kwargs)  # type: ignore[arg-type]
