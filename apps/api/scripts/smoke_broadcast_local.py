"""Local end-to-end broadcast smoke — Phase 6 PR-9.

Drives the full Phase 6 outbound lifecycle against LOCAL Postgres +
Redis with a MOCKED meta_whatsapp client. No real WABA required — use
this to verify the broadcast worker before your Meta sandbox / Chatwoot
linkage is ready.

What this verifies end-to-end:
  * Real Postgres schema (migrations 001-047 applied)
  * Real Redis broadcast queue (ZADD + worker SCAN/ZREM)
  * tenant_templates + broadcasts + broadcast_recipients lifecycle
  * Compliance gate (opted_out + 24h-window via Phase 6/PR-6)
  * Worker drain loop with per-recipient send + status flip
  * Outbound event row persistence (broadcast.completed)
  * Tenant cleanup via ON DELETE CASCADE

What this does NOT verify:
  * Real Meta Cloud API send (meta_whatsapp.send_template is mocked)
  * wa-status delivery webhook ingress (PR-7 — needs Meta-side trigger)
  * wa-account onboarding (PR-7 — needs Meta-side trigger)
  * Operator smoke against +60 11-5458 1688 (separate runbook step
    once WABA + Chatwoot are linked)

Prerequisites:
  1. `make migrate` applied through migration 047
  2. .env has POLARIS_DB_URL_SERVICE pointing at a local-or-Supabase DB
     you can write to (the script seeds a temporary tenant + cleans up)
  3. .env has REDIS_URL pointing at a reachable Redis (compose default
     redis://:devpassword@localhost:6379/0)
  4. .env has POLARIS_KMS_KEY set (required by tenant_secrets put)

Usage:
  uv run python -m apps.api.scripts.smoke_broadcast_local
  uv run python -m apps.api.scripts.smoke_broadcast_local --recipients 50
  uv run python -m apps.api.scripts.smoke_broadcast_local --recipients 1000 --verbose
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
from typing import Final
from unittest.mock import patch
from uuid import UUID, uuid4

from polaris_api.config import get_settings
from polaris_api.db.pools import close_pools, get_service_pool, init_pools
from polaris_api.redis_pool import close_redis, get_redis, init_redis
from polaris_api.services import secrets
from polaris_api.services.broadcast import service as broadcast_service
from polaris_api.services.broadcast import worker as broadcast_worker
from polaris_shared_types import CreateBroadcastRequest, FilterAllOptedIn

# Reuse the shared seed helpers from the perf harness; same code path,
# same assertions about what a "complete tenant scaffolding" looks like.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tests"))
from outbound.perf._seed import (  # noqa: E402
    cleanup_tenant,
    seed_approved_template,
    seed_audience,
    seed_channel,
    seed_tenant,
)

logger = logging.getLogger("smoke_broadcast_local")

_DEFAULT_RECIPIENTS: Final[int] = 25
_MAX_DRAIN_SEC: Final[float] = 300.0  # hard cap so a smoke run never hangs


# -----------------------------------------------------------------------------
# Output formatting
# -----------------------------------------------------------------------------


_OK = "[ ok ]"
_FAIL = "[fail]"
_INFO = "[info]"


def _say(symbol: str, msg: str) -> None:
    """Single-line status output. No emojis (host terminals on Windows
    don't render them reliably from a uv-run subprocess)."""
    print(f"{symbol} {msg}", flush=True)


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="smoke_broadcast_local",
        description=(
            "End-to-end Phase 6 broadcast smoke against local Postgres + "
            "Redis with mocked Meta. No WABA required."
        ),
    )
    p.add_argument(
        "--recipients",
        type=int,
        default=_DEFAULT_RECIPIENTS,
        help=f"how many opted-in audience members to seed (default {_DEFAULT_RECIPIENTS})",
    )
    p.add_argument(
        "--keep",
        action="store_true",
        help=(
            "don't clean up the seeded tenant on completion — useful for "
            "inspecting state via psql afterwards"
        ),
    )
    p.add_argument(
        "--verbose",
        action="store_true",
        help="DEBUG-level logging for worker + service",
    )
    return p


# -----------------------------------------------------------------------------
# Stubbed Meta client
# -----------------------------------------------------------------------------


def _build_send_template_stub() -> _TemplateStub:
    """Return a monotonic-counting stub that mirrors send_template's
    signature. Each call returns a fresh wamid so the broadcast_
    recipients_wamid_idx UNIQUE constraint stays happy."""
    return _TemplateStub()


class _TemplateStub:
    """Captures call count + emits deterministic wamids."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, **kwargs: object) -> str:  # noqa: ARG002
        self.calls += 1
        return f"wamid.smoke.{uuid4().hex[:12]}.{self.calls:06d}"


# -----------------------------------------------------------------------------
# Smoke orchestration
# -----------------------------------------------------------------------------


async def _smoke(args: argparse.Namespace) -> int:
    pool = get_service_pool()

    # Seed scaffolding.
    async with pool.acquire() as conn:
        slug = f"smoke-{uuid4().hex[:8]}"
        _say(_INFO, f"seeding tenant slug={slug}")
        tenant_id = await seed_tenant(conn, slug=slug)
        inbox_id = int(time.time() * 1000) % 2_000_000_000
        channel_id = await seed_channel(
            conn,
            tenant_id=tenant_id,
            chatwoot_inbox_id=inbox_id,
        )
        template_id = await seed_approved_template(
            conn,
            tenant_id=tenant_id,
            tenant_channel_id=channel_id,
        )
        _say(_OK, f"seeded tenant={tenant_id} channel={channel_id} template={template_id}")

        _say(_INFO, f"bulk-inserting {args.recipients} audience members…")
        t_seed_start = time.perf_counter()
        await seed_audience(conn, tenant_id=tenant_id, count=args.recipients)
        t_seed_elapsed = time.perf_counter() - t_seed_start
        _say(_OK, f"audience seeded in {t_seed_elapsed:.2f}s")

        # Park a fake WABA token under tenant_secrets so worker._get_token
        # has something to decrypt. The mocked send_template ignores it.
        try:
            await secrets.put(
                conn,
                tenant_id,
                "meta_waba_access_token",
                b"SMOKE_FAKE_TOKEN",
            )
        except Exception as exc:  # noqa: BLE001
            _say(_FAIL, f"tenant_secrets.put failed: {exc}")
            _say(_INFO, "is POLARIS_KMS_KEY set? Required by the AEAD layer.")
            if not args.keep:
                await cleanup_tenant(conn, tenant_id=tenant_id)
            return 2

    # Create + schedule the broadcast via the same service the router uses.
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(
            "SELECT set_config('app.current_tenant_id', $1, true)",
            str(tenant_id),
        )
        req = CreateBroadcastRequest(
            name=f"smoke-{uuid4().hex[:8]}",
            tenant_channel_id=channel_id,
            template_id=template_id,
            audience_filter=FilterAllOptedIn(filter_kind="all_opted_in"),
            variables_template={"1": "Friend"},
            scheduled_at=None,
        )
        broadcast_id = await broadcast_service.create_broadcast(
            conn,
            tenant_id=tenant_id,
            actor_user_id=None,
            req=req,
        )
        _say(_OK, f"broadcast created id={broadcast_id} status=draft")

        total = await broadcast_service.schedule_broadcast(
            conn,
            tenant_id=tenant_id,
            broadcast_id=broadcast_id,
            actor_user_id=None,
        )
        _say(_OK, f"broadcast scheduled; materialised {total} recipients + ZADD'd")

    # Stub meta_whatsapp.send_template + drive the worker.
    stub = _build_send_template_stub()
    with patch(
        "polaris_api.services.meta_whatsapp.MetaWhatsAppClient.send_template",
        new=stub,
    ):
        _say(_INFO, "draining via worker._tick_once()… (mocked Meta send)")
        t_drain_start = time.perf_counter()
        final_status = await _drain_until_terminal(tenant_id, broadcast_id)
        t_drain_elapsed = time.perf_counter() - t_drain_start
        _say(
            _OK if final_status == "completed" else _FAIL,
            f"drain finished: status={final_status} "
            f"elapsed={t_drain_elapsed:.2f}s "
            f"meta_send_calls={stub.calls}",
        )

    # Report counters + outbound events.
    async with pool.acquire() as conn:
        counters = await conn.fetchrow(
            """
            SELECT total_recipients, sent_count, delivered_count,
                   read_count, failed_count, skipped_count, error
              FROM broadcasts
             WHERE id = $1
            """,
            broadcast_id,
        )
        outbound_events = await conn.fetch(
            """
            SELECT event_type, created_at
              FROM webhook_events_outbound
             WHERE tenant_id = $1
             ORDER BY created_at
            """,
            tenant_id,
        )

    _say(_INFO, f"final broadcasts counters: {dict(counters) if counters else None}")
    _say(
        _INFO,
        f"webhook_events_outbound rows: "
        f"{[(e['event_type'], e['created_at'].isoformat()) for e in outbound_events]}",
    )

    exit_code = 0
    if final_status != "completed":
        _say(_FAIL, "broadcast did not reach 'completed' status")
        exit_code = 1
    elif counters is None or counters["sent_count"] != args.recipients:
        _say(
            _FAIL,
            f"sent_count mismatch: expected {args.recipients}, got "
            f"{counters['sent_count'] if counters else None}",
        )
        exit_code = 1
    else:
        _say(_OK, "all recipients reached sent state — smoke PASS")

    # Cleanup — uses SUPABASE_DB_URL (postgres superuser) because
    # polaris_service lacks DELETE on audit_logs (immutable by design).
    if not args.keep:
        await _cleanup_as_superuser(tenant_id)
        _say(_INFO, f"tenant {tenant_id} cleaned up (use --keep to inspect)")
    else:
        _say(_INFO, f"tenant {tenant_id} retained (--keep was set)")

    return exit_code


def _read_supabase_db_url() -> str | None:
    """Read SUPABASE_DB_URL from .env without pydantic-settings.

    Sync helper so the caller (an async function) avoids ASYNC240 —
    pathlib.Path methods must not be called inside async def.
    """
    from pathlib import Path as _Path

    env_file = _Path(".env")
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if line.startswith("SUPABASE_DB_URL="):
                return line.split("=", 1)[1].strip()
    return None


async def _cleanup_as_superuser(tenant_id: UUID) -> None:
    """Delete the smoke tenant using the superuser DSN.

    polaris_service has INSERT but not DELETE on audit_logs (immutable by
    design), so we connect as postgres for teardown only.  Falls back to
    a warning if SUPABASE_DB_URL is not configured.
    """

    su_url = _read_supabase_db_url()

    if not su_url:
        _say(_INFO, "SUPABASE_DB_URL not set — smoke tenant NOT cleaned up; delete manually")
        return

    # ssl='require' encrypts the connection without verifying the Supabase
    # pooler's self-signed cert chain (same posture as the rest of the codebase).
    import asyncpg as _asyncpg
    conn = await _asyncpg.connect(su_url, ssl='require')
    try:
        await cleanup_tenant(conn, tenant_id=tenant_id)
    finally:
        await conn.close()


async def _drain_until_terminal(
    tenant_id: UUID,  # noqa: ARG001 — kept for symmetry with perf harness
    broadcast_id: UUID,
) -> str:
    """Pump _tick_once() until terminal or deadline."""
    pool = get_service_pool()
    deadline = time.perf_counter() + _MAX_DRAIN_SEC
    last_log_at = 0.0
    while time.perf_counter() < deadline:
        await broadcast_worker._tick_once()  # noqa: SLF001 — smoke entry point

        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT status, sent_count, total_recipients
                  FROM broadcasts WHERE id = $1
                """,
                broadcast_id,
            )
        if row is None:
            return "MISSING"
        if row["status"] in ("completed", "cancelled", "errored"):
            return str(row["status"])

        # Progress log every ~3s.
        now = time.perf_counter()
        if now - last_log_at > 3.0:
            _say(
                _INFO,
                f"  progress: sent={row['sent_count']}/{row['total_recipients']} "
                f"status={row['status']}",
            )
            last_log_at = now

        await asyncio.sleep(0.1)

    return "TIMEOUT"


# -----------------------------------------------------------------------------
# Entry
# -----------------------------------------------------------------------------


async def _main() -> int:
    parser = _build_parser()
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    _settings = get_settings()
    # services/secrets.py reads POLARIS_KMS_KEY directly from os.environ (not
    # pydantic-settings), so bridge the .env value across for standalone runs.
    if _settings.polaris_kms_key and not os.environ.get("POLARIS_KMS_KEY"):
        os.environ["POLARIS_KMS_KEY"] = _settings.polaris_kms_key
    if not _settings.polaris_db_url_service:
        _say(_FAIL, "POLARIS_DB_URL_SERVICE not set — set it in .env first")
        return 2
    if not _settings.redis_url or _settings.redis_url == "redis://localhost:6379":
        # redis_url has a non-empty default, so also warn if it's still the bare
        # default (no password) — the compose stack requires a password.
        pass  # allow default; pools.init_redis will fail fast if unreachable
    if not _settings.polaris_kms_key:
        _say(_FAIL, "POLARIS_KMS_KEY not set — required by secrets.put (AEAD layer)")
        return 2

    _say(_INFO, "bootstrapping pools (Postgres + Redis)…")
    await init_pools()
    await init_redis()
    try:
        # Cheap sanity: SELECT 1 against the DB, PING Redis.
        async with get_service_pool().acquire() as conn:
            await conn.fetchval("SELECT 1")
        await get_redis().ping()
        _say(_OK, "pools reachable")
    except Exception as exc:  # noqa: BLE001
        _say(_FAIL, f"pool bootstrap failed: {exc}")
        await close_redis()
        await close_pools()
        return 2

    try:
        return await _smoke(args)
    finally:
        await close_redis()
        await close_pools()


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(_main()))
    except KeyboardInterrupt:
        _say(_INFO, "interrupted")
        sys.exit(130)
