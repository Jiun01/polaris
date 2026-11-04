"""One-shot CLI — seed the platform_health KMS sentinel row.

Run once per environment after `make migrate` lands migrations 005 + 005b.
Re-run whenever you intentionally rotate `POLARIS_KMS_KEY` (combine with
re-encryption of all tenant_secrets rows under the new key — separate
maintenance script, not part of PR-3).

Usage:
    uv run --env-file .env python apps/api/scripts/seed_kms_sentinel.py

Exit codes:
    0  — sentinel seeded (or overwritten).
    1  — failure (env misconfig, DB connect, encrypt). stderr carries why.

Uses `service_pool` (BYPASSRLS) because `platform_health` has no GRANT to
`polaris_anon` — it is platform-only by design.
"""

from __future__ import annotations

import asyncio
import sys

from polaris_api.db.pools import close_pools, get_service_pool, init_pools
from polaris_api.services.kms_sentinel import seed_sentinel


async def _main() -> int:
    try:
        await init_pools()
    except Exception as exc:  # noqa: BLE001
        print(f"seed_kms_sentinel: init_pools failed: {exc}", file=sys.stderr)
        return 1
    try:
        async with get_service_pool().acquire() as conn:
            await seed_sentinel(conn)
        print("KMS sentinel seeded (platform_health row id=1 written).")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"seed_kms_sentinel: failed: {exc}", file=sys.stderr)
        return 1
    finally:
        await close_pools()


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
