"""One-shot DB connection check — Phase 4 smoke prereq.

Verifies that POLARIS_DB_URL_ANON + POLARIS_DB_URL_SERVICE both resolve,
connect, and authenticate. Tells you the actual failure mode (DNS vs
auth vs SSL) instead of dumping the asyncpg traceback.

Usage:
  uv run python -m apps.api.scripts.check_db_connections
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import asyncpg  # noqa: E402

try:
    from dotenv import load_dotenv  # noqa: E402
    load_dotenv(_REPO_ROOT / ".env")
except ImportError:
    # python-dotenv isn't a runtime dep; if missing, fall back to OS env
    pass


async def _check(name: str, dsn: str | None) -> bool:
    if not dsn:
        print(f"  ✗ {name}: NOT SET in .env")
        return False
    # Sanitised display — strip the password before printing
    safe = dsn
    if "@" in dsn and ":" in dsn.split("@")[0]:
        scheme_user, _, host_db = dsn.partition("@")
        scheme, _, user_pw = scheme_user.rpartition("//")
        user, _, _pw = user_pw.partition(":")
        safe = f"{scheme}//{user}:<HIDDEN>@{host_db}"
    print(f"  → {name}")
    print(f"     dsn: {safe}")
    try:
        conn = await asyncpg.connect(dsn, timeout=10.0)
        row = await conn.fetchrow("SELECT current_user AS u, current_database() AS db")
        await conn.close()
        print(f"     ✓ OK  (logged in as {row['u']!r}, db={row['db']!r})")
        return True
    except OSError as exc:
        print(f"     ✗ NETWORK: {type(exc).__name__}: {exc}")
        print("     → DNS / IPv4-IPv6 / firewall problem. Try the Session pooler host:")
        print("       aws-1-<region>.pooler.supabase.com:5432")
        return False
    except asyncpg.InvalidPasswordError:
        print("     ✗ AUTH: invalid password")
        print("     → Re-run in Supabase SQL Editor:")
        print("       ALTER ROLE <role> WITH PASSWORD '<paste-fresh>';")
        print("     → Then update the password segment in the DSN in .env.")
        return False
    except asyncpg.InvalidAuthorizationSpecificationError as exc:
        print(f"     ✗ AUTH: {exc}")
        print("     → Username format for Supabase Session pooler is <role>.<project-ref>")
        print("     → e.g. polaris_anon.wvkrivpwccjumhqygjqh")
        return False
    except Exception as exc:  # noqa: BLE001
        print(f"     ✗ OTHER: {type(exc).__name__}: {exc}")
        return False


async def main() -> int:
    print("Checking DB connections from .env...\n")
    anon_ok = await _check("POLARIS_DB_URL_ANON", os.environ.get("POLARIS_DB_URL_ANON"))
    print()
    svc_ok = await _check("POLARIS_DB_URL_SERVICE", os.environ.get("POLARIS_DB_URL_SERVICE"))
    print()
    if anon_ok and svc_ok:
        print("✅ Both DSNs work. You can run:")
        print("   uv run python -m apps.api.scripts.seed_demo_knowledge")
        return 0
    print("❌ Fix the failures above before running the seed/smoke.")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
