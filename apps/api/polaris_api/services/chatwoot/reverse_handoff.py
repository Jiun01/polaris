"""Reverse handoff — bot resumes after agent resolves or removes human-handling.

Called from ``webhooks/chatwoot_router.py`` when Chatwoot fires a
``conversation_status_changed`` (status=resolved) or ``label_updated``
(human-handling removed) event.

Acts ONLY on a genuine handoff→resume transition:
  - Conversation found + ``handoff_at`` set → resume (steps 2-4 below).
  - Conversation found + ``handoff_at`` IS NULL → clean no-op. The conversation
    was never in handoff (or a prior resume already cleared it), so an unrelated
    label change / status flip must not resume the bot over a live human.
  - Conversation NOT found by ``chatwoot_conversation_id`` → race fallback: the
    resume can arrive before the pipeline committed the link. If EXACTLY ONE
    in-handoff conversation is still unlinked (``chatwoot_conversation_id IS
    NULL``), reconcile it (back-fill the link) and resume; otherwise escalate to
    ERROR (a stuck handoff must be loud, not a quiet warning).

Side-effects on a real transition (in order, inside one DB transaction):
  1. Resolve / reconcile the Polaris conversation row.
  2. Clear handoff columns: ``handoff_at``,
     ``handoff_pending_recovery_at``, ``handoff_pending_recovery_reason``.
  3. Write ``handoff.resolved`` to ``audit_logs``.

After the transaction commits (best-effort, outside tx):
  4. Apply ``bot-handling`` label via Chatwoot API.
  5. Flip the Chatwoot conversation status back to ``pending`` so the bot
     re-owns it. The label alone is cosmetic — the inbound gate in
     ``chatwoot_router.is_bot_owned`` keys on ``status == 'pending'``, so without
     this flip a resolved/reopened conversation comes back as ``open`` and the
     bot stays silent. Gated by the ``handoff_at`` check above, so ONLY a
     previously-handed-off conversation bounces back to the bot on resolve.

Any step-4/5 failure is caught + logged — the DB changes have already
committed, so a label/status write not making it is recoverable by the next
inbound or a manual operator action.  Same best-effort posture as
``handoff.initiated`` in ``handoff.py`` lines 701-726.
"""

from __future__ import annotations

import logging
from uuid import UUID

from polaris_api.db.pools import get_service_pool
from polaris_api.services import audit
from polaris_api.services.chatwoot.client import ChatwootError, get_chatwoot_client
from polaris_api.services.chatwoot.label_sync import sync_handling_label

logger = logging.getLogger(__name__)


async def process_reverse_handoff(
    *,
    tenant_id: UUID,
    chatwoot_account_id: int,
    chatwoot_conversation_id: int,
    trigger_event: str,
    trigger_detail: str,
) -> None:
    """Clear handoff state and re-apply bot-handling label.

    Args:
        tenant_id: Polaris tenant UUID.
        chatwoot_account_id: Chatwoot account.id from the webhook payload.
        chatwoot_conversation_id: Chatwoot conversation.id from the payload.
        trigger_event: The Chatwoot event name that caused this call.
        trigger_detail: Human-readable detail string for the audit row.
    """
    pool = get_service_pool()
    polaris_conversation_id: UUID | None = None
    prior_handoff_at: str | None = None
    reconciled_via_race = False

    async with pool.acquire() as conn:
        async with conn.transaction():
            # Step 1: Resolve the Polaris conversation row by its Chatwoot link.
            row = await conn.fetchrow(
                """
                SELECT id, handoff_at
                  FROM conversations
                 WHERE chatwoot_conversation_id = $1
                   AND tenant_id = $2
                """,
                chatwoot_conversation_id,
                tenant_id,
            )

            if row is not None:
                if row["handoff_at"] is None:
                    # Not in handoff: never handed off, or a prior resume already
                    # cleared it. An unrelated label change / status flip must NOT
                    # resume the bot — clean no-op (no clear/audit/label). This is
                    # the guard that kills the spurious resume over a live human,
                    # and it protects BOTH the label_updated and the
                    # conversation_status_changed trigger paths.
                    logger.debug(
                        "reverse_handoff: conversation %s not in handoff "
                        "(handoff_at IS NULL) — ignoring %s (cw_conversation=%s)",
                        row["id"],
                        trigger_event,
                        chatwoot_conversation_id,
                    )
                    return
                polaris_conversation_id = row["id"]
                prior_handoff_at = row["handoff_at"].isoformat()
            else:
                # Row not found by the Chatwoot link. This is the race where the
                # resume event arrives before the pipeline committed
                # ``chatwoot_conversation_id`` onto the conversation row. Try to
                # reconcile: a conversation that IS in handoff but UNLINKED
                # (chatwoot_conversation_id IS NULL) is what this resume targets.
                # Only act when EXACTLY ONE such candidate exists — otherwise we
                # cannot safely decide which conversation to resume.
                candidates = await conn.fetch(
                    """
                    SELECT id, handoff_at
                      FROM conversations
                     WHERE tenant_id = $1
                       AND handoff_at IS NOT NULL
                       AND chatwoot_conversation_id IS NULL
                    """,
                    tenant_id,
                )
                if len(candidates) != 1:
                    # 0 → nothing in handoff to resume (lost link / already done);
                    # >1 → ambiguous. Either way escalate LOUD (ERROR) so a stuck
                    # handoff is observable, instead of the old quiet warning that
                    # hid it.
                    logger.error(
                        "reverse_handoff: no conversations row for "
                        "chatwoot_conversation_id=%s tenant_id=%s and %d unlinked "
                        "in-handoff candidate(s) — cannot resume; conversation may "
                        "be STUCK in handoff (trigger=%s)",
                        chatwoot_conversation_id,
                        tenant_id,
                        len(candidates),
                        trigger_event,
                    )
                    return
                candidate = candidates[0]
                polaris_conversation_id = candidate["id"]
                # The candidates query filters ``handoff_at IS NOT NULL``, so this
                # is always set — record it unconditionally (same as the linked
                # path above) rather than a misleading null in the audit row.
                prior_handoff_at = candidate["handoff_at"].isoformat()
                reconciled_via_race = True
                # Reconcile the missing link so this resume is idempotent: a
                # replay then resolves the row by chatwoot_conversation_id and
                # hits the handoff_at-IS-NULL no-op above.
                await conn.execute(
                    """
                    UPDATE conversations
                       SET chatwoot_conversation_id = $1
                     WHERE id = $2
                       AND tenant_id = $3
                    """,
                    chatwoot_conversation_id,
                    polaris_conversation_id,
                    tenant_id,
                )
                logger.warning(
                    "reverse_handoff: race fallback reconciled unlinked in-handoff "
                    "conversation %s with cw_conversation=%s tenant_id=%s",
                    polaris_conversation_id,
                    chatwoot_conversation_id,
                    tenant_id,
                )

            # Step 2: Clear handoff columns.
            await conn.execute(
                """
                UPDATE conversations
                   SET handoff_at                      = NULL,
                       handoff_pending_recovery_at     = NULL,
                       handoff_pending_recovery_reason = NULL
                 WHERE id = $1
                   AND tenant_id = $2
                """,
                polaris_conversation_id,
                tenant_id,
            )

            # Step 3: Write audit row — best-effort inside the same tx.
            try:
                await audit.record(
                    conn,
                    tenant_id,
                    None,  # no human actor — Chatwoot event triggered this
                    "handoff.resolved",
                    before={"handoff_at": prior_handoff_at},
                    after={
                        "entity_type": "conversation",
                        "entity_id": str(polaris_conversation_id),
                        "trigger_event": trigger_event,
                        "trigger_detail": trigger_detail,
                        "chatwoot_conversation_id": chatwoot_conversation_id,
                        "reconciled_via_race": reconciled_via_race,
                    },
                )
            except Exception:  # noqa: BLE001 — audit write is best-effort
                logger.warning(
                    "reverse_handoff: failed to write audit_logs handoff.resolved "
                    "tenant_id=%s conversation_id=%s",
                    tenant_id,
                    polaris_conversation_id,
                )

    logger.info(
        "reverse_handoff: cleared handoff state tenant_id=%s "
        "conversation_id=%s cw_conversation=%s trigger=%s",
        tenant_id,
        polaris_conversation_id,
        chatwoot_conversation_id,
        trigger_event,
    )

    # Step 4: Apply bot-handling label — best-effort, outside tx.
    try:
        cw = get_chatwoot_client()
        await sync_handling_label(
            account_id=chatwoot_account_id,
            conversation_id=chatwoot_conversation_id,
            owner="bot",
            chatwoot=cw,
        )
        logger.info(
            "reverse_handoff: bot-handling label applied cw_conversation=%s",
            chatwoot_conversation_id,
        )
    except ChatwootError as exc:
        logger.warning(
            "reverse_handoff: failed to apply bot-handling label "
            "cw_conversation=%s: %s",
            chatwoot_conversation_id,
            exc,
        )
    except Exception:  # noqa: BLE001 — label apply is best-effort
        logger.warning(
            "reverse_handoff: unexpected error applying bot-handling label "
            "cw_conversation=%s",
            chatwoot_conversation_id,
        )

    # Step 5: Flip Chatwoot status back to ``pending`` so the bot re-owns the
    # conversation. ``is_bot_owned`` gates on status (not the label), so this is
    # what actually re-engages the bot. Independent best-effort block (own
    # try/except) so a label-sync failure above does not skip the status flip,
    # and vice versa. Only reached on a real handoff→resume transition (the
    # ``handoff_at IS NULL`` paths returned earlier).
    try:
        cw = get_chatwoot_client()
        await cw.flip_conversation_status(
            account_id=chatwoot_account_id,
            conversation_id=chatwoot_conversation_id,
            status="pending",
        )
        logger.info(
            "reverse_handoff: status flipped to pending cw_conversation=%s",
            chatwoot_conversation_id,
        )
    except ChatwootError as exc:
        logger.warning(
            "reverse_handoff: failed to flip status to pending "
            "cw_conversation=%s: %s",
            chatwoot_conversation_id,
            exc,
        )
    except Exception:  # noqa: BLE001 — status flip is best-effort
        logger.warning(
            "reverse_handoff: unexpected error flipping status to pending "
            "cw_conversation=%s",
            chatwoot_conversation_id,
        )
