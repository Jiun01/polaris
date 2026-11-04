"""Sync Supabase Postgres role passwords to match `.env` DSNs.

Single source of truth = `.env`. This script parses the passwords
out of POLARIS_DB_URL_ANON + POLARIS_DB_URL_SERVICE, connects as
the superuser via SUPABASE_DB_URL, and runs:

    ALTER ROLE polaris_anon    WITH PASSWORD '<from .env>';
    ALTER ROLE polaris_service WITH PASSWORD '<from .env>';

Then re-verifies by reconnecting through the pooler endpoints —
catches both typo drift (passwords genuinely don't match) and
Supavisor credential-cache lag (verification fails immediately
after the ALTER → wait + retry, or kick the project's "Restart
Database" if persistent).

Usage:
    uv run --env-file .env python -m apps.api.scripts.sync_role_passwords

Exit codes:
    0 = both roles synced + verified through the pooler
    1 = sync ran but post-sync auth still fails (likely pooler cache)
    2 = prerequisite missing (env vars or roles don't exist)
    3 = unexpected exception
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import urllib.parse
from pathlib import Path

import asyncpg

# Make `from polaris_api.*` resolve when invoked as a script.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

logger = logging.getLogger("sync_role_passwords")

_ROLES = ("polaris_anon", "polaris_service")
_ENV_KEY_BY_ROLE = {
    "polaris_anon": "POLARIS_DB_URL_ANON",
    "polaris_service": "POLARIS_DB_URL_SERVICE",
}


def _password_from_dsn(dsn: str) -> str | None:
    """Extract the URL-decoded password from a postgres:// DSN."""
    parsed = urllib.parse.urlparse(dsn)
    if not parsed.password:
        return None
    return urllib.parse.unquote(parsed.password)


async def _role_exists(conn: asyncpg.Connection, role: str) -> bool:
    row = await conn.fetchval("SELECT 1 FROM pg_roles WHERE rolname = $1", role)
    return row is not None


async def _alter_role_password(
    conn: asyncpg.Connection, role: str, password: str,
) -> None:
    # ALTER ROLE doesn't support parameter binding for the role name OR
    # the password literal. Role name is hard-coded (allowlist), password
    # is escaped via PostgreSQL's E'\\x' literal-safe form.
    if role not in _ROLES:
        raise ValueError(f"refusing ALTER ROLE on non-allowlisted role: {role!r}")
    # Postgres password literal: wrap in single quotes, escape embedded
    # single quotes by doubling them. No other escapes needed for
    # ALTER ROLE ... WITH PASSWORD '...'.
    safe_pw = password.replace("'", "''")
    await conn.execute(f"ALTER ROLE {role} WITH PASSWORD '{safe_pw}'")  # noqa: S608


async def _verify_pooler_auth(env_key: str, role: str) -> bool:
    dsn = os.environ.get(env_key)
    if not dsn:
        logger.error("verify: %s is unset", env_key)
        return False
    try:
        conn = await asyncpg.connect(dsn)
    except asyncpg.InvalidPasswordError:
        logger.error(
            "verify FAIL via pooler: %s — password rejected by Supavisor",
            role,
        )
        return False
    except asyncpg.InternalServerError as exc:
        # Supavisor returns InternalServerError "(ENOTFOUND) tenant/user ... not found"
        # when its credential cache hasn't loaded the role yet.  Treat as retryable.
        logger.warning("verify RETRY-NEEDED via pooler: %s — %s", role, exc)
        return False
    except OSError as exc:
        logger.warning("verify RETRY-NEEDED via pooler: %s — network: %s", role, exc)
        return False
    try:
        actual = await conn.fetchval("SELECT current_user")
    finally:
        await conn.close()
    if actual != role:
        logger.error("verify: connected as %s, expected %s", actual, role)
        return False
    logger.info("verify OK via pooler: %s", role)
    return True


async def _amain() -> int:
    su_dsn = os.environ.get("SUPABASE_DB_URL")
    if not su_dsn:
        logger.error("SUPABASE_DB_URL unset — needed as the superuser entry point")
        return 2

    passwords: dict[str, str] = {}
    for role, env_key in _ENV_KEY_BY_ROLE.items():
        dsn = os.environ.get(env_key)
        if not dsn:
            logger.error("%s unset — can't determine target password for %s", env_key, role)
            return 2
        pw = _password_from_dsn(dsn)
        if not pw:
            logger.error("could not parse password from %s", env_key)
            return 2
        passwords[role] = pw

    try:
        conn = await asyncpg.connect(su_dsn)
    except Exception as exc:  # noqa: BLE001
        logger.error("SUPABASE_DB_URL connect failed: %s", exc)
        return 2

    try:
        for role in _ROLES:
            if not await _role_exists(conn, role):
                logger.error(
                    "role %s does not exist — run migration 010 first via "
                    "`uv run python packages/db-migrations/apply.py`",
                    role,
                )
                return 2
            await _alter_role_password(conn, role, passwords[role])
            logger.info("ALTER ROLE %s WITH PASSWORD '<from %s>'", role, _ENV_KEY_BY_ROLE[role])
    finally:
        await conn.close()

    # Post-sync verification through the pooler. Supavisor may take up to
    # 60–90 s to refresh its credential cache after ALTER ROLE; if it hasn't
    # yet, it returns InternalServerError "tenant/user not found".  Retry up
    # to 3 times with 30 s pauses before declaring failure.
    logger.info("verifying via pooler endpoints...")
    all_ok = True
    for role, env_key in _ENV_KEY_BY_ROLE.items():
        ok = False
        for attempt in range(1, 4):
            ok = await _verify_pooler_auth(env_key, role)
            if ok:
                break
            wait = 30
            logger.info(
                "retrying %s in %ds (Supavisor cache may lag — attempt %d/3)...",
                role, wait, attempt,
            )
            await asyncio.sleep(wait)
        all_ok = all_ok and ok

    if not all_ok:
        logger.error(
            "post-sync verification failed. Likely causes:\n"
            "  1. Supavisor (pooler) credential cache hasn't refreshed — "
            "wait 60s and retry, or restart the project in the Supabase dashboard.\n"
            "  2. The DSN host is not the session-pooler endpoint expected by "
            "your env (transaction pooler on port 6543 would also fail differently)."
        )
        return 1

    logger.info("all roles synced + verified ✓")
    return 0


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        return asyncio.run(_amain())
    except Exception:  # noqa: BLE001
        logger.exception("sync_role_passwords crashed:")
        return 3


if __name__ == "__main__":
    sys.exit(main())
