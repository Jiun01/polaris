"""Unit tests for email_assist pure helpers — email channel M2.

Cost guard, acceptance classification, and payload→job construction need no DB
or live fork, so these run everywhere. The DB-backed orchestration
(``generate_and_post_draft`` / ``record_agent_reply``) is exercised by the
e2e suites that gate on ``POLARIS_DATABASE_URL``.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import Any

import pytest
from polaris_api.services import email_assist
from polaris_api.services.email_assist import EmailAssistJob


def _job(
    *,
    sender_email: str | None = "alice@example.com",
    body: str = "Do you ship to Penang?",
    signals: dict[str, object] | None = None,
) -> EmailAssistJob:
    return EmailAssistJob(
        tenant_id=uuid.uuid4(),
        channel_id=uuid.uuid4(),
        chatwoot_account_id=1,
        chatwoot_conversation_id=2,
        sender_email=sender_email,
        body=body,
        signals=signals or {},
    )


@pytest.mark.parametrize(
    "sender",
    [
        "no-reply@shop.com",
        "noreply@x.io",
        "mailer-daemon@y.com",
        "postmaster@z.org",
        "bounces@list.com",
        "notifications@app.com",
    ],
)
def test_cost_guard_skips_non_customer_sender(sender: str) -> None:
    reason = email_assist.should_skip_draft(_job(sender_email=sender))
    assert reason is not None
    assert reason.startswith("non_customer_sender:")


def test_cost_guard_skips_bulk_header() -> None:
    reason = email_assist.should_skip_draft(
        _job(signals={"List-Unsubscribe": "<mailto:u@x.com>"})
    )
    assert reason == "bulk_header:list-unsubscribe"


def test_cost_guard_skips_empty_body() -> None:
    assert email_assist.should_skip_draft(_job(body="   ")) == "empty_body"


def test_cost_guard_allows_real_customer() -> None:
    assert email_assist.should_skip_draft(_job()) is None


def test_cost_guard_allows_lookalike_localpart() -> None:
    # Regression: a bare-prefix match would wrongly skip these real people.
    assert email_assist.should_skip_draft(_job(sender_email="bouncer@acme.com")) is None
    assert (
        email_assist.should_skip_draft(_job(sender_email="notificationsmgr@x.com"))
        is None
    )


def test_cost_guard_skips_compound_bounce_address() -> None:
    reason = email_assist.should_skip_draft(_job(sender_email="bounces-12345@list.com"))
    assert reason == "non_customer_sender:bounces"


def test_classify_outcome_accepted() -> None:
    s = "Yes, we ship to Penang within 3 days."
    outcome, ratio = email_assist.classify_outcome(s, s)
    assert outcome == "accepted"
    assert ratio == pytest.approx(1.0)


def test_classify_outcome_ignored() -> None:
    s = "Yes, we ship to Penang within 3 days."
    outcome, _ = email_assist.classify_outcome(s, "zzzzz qqqqq wwwww")
    assert outcome == "ignored"


def test_build_job_from_payload() -> None:
    tid = uuid.uuid4()
    cid = uuid.uuid4()
    payload = {
        "account": {"id": 7},
        "conversation": {"id": 99},
        "content": "Hi, question about my order",
        "sender": {"email": "bob@buyer.com", "name": "Bob"},
        "content_attributes": {"email": {"List-Unsubscribe": "x"}},
    }
    job = email_assist.build_job(payload, tenant_id=tid, channel_id=cid)
    assert job is not None
    assert job.chatwoot_account_id == 7
    assert job.chatwoot_conversation_id == 99
    assert job.sender_email == "bob@buyer.com"
    assert job.body == "Hi, question about my order"
    assert job.signals.get("list-unsubscribe") == "x"


def test_build_job_missing_ids_returns_none() -> None:
    job = email_assist.build_job(
        {"content": "hi"}, tenant_id=uuid.uuid4(), channel_id=None
    )
    assert job is None


def _aret(value: Any) -> Callable[..., Awaitable[Any]]:
    """Build an async stub returning ``value`` regardless of args."""

    async def _f(*_a: Any, **_k: Any) -> Any:
        return value

    return _f


class _FakeConn:
    async def execute(self, *_a: Any, **_k: Any) -> str:
        return "INSERT 0 1"


class _FakeAcquire:
    async def __aenter__(self) -> _FakeConn:
        return _FakeConn()

    async def __aexit__(self, *_a: Any) -> bool:
        return False


class _FakePool:
    def acquire(self) -> _FakeAcquire:
        return _FakeAcquire()


class _FakeChatwoot:
    def __init__(self, *, fail_label: bool = False) -> None:
        self.notes: list[str] = []
        self.labels: list[str] = []
        self.flips: list[str] = []
        self._fail_label = fail_label

    async def post_private_note(
        self, *, account_id: int, conversation_id: int, content: str
    ) -> int:
        self.notes.append(content)
        return 123

    async def apply_conversation_label(
        self, *, account_id: int, conversation_id: int, label: str
    ) -> None:
        if self._fail_label:
            raise RuntimeError("chatwoot label endpoint down")
        self.labels.append(label)

    # Stage-3 handling-label sync (read-modify-write).
    async def get_conversation_labels(
        self, *, account_id: int, conversation_id: int
    ) -> list[str]:
        return list(self.labels)

    async def set_conversation_labels(
        self, *, account_id: int, conversation_id: int, labels: list[str]
    ) -> None:
        if self._fail_label:
            raise RuntimeError("chatwoot label endpoint down")
        self.labels = list(labels)

    async def ensure_label(self, *, account_id: int, label: str) -> bool:
        return True

    async def flip_conversation_status(
        self, *, account_id: int, conversation_id: int, status: str = "open"
    ) -> None:
        self.flips.append(status)


def _patch_assist_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the RAG+LLM+quota chain so generate_and_post_draft runs offline.

    Patch by import-path string (not ``email_assist.<mod>``) so we hit the real
    module objects without tripping mypy's no-implicit-reexport rule."""
    monkeypatch.setattr("polaris_api.services.embed.embed_query", _aret([0.0]))
    monkeypatch.setattr("polaris_api.services.rag.search_hybrid", _aret([]))
    monkeypatch.setattr(
        "polaris_api.services.tenant_snapshot.load_tenant_snapshot", _aret({})
    )
    monkeypatch.setattr(
        "polaris_api.services.quota.check_quota",
        _aret(SimpleNamespace(over_cap=False)),
    )
    monkeypatch.setattr("polaris_api.services.quota.increment_counter", _aret(1))
    monkeypatch.setattr(
        "polaris_api.services.llm.generate_response",
        _aret(SimpleNamespace(content="draft reply")),
    )


@pytest.mark.asyncio
async def test_generate_and_post_draft_posts_note_and_tags_human_handling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A drafted email posts the private note AND tags the conversation
    `human-handling` (email is human-first — drafted, then handed to a human)."""
    _patch_assist_chain(monkeypatch)
    chatwoot: Any = _FakeChatwoot()
    pool: Any = _FakePool()
    redis_stub: Any = object()

    result = await email_assist.generate_and_post_draft(
        _job(), pool=pool, redis=redis_stub, chatwoot=chatwoot
    )

    assert result == "posted"
    assert chatwoot.notes == ["draft reply"]
    assert chatwoot.labels == ["human-handling"]
    # Routed to a human: flipped pending -> open so it surfaces in agents' inbox
    # (Chatwoot auto-assignment then round-robins it to an agent).
    assert chatwoot.flips == ["open"]


@pytest.mark.asyncio
async def test_generate_and_post_draft_skips_non_customer() -> None:
    """Cost guard short-circuits before any LLM / Chatwoot work."""
    chatwoot: Any = _FakeChatwoot()
    pool: Any = _FakePool()
    redis_stub: Any = object()

    result = await email_assist.generate_and_post_draft(
        _job(sender_email="no-reply@shop.com"),
        pool=pool,
        redis=redis_stub,
        chatwoot=chatwoot,
    )

    assert result.startswith("skipped:")
    assert chatwoot.notes == []
    assert chatwoot.labels == []


@pytest.mark.asyncio
async def test_generate_and_post_draft_label_failure_still_posts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A label failure is best-effort: the note is still posted and the draft
    flow completes ('posted' is returned only after the draft is recorded)."""
    _patch_assist_chain(monkeypatch)
    chatwoot: Any = _FakeChatwoot(fail_label=True)
    pool: Any = _FakePool()
    redis_stub: Any = object()

    result = await email_assist.generate_and_post_draft(
        _job(), pool=pool, redis=redis_stub, chatwoot=chatwoot
    )

    assert result == "posted"
    assert chatwoot.notes == ["draft reply"]
    assert chatwoot.labels == []
