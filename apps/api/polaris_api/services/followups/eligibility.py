"""Pure follow-up eligibility predicates — Phase 13 follow-ups PR-1.

No I/O — the caller gathers the signals (from the DB) and these decide.
Two gates:

  * ``should_schedule_followup`` — at the end of the inbound pipeline:
    is this conversation a candidate for a follow-up job?
  * ``should_send_followup`` — when the worker claims a due job: is it
    STILL valid (defends the cancel-on-inbound race + the 24h window)?

"Customer stopped replying" = the last message in the conversation is the
bot's (``last_message_role == "assistant"``). A newer customer inbound
would have cancelled the pending job; the send-gate's supersede check is
defence-in-depth against the race where one slips through.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

_FREE_WINDOW = timedelta(hours=24)


def is_followup_enabled(settings: Any) -> bool:
    """Read ``settings.followup_config.enabled`` defensively (asyncpg may
    hand the JSONB column back as a str or a dict). Shared by the
    scheduler (schedule gate) and the worker (send gate)."""
    if isinstance(settings, str):
        try:
            settings = json.loads(settings)
        except json.JSONDecodeError:
            return False
    if not isinstance(settings, dict):
        return False
    cfg = settings.get("followup_config")
    return bool(cfg.get("enabled")) if isinstance(cfg, dict) else False


@dataclass(frozen=True, slots=True)
class ScheduleSignals:
    """Inputs for the schedule-time gate (inbound pipeline)."""

    enabled: bool
    channel: str
    handoff_at: datetime | None
    last_inbound_at: datetime | None
    last_message_role: str | None  # last message in the conversation
    opted_out: bool


def should_schedule_followup(s: ScheduleSignals) -> bool:
    """Schedule a follow-up only for an enabled, WhatsApp, non-handed-off
    conversation where the bot replied last (customer went quiet) and the
    customer hasn't opted out."""
    return (
        s.enabled
        and s.channel == "whatsapp"
        and s.handoff_at is None
        and s.last_inbound_at is not None
        and s.last_message_role == "assistant"
        and not s.opted_out
    )


@dataclass(frozen=True, slots=True)
class SendSignals:
    """Inputs for the send-time gate (worker claim)."""

    enabled: bool
    channel: str
    handoff_at: datetime | None
    latest_inbound_at: datetime | None  # current MAX(role='user') for the conversation
    anchor_inbound_at: datetime  # the inbound the job was scheduled from
    opted_out: bool
    now: datetime
    attempts: int
    max_attempts: int


def should_send_followup(s: SendSignals) -> bool:
    """Re-verify at send time. Skips if superseded (a newer inbound than
    the job's anchor), outside the 24h free window, handed off, disabled,
    opted out, or out of attempts."""
    # Superseded: a customer inbound arrived after the job was scheduled.
    if s.latest_inbound_at is None or s.latest_inbound_at > s.anchor_inbound_at:
        return False
    # Free window closed (would fail Meta 131026 anyway).
    if s.now - s.anchor_inbound_at >= _FREE_WINDOW:
        return False
    return (
        s.enabled
        and s.channel == "whatsapp"
        and s.handoff_at is None
        and not s.opted_out
        and s.attempts < s.max_attempts
    )
