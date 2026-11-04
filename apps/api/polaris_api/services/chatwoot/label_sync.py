"""Chatwoot label sync — Phase 7 PR-4.

Two non-blocking side-effects that propagate Polaris-side state changes
into Chatwoot as contact / conversation labels:

  * ``sync_opt_out_label``      — fires from ``services/audience/opt_out.py
                                  ::mark_opted_out`` after the DB write.
                                  Applies ``pol_opted_out`` to the matching
                                  Chatwoot contact. Eventual consistency
                                  ≤5s SLA per [04 Phase 7 exit criteria].

  * ``sync_suspension_label``   — one-shot scan called from the tenant-
                                  suspension state-transition handler
                                  (Phase 9 platform-admin work; ready
                                  for that integration). Applies
                                  ``pol_bot_suspended`` to every open
                                  conversation under the tenant's
                                  ``chatwoot_account_id``.

Both functions are SELECT-driven + non-blocking. They silently skip
when the Chatwoot link is missing (NULL ``chatwoot_contact_id`` /
``chatwoot_conversation_id`` / ``chatwoot_account_id``) — the Polaris-
side write is the source of truth; the Chatwoot label is best-effort.

Chatwoot failures (transient OR structural) are caught and logged but
NOT re-raised — opt-out is PDPA-blocking work that MUST succeed
regardless of Chatwoot health.

Q-PHASE7-02 hybrid label provisioning: ``ChatwootClient.apply_*_label``
calls ``ensure_label`` first (when ``CHATWOOT_LABEL_AUTO_PROVISION=true``,
the GA default). Phase 9 tenant provisioning will also seed the two
GA labels at tenant creation — defence-in-depth.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Literal
from uuid import UUID

import asyncpg

from polaris_api.services.chatwoot.client import ChatwootClient, ChatwootError

logger = logging.getLogger(__name__)


# Label constants — keep aligned with [04h §11 Q-PHASE7-02] and the
# Phase 9 platform-admin tenant-provisioning seeder.
_LABEL_OPT_OUT = "pol_opted_out"
_LABEL_BOT_SUSPENDED = "pol_bot_suspended"

# Handling-ownership labels (Chatwoot-native rehaul, stage 3). Exactly one is
# present on a conversation at any time, mirroring its status: bot-handling
# while the bot owns it (status=pending), human-handling once escalated
# (status=open). The default agent inbox filters to status=Open so it shows
# only human conversations; agents click the bot-handling label to view bot
# activity.
_LABEL_BOT_HANDLING = "bot-handling"
_LABEL_HUMAN_HANDLING = "human-handling"
_HANDLING_LABELS = frozenset({_LABEL_BOT_HANDLING, _LABEL_HUMAN_HANDLING})

HandlingOwner = Literal["bot", "human"]


def _hash_channel_user_id(channel_user_id: str) -> str:
    """Return an 8-char correlation hash for log lines.

    ``channel_user_id`` is typically an E.164 phone number for
    WhatsApp / a Page-Scoped ID for Messenger — both PII. Logs that
    might reach a shared aggregator (Sentry, Grafana Cloud) must not
    carry PII verbatim. The hash is one-way + deterministic so ops can
    grep by hash across multiple log lines for the same number without
    seeing the number itself. SHA-256 truncated is sufficient — this
    is correlation, not authentication.
    """
    return hashlib.sha256(channel_user_id.encode("utf-8")).hexdigest()[:8]


# =====================================================================
# Opt-out label sync
# =====================================================================


_LOOKUP_OPT_OUT_SQL = """
SELECT
    a.chatwoot_contact_id  AS chatwoot_contact_id,
    t.chatwoot_account_id  AS chatwoot_account_id
FROM audience_members a
JOIN tenants t ON t.id = a.tenant_id
WHERE a.tenant_id      = $1
  AND a.channel        = $2
  AND a.channel_user_id = $3
ORDER BY a.opted_out_at DESC NULLS LAST, a.created_at DESC
LIMIT 1
"""


async def sync_opt_out_label(
    *,
    tenant_id: UUID,
    channel: str,
    channel_user_id: str,
    conn: asyncpg.Connection,
    chatwoot: ChatwootClient | None,
) -> bool:
    """Apply ``pol_opted_out`` to the matching Chatwoot contact.

    Returns ``True`` iff the label was actually applied. Returns
    ``False`` in any of these skip conditions:
      - ``chatwoot`` is None (test / simulator)
      - no ``audience_members`` row matches (STOP from an unknown number)
      - the matched row has NULL ``chatwoot_contact_id`` (CSV-imported
        contact that never had a Chatwoot interaction)
      - the tenant has NULL ``chatwoot_account_id`` (demo tenant)
      - Chatwoot rejected the call (logged, never re-raised)

    Idempotent: re-call with the same args after a successful apply
    is a Chatwoot-side 200 no-op (labels endpoint is set-style, not
    list-append).
    """
    if chatwoot is None:
        return False

    row = await conn.fetchrow(
        _LOOKUP_OPT_OUT_SQL, tenant_id, channel, channel_user_id
    )
    if row is None:
        # STOP from a number never registered as an audience_member —
        # opt-out path in Phase 6 still records the audit row; we just
        # don't have a Chatwoot contact to label.
        return False

    contact_id = row["chatwoot_contact_id"]
    account_id = row["chatwoot_account_id"]
    if not isinstance(contact_id, int) or not isinstance(account_id, int):
        # security-reviewer Nit 1: hash channel_user_id (PII) — never log
        # the raw E.164 / page-scoped id. Hash is deterministic so ops
        # can grep by hash across log lines for the same number.
        logger.debug(
            "label_sync: opt_out skip (chatwoot link missing) "
            "tenant_id=%s channel=%s cu_hash=%s "
            "contact_id=%r account_id=%r",
            tenant_id,
            channel,
            _hash_channel_user_id(channel_user_id),
            contact_id,
            account_id,
        )
        return False

    try:
        await chatwoot.apply_contact_label(
            account_id=account_id,
            contact_id=contact_id,
            label=_LABEL_OPT_OUT,
        )
    except ChatwootError as exc:
        logger.warning(
            "label_sync: opt_out label apply failed (non-blocking) "
            "tenant_id=%s contact_id=%s status=%s err=%s",
            tenant_id,
            contact_id,
            getattr(exc, "status_code", None),
            type(exc).__name__,
        )
        return False
    except Exception:  # noqa: BLE001 — bound the non-blocking surface
        logger.exception(
            "label_sync: opt_out label apply unexpected exception "
            "(non-blocking) tenant_id=%s contact_id=%s",
            tenant_id,
            contact_id,
        )
        return False

    logger.info(
        "label_sync: pol_opted_out applied tenant_id=%s contact_id=%s",
        tenant_id,
        contact_id,
    )
    return True


# =====================================================================
# Suspension label sync
# =====================================================================


_LOOKUP_TENANT_SQL = """
SELECT chatwoot_account_id FROM tenants WHERE id = $1
"""


_LIST_OPEN_CONVERSATIONS_SQL = """
SELECT chatwoot_conversation_id
FROM conversations
WHERE tenant_id = $1
  AND chatwoot_conversation_id IS NOT NULL
"""


async def sync_suspension_label(
    *,
    tenant_id: UUID,
    conn: asyncpg.Connection,
    chatwoot: ChatwootClient | None,
) -> int:
    """One-shot scan: apply ``pol_bot_suspended`` to all open Chatwoot
    conversations for the tenant.

    Returns the count of conversations successfully labelled. Caller
    decides whether to alert on a count below expectation.

    Called by Phase 9 platform-admin's tenant-suspension state-
    transition handler (which sets ``tenants.suspended_reason``).
    Phase 7 ships this function ready for Phase 9 to invoke; until
    then the suspension flow is operator-manual (operator runs the
    function from a REPL on suspension).

    Partial-failure posture: if one conversation's label apply fails
    (e.g. conversation already deleted Chatwoot-side), the scan
    continues with the rest. Better to label most conversations than
    to bail on the first error. Failures log at WARNING.
    """
    if chatwoot is None:
        return 0

    tenant_row = await conn.fetchrow(_LOOKUP_TENANT_SQL, tenant_id)
    if tenant_row is None:
        logger.warning(
            "label_sync: sync_suspension_label called for unknown tenant_id=%s",
            tenant_id,
        )
        return 0

    account_id = tenant_row["chatwoot_account_id"]
    if not isinstance(account_id, int):
        logger.info(
            "label_sync: suspension skip (tenant not chatwoot-linked) "
            "tenant_id=%s",
            tenant_id,
        )
        return 0

    rows = await conn.fetch(_LIST_OPEN_CONVERSATIONS_SQL, tenant_id)
    applied = 0
    failed = 0
    for row in rows:
        cw_conv_id = row["chatwoot_conversation_id"]
        if not isinstance(cw_conv_id, int):
            continue
        try:
            await chatwoot.apply_conversation_label(
                account_id=account_id,
                conversation_id=cw_conv_id,
                label=_LABEL_BOT_SUSPENDED,
            )
            applied += 1
        except ChatwootError as exc:
            failed += 1
            logger.warning(
                "label_sync: suspension label apply failed "
                "tenant_id=%s chatwoot_conv_id=%s status=%s err=%s",
                tenant_id,
                cw_conv_id,
                getattr(exc, "status_code", None),
                type(exc).__name__,
            )
        except Exception:  # noqa: BLE001 — bound the loop
            failed += 1
            logger.exception(
                "label_sync: suspension label apply unexpected exception "
                "tenant_id=%s chatwoot_conv_id=%s",
                tenant_id,
                cw_conv_id,
            )

    # silent-failure-hunter MEDIUM: a full Chatwoot outage at suspension
    # time means we scanned N conversations and labelled zero. Default
    # INFO log hides that as routine traffic; alert rules can't catch it.
    # Promote to ERROR when applied=0 and failed>0 so ops gets paged.
    if rows and applied == 0 and failed > 0:
        logger.error(
            "label_sync: pol_bot_suspended scan ZERO_APPLIED_UNDER_OUTAGE "
            "tenant_id=%s failed=%d total=%d — Chatwoot likely fully unreachable; "
            "operator must verify suspension label state when Chatwoot recovers. "
            "Ops: grep 'ZERO_APPLIED_UNDER_OUTAGE' for prevalence.",
            tenant_id,
            failed,
            len(rows),
        )
    else:
        logger.info(
            "label_sync: pol_bot_suspended scan complete tenant_id=%s "
            "applied=%d failed=%d total=%d",
            tenant_id,
            applied,
            failed,
            len(rows),
        )
    return applied


# =====================================================================
# Handling-ownership label sync (bot-handling / human-handling)
# =====================================================================


def compute_handling_labels(current: list[str], owner: HandlingOwner) -> list[str]:
    """Return the conversation label set carrying exactly one handling label.

    Drops BOTH handling labels from ``current``, then appends the one matching
    ``owner`` (``bot`` → ``bot-handling``, ``human`` → ``human-handling``).
    Every non-handling label (e.g. ``pol_bot_suspended``) is preserved in its
    original order; duplicates are collapsed to first occurrence. Pure — no IO.
    """
    desired = _LABEL_BOT_HANDLING if owner == "bot" else _LABEL_HUMAN_HANDLING
    out: list[str] = []
    seen: set[str] = set()
    for label in current:
        if label in _HANDLING_LABELS or label in seen:
            continue
        seen.add(label)
        out.append(label)
    out.append(desired)
    return out


async def sync_handling_label(
    *,
    account_id: int,
    conversation_id: int,
    owner: HandlingOwner,
    chatwoot: ChatwootClient,
) -> bool:
    """Ensure a conversation carries exactly one handling label = ``owner``.

    Read-modify-write so unrelated conversation labels survive (Chatwoot's
    labels endpoint is set-style/replace, not append). Skips the write when
    the conversation already carries exactly the desired handling label (and
    no duplicates), so the steady-state cost is a single GET — cheap enough to
    call on every bot turn.

    NOT atomic: the GET→POST has no compare-and-swap, so two concurrent calls
    (e.g. a handoff racing a bot turn across workers) can clobber each other's
    non-handling labels in the window. Accepted — Chatwoot offers no CAS and
    the handling labels themselves always converge (last writer wins).

    Raises ``ChatwootError`` on Chatwoot failure: callers keep their own
    posture (handoff treats it as a critical write; pipeline / reverse-handoff
    / email-assist wrap it best-effort). Returns ``True`` if a write was
    issued, ``False`` if already in sync.
    """
    current = await chatwoot.get_conversation_labels(
        account_id=account_id, conversation_id=conversation_id
    )
    desired = compute_handling_labels(current, owner)
    # Skip the write only when the conversation ALREADY carries exactly the
    # desired set AND has no duplicate entries to normalise. Compare as sets
    # (Chatwoot stores labels unordered, so order alone is not worth a write),
    # but the ``len`` guard means a degenerate ["x","x",…] state still gets
    # rewritten cleanly instead of being treated as already-synced.
    if set(current) == set(desired) and len(current) == len(set(current)):
        return False
    # Provision the handling label we're adding (compute_handling_labels placed
    # it last); ensure_label is a no-op when auto-provision is off or it already
    # exists. The labels we keep already exist by construction.
    await chatwoot.ensure_label(account_id=account_id, label=desired[-1])
    await chatwoot.set_conversation_labels(
        account_id=account_id, conversation_id=conversation_id, labels=desired
    )
    return True
