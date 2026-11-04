"""Hash-chained append-only audit log — Phase 1 PR-4.

Each row's `row_hash` is `SHA-256(canonical_json(prev_hash || tenant_id ||
actor || action || before || after || created_at))`. `prev_hash` is the
previous row's `row_hash` for the same tenant (NULL for the first row).
The chain is per-tenant — each tenant has its own independent chain.

Concurrency
-----------
Two concurrent `record()` calls for the same tenant would both read the
same "previous row" and compute the same `prev_hash`, producing a
silently-broken chain. To prevent this we take a per-tenant
`pg_advisory_xact_lock` keyed on `tenant_id` at the start of `record()`.
The lock releases at transaction end. Different tenants don't contend.

Transactional contract
----------------------
The caller MUST pass an `asyncpg.Connection` that is already inside an
open transaction — the audit insert lives or dies with the audited action.
Phase 1 uses `TenantContext.get_request_conn` (PR-2), which holds the
request-scoped transaction; PR-4 handlers call `record()` against that
same connection so the audit and the action commit atomically.

Verification
------------
`verify_chain()` walks a tenant's chain in `(created_at, id)` order. For
each row it re-computes `row_hash` from the content fields and asserts:
  - the row's `prev_hash` matches the previous row's `row_hash`
  - the row's stored `row_hash` matches the recomputed hash

First mismatch returns `(False, broken_row_id)`. A clean walk returns
`(True, None)`. Verification reads via `polaris_service` (BYPASSRLS); the
tenant role has no SELECT grant on `audit_logs` per ADR-030.

Canonical JSON
--------------
`json.dumps(value, sort_keys=True, separators=(",", ":"))` produces the
canonical form used at both write and verify. Changing this serialiser
silently breaks every existing chain — guarded by `test_audit_chain.py`
pinning the byte output for a fixed payload.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import asyncpg

_LOCK_NAMESPACE = "polaris.audit"


def _canonical_json(value: Any) -> bytes:
    """Sorted-keys, no-whitespace, UTF-8 JSON. Stable across runs.

    `default=str` lets datetime + UUID round-trip via their str() form —
    callers normally pre-stringify those, but this is a belt-and-braces
    guard so an accidental UUID in `before`/`after` doesn't raise.
    """
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")


def compute_row_hash(
    prev_hash: bytes | None,
    tenant_id: UUID,
    actor: UUID | None,
    action: str,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    created_at: datetime,
) -> bytes:
    """SHA-256 over a canonical projection of the row's content fields.

    Excludes `id` (random per row; doesn't carry integrity signal).
    Includes everything else — tamper any field and `verify_chain` flags
    the row.
    """
    parts = {
        "prev_hash": prev_hash.hex() if prev_hash is not None else None,
        "tenant_id": str(tenant_id),
        "actor": str(actor) if actor is not None else None,
        "action": action,
        "before": before,
        "after": after,
        "created_at": created_at.isoformat(),
    }
    return hashlib.sha256(_canonical_json(parts)).digest()


async def record(
    conn: asyncpg.Connection,
    tenant_id: UUID,
    actor: UUID | None,
    action: str,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    *,
    impersonating_kind: str | None = None,
    impersonating_id: UUID | None = None,
) -> UUID:
    """Append an audit-log row. Returns the new row's id.

    Caller is responsible for:
      - providing an asyncpg.Connection inside an open transaction
      - having `app.current_tenant_id` set to `tenant_id` if the
        connection runs as `polaris_anon` (the RLS INSERT policy gates
        cross-tenant writes via WITH CHECK)

    Takes a per-tenant advisory lock so concurrent `record()` calls for
    the same tenant serialise on the chain head. Released at tx end.

    Phase 9 PR-5: optional impersonating_kind / impersonating_id kwargs
    map to audit_logs columns of the same name (migration 056). When None
    the columns stay NULL.
    """
    if not action or len(action) > 200:
        # Schema CHECK would catch this, but failing fast in app code
        # gives a clearer error than asyncpg's "violates check constraint".
        raise ValueError("action must be 1..200 chars")

    # Per-tenant lock. hashtext() produces a stable int4 hash; collisions
    # across tenants just mean two unrelated tenants briefly serialise on
    # each other — harmless. The lock auto-releases at transaction end.
    await conn.execute(
        "SELECT pg_advisory_xact_lock(hashtext($1))",
        f"{_LOCK_NAMESPACE}:{tenant_id}",
    )

    # Chain-head lookup via SECURITY DEFINER function — polaris_anon has no
    # SELECT grant on audit_logs (ADR-030: tenant role never reads the table
    # directly). The function lives in 012_audit_grants.sql; both anon +
    # service have EXECUTE on it, no one else does.
    prev_hash_row = await conn.fetchval(
        "SELECT audit_log_prev_hash($1)", tenant_id
    )
    prev_hash: bytes | None = bytes(prev_hash_row) if prev_hash_row is not None else None

    created_at = datetime.now(UTC)
    row_hash = compute_row_hash(
        prev_hash, tenant_id, actor, action, before, after, created_at
    )

    # Pass dicts directly — pools.py registers the JSONB codec (encoder=json.dumps)
    # so asyncpg encodes dict→JSON object, not the double-encoded string that
    # json.dumps(dict)→str→JSONB string would produce.
    new_id = uuid4()
    await conn.execute(
        """
        INSERT INTO audit_logs
            (id, tenant_id, actor, action, before, after, prev_hash, row_hash, created_at,
             impersonating_kind, impersonating_id)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
        """,
        new_id,
        tenant_id,
        actor,
        action,
        before,
        after,
        prev_hash,
        row_hash,
        created_at,
        impersonating_kind,
        impersonating_id,
    )
    return new_id


async def verify_chain(
    conn: asyncpg.Connection,
    tenant_id: UUID,
) -> tuple[bool, UUID | None]:
    """Walk the chain. Returns `(True, None)` if intact, else `(False, id)`.

    Reads require `polaris_service` (no SELECT grant to `polaris_anon`).
    Empty tenant returns `(True, None)` — vacuous truth.
    """
    rows = await conn.fetch(
        "SELECT id, actor, action, before, after, prev_hash, row_hash, created_at "
        "FROM audit_logs "
        "WHERE tenant_id = $1 "
        "ORDER BY created_at ASC, id ASC",
        tenant_id,
    )

    expected_prev: bytes | None = None
    for row in rows:
        actual_prev = (
            bytes(row["prev_hash"]) if row["prev_hash"] is not None else None
        )
        if actual_prev != expected_prev:
            return False, row["id"]

        # Rows inserted before the double-encoding fix have JSONB string values;
        # normalize them to dicts so compute_row_hash produces the correct hash.
        before = json.loads(row["before"]) if isinstance(row["before"], str) else row["before"]
        after = json.loads(row["after"]) if isinstance(row["after"], str) else row["after"]
        recomputed = compute_row_hash(
            actual_prev,
            tenant_id,
            row["actor"],
            row["action"],
            before,
            after,
            row["created_at"],
        )
        if recomputed != bytes(row["row_hash"]):
            return False, row["id"]

        expected_prev = bytes(row["row_hash"])

    return True, None
