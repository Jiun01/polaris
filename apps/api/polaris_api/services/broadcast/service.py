"""Broadcast CRUD + lifecycle — Phase 6 PR-5.

Five public functions:

  create_broadcast   — POST /api/tenant/broadcasts entry. Validates
                       tier-gate (Pro+ only per ADR-026 Am 3rd) +
                       template approval + audience-non-empty
                       precondition. Returns the new broadcast id;
                       status='draft' until schedule().

  schedule_broadcast — Materialises broadcast_recipients via the
                       audience_filter compiler + bulk INSERT under
                       the request transaction. Enforces the
                       monthly_broadcast_recipients quota INSIDE the
                       transaction — partial fanout impossible.
                       ZADD to ``broadcast:queue:{tenant_id}`` keyed
                       by scheduled_at unix timestamp.

  pause_broadcast    — ZREM the entry + flip status='paused'. Worker
                       checks status between recipients and bails.

  resume_broadcast   — Validate status=='paused', flip back to
                       'scheduled', ZADD again with original
                       scheduled_at.

  cancel_broadcast   — ZREM + flip status='cancelled' +
                       UPDATE broadcast_recipients SET status='skipped',
                       skip_reason='cancelled' WHERE status='pending'.

Tier-gate failure: raises ``BroadcastTierBlockedError``.
Template-not-approved: raises ``BroadcastTemplateNotApprovedError``.
Audience-empty: raises ``BroadcastAudienceEmptyError``.
Quota-exceeded: raises ``BroadcastQuotaExceededError``.
State-invalid: raises ``BroadcastStateInvalidError``.

The router maps these onto HTTP error envelopes; the service stays
pure async + DB.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Final
from uuid import UUID, uuid4

import asyncpg
from polaris_shared_types import AudienceFilter, CreateBroadcastRequest

from polaris_api.redis_pool import get_redis
from polaris_api.services import audit
from polaris_api.services.audience.filter import compile_audience_filter

logger = logging.getLogger(__name__)


class BroadcastError(RuntimeError):
    """Base for typed broadcast errors. The router maps each subclass
    onto a distinct HTTP error envelope code."""


class BroadcastTierBlockedError(BroadcastError):
    """tenant_quotas.outbound_broadcast_enabled IS FALSE — Pro+ only."""


class BroadcastTemplateNotApprovedError(BroadcastError):
    """Template status != 'approved'. Send would 4xx Meta-side."""


class BroadcastAudienceEmptyError(BroadcastError):
    """audience_filter materialised to zero rows. Refuse to schedule a
    no-op broadcast — silent zero-recipient drains corrupt the
    metrics dashboard."""


class BroadcastQuotaExceededError(BroadcastError):
    """monthly_broadcast_recipients would be exceeded. Inside the
    materialisation transaction — caller sees zero rows on rollback."""


class BroadcastStateInvalidError(BroadcastError):
    """Invalid lifecycle transition (e.g. resume on completed)."""


_DEFAULT_QUEUE_NAMESPACE: Final[str] = "broadcast"


def _queue_ns() -> str:
    return os.getenv("BROADCAST_QUEUE_NAMESPACE", _DEFAULT_QUEUE_NAMESPACE)


def _queue_key(tenant_id: UUID) -> str:
    """Redis ZSET key per tenant. The worker drains tenant-by-tenant
    to keep per-WABA rate limits honoured (one tenant -> one WABA
    typically; Meta rate-limits at the WABA level)."""
    return f"{_queue_ns()}:queue:{tenant_id}"


def _recipients_key(broadcast_id: UUID) -> str:
    """Redis LIST per broadcast — worker LPOPs to drain recipients."""
    return f"{_queue_ns()}:recipients:{broadcast_id}"


# ---------------------------------------------------------------------
# create_broadcast
# ---------------------------------------------------------------------


async def create_broadcast(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    actor_user_id: UUID | None,
    req: CreateBroadcastRequest,
) -> UUID:
    """Insert a broadcast row in status='draft'. Returns the new id.

    Validates:
      - Tier-gate: tenant_quotas.outbound_broadcast_enabled IS TRUE
      - Template exists + status='approved' (RLS-scoped)
      - tenant_channel exists (RLS-scoped)
    """
    # Tier-gate. Quota table is RLS-scoped to the tenant via the
    # request-scoped TenantContext GUC; SELECT returns at most one row.
    quota_row = await conn.fetchrow(
        "SELECT outbound_broadcast_enabled FROM tenant_quotas WHERE tenant_id = $1",
        tenant_id,
    )
    if quota_row is None or not quota_row["outbound_broadcast_enabled"]:
        raise BroadcastTierBlockedError(
            f"tenant {tenant_id}: outbound_broadcast_enabled=false (Pro+ only)"
        )

    # Template approval check.
    template_row = await conn.fetchrow(
        "SELECT id, status, tenant_channel_id FROM tenant_templates WHERE id = $1",
        req.template_id,
    )
    if template_row is None:
        raise BroadcastTemplateNotApprovedError(
            f"template {req.template_id} not found"
        )
    if template_row["status"] != "approved":
        raise BroadcastTemplateNotApprovedError(
            f"template {req.template_id} status={template_row['status']!r} "
            "(must be 'approved')"
        )

    # Channel existence (caller may specify a different channel from the
    # template's binding; both must exist for this tenant).
    channel_exists = await conn.fetchval(
        "SELECT 1 FROM tenant_channels WHERE id = $1",
        req.tenant_channel_id,
    )
    if not channel_exists:
        raise BroadcastError(
            f"tenant_channel {req.tenant_channel_id} not found for tenant"
        )

    new_id = uuid4()
    await conn.execute(
        """
        INSERT INTO broadcasts
            (id, tenant_id, tenant_channel_id, template_id, name,
             audience_filter, variables_template, status,
             scheduled_at, created_by, created_at, updated_at)
        VALUES
            ($1, $2, $3, $4, $5, $6::jsonb, $7::jsonb, 'draft',
             $8, $9, now(), now())
        """,
        new_id,
        tenant_id,
        req.tenant_channel_id,
        req.template_id,
        req.name,
        # Pass the dict/list DIRECTLY — pools.py registers a JSONB codec
        # (encoder=json.dumps) so asyncpg serialises ONCE. A json.dumps(...)
        # string here would be double-encoded into a JSONB string (see
        # services/audit.py). The discriminated union doesn't dump cleanly via
        # model_dump on its own — we round-trip the underlying model.
        req.audience_filter.model_dump(mode="json"),
        req.variables_template,
        req.scheduled_at,
        actor_user_id,
    )

    await audit.record(
        conn,
        tenant_id,
        actor_user_id,
        "broadcast.created",
        before=None,
        after={
            "broadcast_id": str(new_id),
            "name": req.name,
            "template_id": str(req.template_id),
            "scheduled_at": (
                req.scheduled_at.isoformat() if req.scheduled_at else None
            ),
        },
    )

    return new_id


# ---------------------------------------------------------------------
# schedule_broadcast
# ---------------------------------------------------------------------


async def schedule_broadcast(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    broadcast_id: UUID,
    actor_user_id: UUID | None,
) -> int:
    """Materialise recipients + ZADD. Returns total_recipients.

    Atomicity invariants:
      - Audience compile + INSERT...SELECT happen inside the caller's
        existing request transaction.
      - Quota check happens AFTER materialisation (we know the count
        only after the SELECT runs) but INSIDE the transaction — over-
        quota -> ROLLBACK -> zero recipient rows persisted.
      - Status flip draft->scheduled happens AFTER the count check.
      - Redis ZADD happens OUTSIDE the transaction. If Redis fails
        after the DB commit, the worker can be kicked manually via an
        admin tool — broadcasts.status='scheduled' is the
        source-of-truth; the ZSET is a hot-path index only.
    """
    row = await conn.fetchrow(
        """
        SELECT b.id, b.audience_filter, b.scheduled_at, b.status,
               b.tenant_channel_id, t.status AS template_status
          FROM broadcasts b
          JOIN tenant_templates t ON t.id = b.template_id
         WHERE b.id = $1
        """,
        broadcast_id,
    )
    if row is None:
        raise BroadcastError(f"broadcast {broadcast_id} not found")
    if row["status"] != "draft":
        raise BroadcastStateInvalidError(
            f"broadcast {broadcast_id} status={row['status']!r}; can only "
            "schedule from 'draft'"
        )
    if row["template_status"] != "approved":
        raise BroadcastTemplateNotApprovedError(
            f"template no longer approved (status={row['template_status']!r})"
        )

    # Compile filter. The stored audience_filter is JSONB; we re-parse
    # it through Pydantic to recover the discriminated-union type.
    af_json = row["audience_filter"]
    if isinstance(af_json, str):
        af_dict = json.loads(af_json)
    else:
        af_dict = af_json

    from pydantic import TypeAdapter

    # TypeAdapter on a discriminated Union returns the resolved variant
    # (one of FilterAllOptedIn / FilterTagsAny / ... / FilterCombinedAnd);
    # the compiler accepts any of them under the AudienceFilter alias.
    af: AudienceFilter = TypeAdapter(AudienceFilter).validate_python(af_dict)
    where_sql, where_params = compile_audience_filter(af, starting_param_idx=3)

    # Materialise recipients. $1=broadcast_id, $2=tenant_id, then the
    # compiler-supplied params start at $3. where_sql is composed by
    # compile_audience_filter from a fixed allowlist of clause templates
    # in services/audience/filter.py — every value flows through
    # ${N} placeholders, never string-interpolation. No SQL injection.
    insert_sql = f"""
        INSERT INTO broadcast_recipients
            (id, broadcast_id, tenant_id, audience_member_id, channel,
             channel_user_id, status, created_at, updated_at)
        SELECT gen_random_uuid(), $1, $2, am.id, am.channel,
               am.channel_user_id, 'pending', now(), now()
          FROM audience_members am
         WHERE am.tenant_id = $2
           AND am.opted_out_at IS NULL
           AND ({where_sql})
    """  # noqa: S608 — compiler-allowlisted clause templates; values are placeholders
    inserted = await conn.execute(
        insert_sql,
        broadcast_id,
        tenant_id,
        *where_params,
    )
    # asyncpg execute() returns "INSERT 0 N" for INSERTs.
    inserted_n = int(inserted.split()[-1]) if inserted.startswith("INSERT") else 0

    if inserted_n == 0:
        raise BroadcastAudienceEmptyError(
            f"audience_filter matched zero opted-in members for tenant {tenant_id}"
        )

    # Quota check INSIDE the transaction. Compute period-to-date
    # broadcast_recipients usage (rolling 30 days; ADR-026 Phase 10 will
    # replace this with the proper billing-period boundary).
    used = await conn.fetchval(
        """
        SELECT COALESCE(SUM(total_recipients), 0)
          FROM broadcasts
         WHERE tenant_id = $1
           AND status IN ('scheduled', 'running', 'completed')
           AND created_at >= now() - INTERVAL '30 days'
        """,
        tenant_id,
    )
    cap = await conn.fetchval(
        "SELECT monthly_broadcast_recipients FROM tenant_quotas WHERE tenant_id = $1",
        tenant_id,
    )
    projected = int(used or 0) + inserted_n
    if cap is not None and projected > int(cap):
        # Raise BEFORE flipping status; caller's transaction wrapper
        # rolls back and the INSERTed recipient rows vanish.
        raise BroadcastQuotaExceededError(
            f"would exceed monthly_broadcast_recipients: used={used} + "
            f"new={inserted_n} > cap={cap}"
        )

    # Flip status + populate counters.
    await conn.execute(
        """
        UPDATE broadcasts
           SET status = 'scheduled',
               total_recipients = $2,
               scheduled_at = COALESCE(scheduled_at, now()),
               updated_at = now()
         WHERE id = $1
        """,
        broadcast_id,
        inserted_n,
    )

    await audit.record(
        conn,
        tenant_id,
        actor_user_id,
        "broadcast.scheduled",
        before=None,
        after={
            "broadcast_id": str(broadcast_id),
            "total_recipients": inserted_n,
        },
    )

    # ZADD outside the txn boundary. Read scheduled_at fresh in case
    # the UPDATE just set it via COALESCE.
    final_sched = await conn.fetchval(
        "SELECT scheduled_at FROM broadcasts WHERE id = $1", broadcast_id
    )
    redis = get_redis()
    score = final_sched.timestamp() if final_sched else 0.0
    await redis.zadd(_queue_key(tenant_id), {str(broadcast_id): score})

    return inserted_n


# ---------------------------------------------------------------------
# pause / resume / cancel
# ---------------------------------------------------------------------


async def pause_broadcast(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    broadcast_id: UUID,
    actor_user_id: UUID | None,
) -> None:
    """ZREM + status='paused'. Worker bails on its next status check."""
    row = await conn.fetchrow(
        "SELECT status FROM broadcasts WHERE id = $1",
        broadcast_id,
    )
    if row is None:
        raise BroadcastError(f"broadcast {broadcast_id} not found")
    if row["status"] not in ("scheduled", "running"):
        raise BroadcastStateInvalidError(
            f"cannot pause broadcast in status={row['status']!r}"
        )

    await conn.execute(
        """
        UPDATE broadcasts
           SET status = 'paused', updated_at = now()
         WHERE id = $1
        """,
        broadcast_id,
    )
    await audit.record(
        conn,
        tenant_id,
        actor_user_id,
        "broadcast.paused",
        before={"status": row["status"]},
        after={"status": "paused"},
    )

    redis = get_redis()
    await redis.zrem(_queue_key(tenant_id), str(broadcast_id))


async def resume_broadcast(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    broadcast_id: UUID,
    actor_user_id: UUID | None,
) -> None:
    """Status='paused' -> 'scheduled'; ZADD with original scheduled_at."""
    row = await conn.fetchrow(
        "SELECT status, scheduled_at FROM broadcasts WHERE id = $1",
        broadcast_id,
    )
    if row is None:
        raise BroadcastError(f"broadcast {broadcast_id} not found")
    if row["status"] != "paused":
        raise BroadcastStateInvalidError(
            f"cannot resume broadcast in status={row['status']!r}"
        )

    await conn.execute(
        """
        UPDATE broadcasts
           SET status = 'scheduled', updated_at = now()
         WHERE id = $1
        """,
        broadcast_id,
    )
    await audit.record(
        conn,
        tenant_id,
        actor_user_id,
        "broadcast.resumed",
        before={"status": "paused"},
        after={"status": "scheduled"},
    )

    redis = get_redis()
    score = row["scheduled_at"].timestamp() if row["scheduled_at"] else 0.0
    await redis.zadd(_queue_key(tenant_id), {str(broadcast_id): score})


async def cancel_broadcast(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    broadcast_id: UUID,
    actor_user_id: UUID | None,
) -> None:
    """ZREM + status='cancelled' + flip pending recipients -> skipped.

    Recipients already in 'sent' / 'delivered' / 'read' stay as-is —
    the actual sends happened and can't be unsent. The Meta-side
    delivery status webhook will continue to flow in and update
    those rows naturally.
    """
    row = await conn.fetchrow(
        "SELECT status FROM broadcasts WHERE id = $1",
        broadcast_id,
    )
    if row is None:
        raise BroadcastError(f"broadcast {broadcast_id} not found")
    if row["status"] in ("completed", "cancelled", "errored"):
        raise BroadcastStateInvalidError(
            f"cannot cancel broadcast in terminal status={row['status']!r}"
        )

    await conn.execute(
        """
        UPDATE broadcasts
           SET status = 'cancelled',
               finished_at = COALESCE(finished_at, now()),
               started_at = COALESCE(started_at, now()),
               updated_at = now()
         WHERE id = $1
        """,
        broadcast_id,
    )
    skip_result = await conn.execute(
        """
        UPDATE broadcast_recipients
           SET status = 'skipped',
               skip_reason = 'cancelled',
               updated_at = now()
         WHERE broadcast_id = $1 AND status = 'pending'
        """,
        broadcast_id,
    )
    skipped_n = (
        int(skip_result.split()[-1]) if skip_result.startswith("UPDATE") else 0
    )

    await conn.execute(
        """
        UPDATE broadcasts
           SET skipped_count = skipped_count + $2,
               updated_at = now()
         WHERE id = $1
        """,
        broadcast_id,
        skipped_n,
    )

    await audit.record(
        conn,
        tenant_id,
        actor_user_id,
        "broadcast.cancelled",
        before={"status": row["status"]},
        after={"status": "cancelled", "skipped_now": skipped_n},
    )

    redis = get_redis()
    await redis.zrem(_queue_key(tenant_id), str(broadcast_id))
