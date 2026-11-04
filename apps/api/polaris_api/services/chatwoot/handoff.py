"""Handoff orchestration — Phase 7 PR-3.

Coordinates a bot-to-human handoff:

  1. Read business-hours config (per-branch override → per-tenant fallback)
     and classify in_hours vs after_hours per ADR-019. Strict cutoff at GA
     per Q-PHASE7-03; ``HANDOFF_BUSINESS_HOURS_GRACE_MIN`` widens the window
     if set.

  2. Select + render the customer-facing handoff message from
     ``tenants.handoff_message_in_hours`` / ``_after_hours`` (with branch
     overrides). Variables substituted: ``{{customer_name}}``,
     ``{{intent_tier}}``, ``{{last_reply}}``.

  3. Sanitise the already-substituted ``private_note`` before posting to
     Chatwoot. Customer-controlled fragments (e.g. ``{{last_reply}}``)
     can carry Markdown injection vectors (``[click](javascript:...)``).
     We replace ``[``/``]``/`` ` `` characters with Markdown-inert Unicode
     variants — the visible text survives; the syntax doesn't.

  4. Fire two outbound paths via ``asyncio.gather``:
        a. Customer-facing message via Meta WhatsApp channel
        b. Chatwoot path: flip status → open, assign team, post private
           note (3 sequential calls — Chatwoot doesn't tolerate parallel
           writes against the same conversation)

  5. Classify the outcome per ADR-051:
        - both succeed                                → CONFIRMED
        - customer succeeded, Chatwoot transient-fail → DECLINED + mark
          ``conversations.handoff_pending_recovery_at`` for next-inbound
          recovery (Q-PHASE7-04)
        - customer succeeded, Chatwoot structural-fail → ERRORED + same
          recovery marker
        - customer failed (any reason)                → ERRORED (no
          recovery marker — customer will message again)

  6. On CONFIRMED only: persist ``handoff.initiated`` event row in
     ``webhook_events_outbound`` per Q-PHASE7-08 (no dispatch worker at
     GA).

This service is invoked by ``ChatwootAdapter.handoff()`` in
``services/agent/ports.py``. The caller maps ``confirmed`` on returned
``HandoffOutcome == "confirmed"`` and ``declined`` otherwise — both
``declined`` and ``errored`` route to the same false-branch behaviour per
ADR-051; the distinction lives in audit, not in the caller's control flow.

Design source: 04h_PHASE_7_DEEP.md §5.2 + §11 (resolved Q-PHASE7-01..08).
"""

from __future__ import annotations

import asyncio
import datetime
import html as _html_module  # noqa: F401 — reserved for future HTML-escape extension
import json
import logging
from datetime import UTC
from typing import Final, Literal
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import asyncpg
from polaris_shared_types.chatwoot_api import HandoffResult

from polaris_api.config import get_settings
from polaris_api.services import audit
from polaris_api.services.chatwoot.business_hours import is_within_schedule
from polaris_api.services.chatwoot.client import (
    ChatwootClient,
    ChatwootStructuralError,
    ChatwootTransientError,
)
from polaris_api.services.chatwoot.label_sync import sync_handling_label
from polaris_api.services.chatwoot.templates import (
    substitute as _shared_substitute,
)
from polaris_api.services.meta_whatsapp import (
    MetaWhatsAppClient,
    MetaWhatsAppError,
)

logger = logging.getLogger(__name__)

# ``tenant_secrets.kind`` under which the per-tenant WhatsApp Business
# API access token lives (Phase 1 schema). Same constant as Phase 6's
# broadcast worker — keep in sync if either side renames.
_WABA_TOKEN_KIND: Final[str] = "meta_waba_access_token"  # noqa: S105

# Recovery reason vocabulary persisted to
# ``conversations.handoff_pending_recovery_reason`` per Q-PHASE7-04.
# Values aligned with HandoffPendingRecoveryReason Literal in shared types.
_RECOVERY_5XX: Final[str] = "chatwoot_5xx_after_retries"
_RECOVERY_4XX: Final[str] = "chatwoot_4xx"
_RECOVERY_TIMEOUT: Final[str] = "chatwoot_timeout"

# Markdown / code-fence inert character map. Used in
# ``_sanitize_for_private_note`` to neutralise injection vectors in
# customer-derived text before forwarding to a Chatwoot private note.
# Visually similar to the originals so the agent's reading experience
# isn't degraded.
_SANITIZE_MAP: Final[dict[int, str]] = str.maketrans(
    {
        "[": "⟦",   # MATHEMATICAL LEFT WHITE SQUARE BRACKET ⟦
        "]": "⟧",   # MATHEMATICAL RIGHT WHITE SQUARE BRACKET ⟧
        "`": "ˋ",   # MODIFIER LETTER GRAVE ACCENT ˋ
    }
)


# =====================================================================
# Pure helpers
# =====================================================================


def is_in_business_hours(
    *,
    business_hours: dict[str, object] | None,
    timezone: str | None,
    grace_minutes: int = 0,
) -> bool:
    """ADR-019 helper. ``True`` iff current tenant-local time is inside
    ``business_hours``.

    Schedule shape (one entry per day-of-week, 0=Monday … 6=Sunday):

        {
          "0": {"open": "09:00", "close": "18:00"},
          ...
          "5": null,
          "6": null
        }

    A missing or NULL day entry is treated as "closed". Invalid time
    strings also fall through to "closed" — we'd rather route to
    after-hours than crash a handoff because a tenant typed "9am"
    instead of "09:00".

    ``grace_minutes`` widens the window symmetrically (both open and
    close). Q-PHASE7-03 ships GA with grace=0; lever exists for v2.1.

    Pure function — no DB / network / IO. Reads ``datetime.datetime.now``
    only via ``zoneinfo.ZoneInfo(timezone)``.
    """
    if not business_hours:
        return False
    try:
        tz = ZoneInfo(timezone) if timezone else ZoneInfo("UTC")
    except Exception:  # noqa: BLE001 — fail-closed to after-hours on bad tz
        logger.warning("handoff: invalid timezone %r, treating as after-hours", timezone)
        return False
    now = datetime.datetime.now(tz=tz)
    return is_within_schedule(
        business_hours=business_hours,
        now=now,
        grace_minutes=grace_minutes,
    )


def substitute(template: str, variables: dict[str, object]) -> str:
    """Replace ``{{varname}}`` refs with str(value). Missing → empty string.

    Thin wrapper over ``chatwoot.templates.substitute`` — the single
    ``{{varname}}`` substitution implementation in the codebase (flow_runtime
    had its own copy pre-retirement; this is the sole survivor).
    """
    return _shared_substitute(template, variables)


def _sanitize_for_private_note(text: str, *, max_len: int) -> str:
    """Strip Markdown-link / code-fence injection vectors + truncate.

    Chatwoot renders private notes as Markdown. A customer who knows
    their reply will be echoed into a private note can craft injection
    payloads: ``[click here](javascript:alert(1))`` becomes a clickable
    link in the agent's UI. Modern Chatwoot strips ``javascript:`` from
    href attributes, so the practical risk is limited to cosmetic
    confusion + phishing-style display text. We neutralise the syntax
    anyway: replacing ``[`` ``]`` `` ` `` with visually-similar Unicode
    variants leaves the visible text intact but defeats the Markdown
    parser.

    Truncation to ``max_len`` happens AFTER sanitisation so the cap is
    measured against final character count (the Unicode replacements
    are single codepoints).
    """
    sanitized = text.translate(_SANITIZE_MAP)
    if len(sanitized) > max_len:
        # Reserve one char for the ellipsis to keep length bounded.
        sanitized = sanitized[: max_len - 1] + "…"  # …
    return sanitized


# =====================================================================
# DB lookup
# =====================================================================


# Branch resolution: prefer the conversation's own ``tenant_branch_id``, else
# fall back to the channel's *current* branch (matched by ``(tenant_id,
# channel)`` kind). This keeps handoff correct for conversations created before
# the channel was assigned to a branch — those rows have a NULL
# ``tenant_branch_id`` (only stamped at INSERT, never re-stamped) and may also
# have a NULL ``channel_id`` (column added later), so we match on ``channel``
# kind, not ``channel_id``. A tenant has at most one channel per kind
# (entitlements model); ``LIMIT 1`` guards against fan-out regardless.
_LOAD_CTX_SQL = """
WITH conv AS (
    SELECT
        c.tenant_id,
        c.chatwoot_conversation_id,
        c.channel,
        c.channel_user_id,
        COALESCE(
            c.tenant_branch_id,
            (SELECT tc.tenant_branch_id
               FROM tenant_channels tc
              WHERE tc.tenant_id = c.tenant_id
                AND tc.channel   = c.channel
              LIMIT 1)
        ) AS resolved_branch_id
    FROM conversations c
    WHERE c.id = $1 AND c.tenant_id = $2
)
SELECT
    t.business_hours                    AS tenant_business_hours,
    t.timezone                          AS tenant_timezone,
    t.handoff_message_in_hours          AS tenant_in_hours,
    t.handoff_message_after_hours       AS tenant_after_hours,
    t.chatwoot_account_id               AS chatwoot_account_id,
    conv.chatwoot_conversation_id       AS chatwoot_conversation_id,
    conv.channel                        AS channel,
    conv.channel_user_id                AS channel_user_id,
    conv.resolved_branch_id             AS tenant_branch_id,
    tb.business_hours                   AS branch_business_hours,
    tb.handoff_message_in_hours         AS branch_in_hours,
    tb.handoff_message_after_hours      AS branch_after_hours
FROM conv
JOIN tenants t        ON t.id  = conv.tenant_id
LEFT JOIN tenant_branches tb ON tb.id = conv.resolved_branch_id
"""


async def _load_handoff_context(
    conn: asyncpg.Connection, conversation_id: UUID, tenant_id: UUID
) -> dict[str, object] | None:
    """SELECT the joined config + conversation context needed for handoff.

    Returns ``None`` when the conversation doesn't exist under this
    tenant — caller maps to ``HandoffOutcome.ERRORED``.
    """
    row = await conn.fetchrow(_LOAD_CTX_SQL, conversation_id, tenant_id)
    return dict(row) if row else None


def _normalise_jsonb(value: object) -> dict[str, object]:
    """asyncpg returns JSONB as ``str`` OR ``dict`` depending on connection
    init. Normalise to ``dict`` so downstream callers don't care."""
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


# =====================================================================
# Recovery marker + event row
# =====================================================================


async def _mark_pending_recovery(
    conn: asyncpg.Connection,
    conversation_id: UUID,
    tenant_id: UUID,
    reason: str,
) -> None:
    """Q-PHASE7-04: set ``conversations.handoff_pending_recovery_*`` so the
    next inbound webhook handler posts a recovery private note.

    Idempotent in the sense that re-marking with a different reason
    overwrites the previous marker (most-recent failure wins). The
    inbound handler clears both columns atomically when it posts the
    recovery note.
    """
    await conn.execute(
        """
        UPDATE conversations
           SET handoff_pending_recovery_at     = NOW(),
               handoff_pending_recovery_reason = $3
         WHERE id = $1 AND tenant_id = $2
        """,
        conversation_id,
        tenant_id,
        reason,
    )


async def _persist_handoff_initiated_event(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    conversation_id: UUID,
    chatwoot_conversation_id: int | None,
    channel: str,
    team_id: str,
    business_hours_state: Literal["in_hours", "after_hours"],
    requested_at: datetime.datetime,
    initiated_at: datetime.datetime,
) -> None:
    """Q-PHASE7-08: persist ``handoff.initiated`` row to
    ``webhook_events_outbound``. No dispatch worker at GA — the v3.0
    outbound webhook delivery worker will read this row format.

    ``external_id`` is the conversation_id so re-firing handoff on the
    same conversation appends a new event row (multiple handoffs over
    a long-lived conversation are possible — resume after auto-resolve).
    Use UUIDv4 suffix to keep the (source, external_id) unique
    constraint happy in webhook_events_outbound.
    """
    payload = {
        "version": 1,
        "tenant_id": str(tenant_id),
        "conversation_id": str(conversation_id),
        "channel": channel,
        "requested_at": requested_at.isoformat(),
        "initiated_at": initiated_at.isoformat(),
        "reason": "intent_classified",
        "chatwoot_conversation_id": chatwoot_conversation_id,
        "team_id": team_id,
        "business_hours_state": business_hours_state,
    }
    await conn.execute(
        """
        INSERT INTO webhook_events_outbound (
            id, tenant_id, source, event_type, external_id, payload, created_at
        ) VALUES ($1, $2, 'handoff', 'handoff.initiated', $3, $4::jsonb, NOW())
        """,
        uuid4(),
        tenant_id,
        f"{conversation_id}:{uuid4().hex[:12]}",
        # dict DIRECTLY — JSONB codec serialises once (services/audit.py).
        payload,
    )


# =====================================================================
# Orchestration
# =====================================================================


def _now_utc() -> datetime.datetime:
    return datetime.datetime.now(UTC)


def _errored(
    *,
    reason: str,
    business_hours_state: Literal["in_hours", "after_hours"] = "after_hours",
) -> HandoffResult:
    """Build an ERRORED result without any side-effects.

    Used for early-exit paths (no conversation, missing chatwoot link,
    bad team_id) — we never started the orchestration so no recovery
    marker is owed.
    """
    return HandoffResult(
        outcome="errored",
        chatwoot_status_flipped=False,
        team_assigned=False,
        private_note_posted=False,
        customer_message_sent=False,
        customer_message_id=None,
        chatwoot_conversation_id=None,
        handoff_at=_now_utc().isoformat(),
        business_hours_state=business_hours_state,
        reason=reason,
    )


async def initiate_handoff(
    *,
    tenant_id: UUID,
    conversation_id: UUID,
    team_id_str: str,
    private_note: str,
    last_reply: str | None,
    intent_tier: str | None,
    customer_name: str | None,
    requested_at: datetime.datetime,
    conn: asyncpg.Connection,
    chatwoot: ChatwootClient,
    meta_client: MetaWhatsAppClient,
) -> HandoffResult:
    """Main orchestration entry point. See module docstring.

    Args:
      tenant_id, conversation_id: scoping
      team_id_str: from HandoffNode.team_id (flow definition carries strings)
      private_note: already-substituted text from the flow runtime's
          ``send_template._substitute`` call inside the handoff node
          executor. We re-sanitise here for the Markdown-injection
          surface — defence-in-depth at the trust boundary.
      last_reply, intent_tier, customer_name: substitution values for
          the customer-facing handoff message template (tenant config)
      requested_at: when the customer's handoff intent was classified
          (the inbound message timestamp; carried into the event row)
      conn: asyncpg connection, RLS-scoped via the caller's GUC
      chatwoot, meta_client: outbound HTTP clients
    """
    settings = get_settings()
    initiated_at = _now_utc()

    # ----- 1. Load context -----
    ctx = await _load_handoff_context(conn, conversation_id, tenant_id)
    if ctx is None:
        return _errored(reason="conversation_not_found")

    chatwoot_account_id = ctx.get("chatwoot_account_id")
    chatwoot_conversation_id = ctx.get("chatwoot_conversation_id")
    channel = ctx.get("channel") or "whatsapp"
    channel_user_id = ctx.get("channel_user_id")

    if chatwoot_account_id is None:
        return _errored(reason="tenant_chatwoot_not_linked")
    if chatwoot_conversation_id is None:
        return _errored(reason="conversation_chatwoot_not_linked")
    if not isinstance(channel_user_id, str) or not channel_user_id:
        return _errored(reason="conversation_channel_user_id_missing")
    assert isinstance(chatwoot_account_id, int)
    assert isinstance(chatwoot_conversation_id, int)

    # ----- 2. Resolve team_id (flow carries strings; Chatwoot wants int) -----
    # "0" / "" / None = no team assignment (useful when no CW team exists yet).
    _team_id_raw = str(team_id_str).strip() if team_id_str else ""
    if _team_id_raw in ("", "0", "none", "null"):
        team_id_int: int | None = None
    else:
        try:
            team_id_int = int(_team_id_raw)
        except (TypeError, ValueError):
            return _errored(reason=f"team_id_not_numeric:{team_id_str!r}")

    # ----- 3. Business-hours classification -----
    branch_hours = _normalise_jsonb(ctx.get("branch_business_hours"))
    tenant_hours = _normalise_jsonb(ctx.get("tenant_business_hours"))
    effective_hours = branch_hours if branch_hours else tenant_hours
    raw_tz = ctx.get("tenant_timezone")
    timezone: str | None = raw_tz if isinstance(raw_tz, str) else None
    in_hours = is_in_business_hours(
        business_hours=effective_hours,
        timezone=timezone,
        grace_minutes=settings.handoff_business_hours_grace_min,
    )
    state: Literal["in_hours", "after_hours"] = "in_hours" if in_hours else "after_hours"

    # ----- 4. Select + render customer message (branch overrides tenant) -----
    if in_hours:
        msg_template = (
            ctx.get("branch_in_hours")
            or ctx.get("tenant_in_hours")
            or "Transferring you to a human agent — one moment."
        )
    else:
        msg_template = (
            ctx.get("branch_after_hours")
            or ctx.get("tenant_after_hours")
            or "We're outside business hours — an agent will reply when back."
        )
    sub_vars: dict[str, object] = {
        "customer_name": customer_name or "",
        "intent_tier": intent_tier or "",
        "last_reply": last_reply or "",
    }
    customer_message = substitute(str(msg_template), sub_vars)

    # ----- 5. Sanitise private note + truncate -----
    safe_note = _sanitize_for_private_note(
        private_note, max_len=settings.handoff_private_note_max_len
    )

    # ----- 6. Resolve Meta WABA token (per-tenant decrypted) -----
    # services.secrets.get returns bytes (the libsodium-decrypted token).
    # meta_whatsapp.send_text wants str — decode with utf-8 (matches
    # Phase 6 broadcast worker line ~561 pattern).
    from polaris_api.services.secrets import get as secrets_get

    waba_token: str | None = None
    try:
        token_bytes = await secrets_get(conn, tenant_id, _WABA_TOKEN_KIND)
        if token_bytes is not None:
            waba_token = token_bytes.decode("utf-8")
    except Exception:  # noqa: BLE001 — failure surfaces as customer-send-fail below
        logger.exception(
            "handoff: failed to load WABA token for tenant_id=%s", tenant_id
        )
        waba_token = None

    # Resolve phone_number_id from the conversation's tenant_channels row.
    # tenant_channels lookup keyed on (tenant_id, channel) — Phase 1
    # schema doesn't FK channel kind back through conversations; we look
    # up by channel string. Multi-WABA tenants (ADR-015 multiplicity)
    # need a more specific lookup; for GA we pick the first matching row.
    phone_number_id: str | None = await conn.fetchval(
        """
        SELECT phone_number_id FROM tenant_channels
         WHERE tenant_id = $1 AND channel = $2 AND phone_number_id IS NOT NULL
         ORDER BY created_at ASC LIMIT 1
        """,
        tenant_id,
        channel,
    )

    # ----- 7. Fire customer message + Chatwoot path in parallel -----
    # silent-failure-hunter MEDIUM: _send_customer returns a 3-tuple
    # (success, wamid, error_str) so all state flows through return
    # values. The previous nonlocal-shared `customer_error` had a
    # double-writer trap where gather's exception-catch path could
    # overwrite whatever the coroutine set.
    chatwoot_path_exc: BaseException | None = None
    chatwoot_status_flipped = False
    team_assigned = False
    private_note_posted = False

    async def _send_customer() -> tuple[bool, str | None, str | None]:
        if not phone_number_id or waba_token is None:
            return False, None, "waba_not_configured"
        try:
            wamid = await meta_client.send_text(
                phone_number_id=phone_number_id,
                to=channel_user_id,
                body=customer_message,
                access_token=waba_token,
            )
            return True, wamid, None
        except MetaWhatsAppError as exc:
            logger.warning(
                "handoff: customer send failed tenant_id=%s status=%s",
                tenant_id,
                exc.status_code,
            )
            return False, None, f"meta_{exc.status_code}"
        except Exception as exc:  # noqa: BLE001
            logger.exception("handoff: customer send unexpected exception")
            return False, None, f"meta_exception:{type(exc).__name__}"

    async def _chatwoot_path() -> None:
        nonlocal chatwoot_status_flipped, team_assigned, private_note_posted
        # Sequential — Chatwoot doesn't tolerate parallel writes against
        # the same conversation (PR-2 client docstring + 04h §9).
        await chatwoot.flip_conversation_status(
            account_id=chatwoot_account_id,
            conversation_id=chatwoot_conversation_id,
            status="open",
        )
        chatwoot_status_flipped = True
        if team_id_int is not None:
            await chatwoot.assign_team(
                account_id=chatwoot_account_id,
                conversation_id=chatwoot_conversation_id,
                team_id=team_id_int,
            )
            team_assigned = True
        await sync_handling_label(
            account_id=chatwoot_account_id,
            conversation_id=chatwoot_conversation_id,
            owner="human",
            chatwoot=chatwoot,
        )
        await chatwoot.post_private_note(
            account_id=chatwoot_account_id,
            conversation_id=chatwoot_conversation_id,
            content=safe_note,
        )
        private_note_posted = True

    results = await asyncio.gather(
        _send_customer(), _chatwoot_path(), return_exceptions=True
    )
    customer_result, chatwoot_result = results

    customer_sent: bool
    customer_msg_id: str | None
    customer_error: str | None
    if isinstance(customer_result, BaseException):
        # Should be unreachable — _send_customer catches both
        # MetaWhatsAppError and bare Exception above. Defensive
        # fall-through in case a future refactor narrows the catch.
        customer_sent = False
        customer_msg_id = None
        customer_error = f"meta_exception:{type(customer_result).__name__}"
        logger.error(
            "handoff: _send_customer escaped its except blocks — "
            "this indicates a coroutine-internal bug; investigate."
        )
    else:
        customer_sent, customer_msg_id, customer_error = customer_result

    if isinstance(chatwoot_result, BaseException):
        chatwoot_path_exc = chatwoot_result

    # ----- 8. Classify outcome + side-effects -----

    # Customer-send failed: ERRORED regardless of Chatwoot path. Customer
    # will message again; do NOT write a recovery marker (next inbound is
    # not a "agent-please-review" moment — it's just the customer retrying).
    if not customer_sent:
        return HandoffResult(
            outcome="errored",
            chatwoot_status_flipped=chatwoot_status_flipped,
            team_assigned=team_assigned,
            private_note_posted=private_note_posted,
            customer_message_sent=False,
            customer_message_id=None,
            chatwoot_conversation_id=chatwoot_conversation_id,
            handoff_at=_now_utc().isoformat(),
            business_hours_state=state,
            reason=customer_error or "customer_send_failed",
        )

    # Chatwoot path errored — classify + write recovery marker (Q-PHASE7-04).
    if chatwoot_path_exc is not None:
        if isinstance(chatwoot_path_exc, ChatwootTransientError):
            recovery_reason = _RECOVERY_5XX
            outcome: Literal["confirmed", "declined", "errored"] = "declined"
            reason_str = "chatwoot_transient"
        elif isinstance(chatwoot_path_exc, ChatwootStructuralError):
            recovery_reason = _RECOVERY_4XX
            outcome = "errored"
            reason_str = f"chatwoot_4xx_{chatwoot_path_exc.status_code}"
        else:
            # Unexpected exception from the Chatwoot path. Treat as
            # transient for retry-on-next-inbound purposes but classify
            # as errored in the result (operator should investigate).
            recovery_reason = _RECOVERY_TIMEOUT
            outcome = "errored"
            reason_str = f"chatwoot_unexpected:{type(chatwoot_path_exc).__name__}"
            logger.exception(
                "handoff: unexpected chatwoot exception", exc_info=chatwoot_path_exc
            )

        await _mark_pending_recovery(conn, conversation_id, tenant_id, recovery_reason)

        return HandoffResult(
            outcome=outcome,
            chatwoot_status_flipped=chatwoot_status_flipped,
            team_assigned=team_assigned,
            private_note_posted=private_note_posted,
            customer_message_sent=True,
            customer_message_id=customer_msg_id,
            chatwoot_conversation_id=chatwoot_conversation_id,
            handoff_at=_now_utc().isoformat(),
            business_hours_state=state,
            reason=reason_str,
        )

    # ----- 9. Both succeeded → CONFIRMED + write event row + handoff_at -----
    confirmed_at = _now_utc()
    await conn.execute(
        """
        UPDATE conversations SET handoff_at = $3 WHERE id = $1 AND tenant_id = $2
        """,
        conversation_id,
        tenant_id,
        confirmed_at,
    )
    await conn.execute(
        """
        UPDATE tenants SET handoff_at = $2 WHERE id = $1
        """,
        tenant_id,
        confirmed_at,
    )
    try:
        await _persist_handoff_initiated_event(
            conn,
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            chatwoot_conversation_id=chatwoot_conversation_id,
            channel=str(channel),
            team_id=team_id_str,
            business_hours_state=state,
            requested_at=requested_at,
            initiated_at=initiated_at,
        )
    except Exception:  # noqa: BLE001 — event-row write is best-effort
        # Don't fail the handoff if the event-row insert blows up; the
        # customer sees the handoff message + the agent sees the
        # conversation. v3.0 worker will simply have one less row to
        # process. Log with a greppable sentinel so ops can alert on
        # the gap (silent-failure-hunter HIGH finding).
        logger.exception(
            "HANDOFF_EVENT_ROW_MISSING: failed to persist handoff.initiated "
            "row tenant_id=%s conversation_id=%s — customer + agent "
            "experience unaffected, but v3.0 webhook delivery has nothing "
            "to replay. Ops: grep 'HANDOFF_EVENT_ROW_MISSING' for prevalence.",
            tenant_id,
            conversation_id,
        )

    # Audit trail — bot-initiated handoff (no human actor). Best-effort:
    # a failed audit write must never fail a confirmed handoff. Same
    # non-fatal posture as the event-row write above.
    try:
        await audit.record(
            conn,
            tenant_id,
            None,
            "handoff.initiated",
            before=None,
            after={
                "entity_type": "conversation",
                "entity_id": str(conversation_id),
                "label": "human-handling",
                "reason": "intent_classified",
                "business_hours_state": state,
                "chatwoot_conversation_id": chatwoot_conversation_id,
            },
        )
    except Exception:  # noqa: BLE001 — audit write is best-effort
        logger.warning(
            "handoff: failed to write audit_logs handoff.initiated row "
            "tenant_id=%s conversation_id=%s",
            tenant_id,
            conversation_id,
        )

    return HandoffResult(
        outcome="confirmed",
        chatwoot_status_flipped=True,
        team_assigned=True,
        private_note_posted=True,
        customer_message_sent=True,
        customer_message_id=customer_msg_id,
        chatwoot_conversation_id=chatwoot_conversation_id,
        handoff_at=confirmed_at.isoformat(),
        business_hours_state=state,
        reason=None,
    )
