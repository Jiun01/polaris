"""Seed Easy!Appointments providers for the E2E fixture.

Spec: ../../docs/Polaris V2/e2e/01-seed-and-teardown.md §3 + §8.

Creates 4 providers in the EA fork (Polaris HQ + Tenant C HQ +
Tenant D HQ + Tenant D Branch-2), each with one or more services
and a working plan. Captures the EA-side provider IDs from the
POST responses (audit confirmed IDs are auto-generated, not
assignable) and writes them onto Polaris's first-class
``tenant_branches.ea_provider_id`` + ``ea_default_service_id`` columns
via the shared production write path (services/booking/provisioning).

Run::

    uv run python apps/api/scripts/seed_e2e_ea_providers.py [--reset] [--verbose]

Required env vars (auto-loaded from repo-root .env):
    POLARIS_DB_URL_SERVICE  (or DATABASE_URL / SUPABASE_DB_URL)
    EA_BASE_URL             default ``http://localhost:8090``
    EA_ADMIN_API_TOKEN      bootstrap bearer token for list/delete ops
    EA_JWT_SIGNING_SECRET   HS256 secret; provider creates use per-tenant JWTs
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

import httpx
import jwt
from dotenv import load_dotenv

load_dotenv(Path(__file__).parents[3] / ".env")

sys.path.insert(0, str(Path(__file__).parents[2]))

import asyncpg  # noqa: E402
from polaris_api.services.booking.branch_bindings import (  # noqa: E402
    set_branch_ea_binding_by_slug,
)

logger = logging.getLogger("seed_e2e_ea_providers")


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ServiceSpec:
    name: str
    duration_minutes: int
    price: float = 0.0
    currency: str = "MYR"


# Working plan format per EA REST API: {day: {start: "HH:MM", end: "HH:MM",
#   breaks: [...]} | null}.
_WORKING_PLAN_DEFAULT = {
    "monday":    {"start": "09:00", "end": "18:00", "breaks": []},
    "tuesday":   {"start": "09:00", "end": "18:00", "breaks": []},
    "wednesday": {"start": "09:00", "end": "18:00", "breaks": []},
    "thursday":  {"start": "09:00", "end": "18:00", "breaks": []},
    "friday":    {"start": "09:00", "end": "18:00", "breaks": []},
    "saturday":  None,
    "sunday":    None,
}


@dataclass(frozen=True)
class ProviderSpec:
    polaris_tenant_id: UUID
    polaris_branch_slug: str  # used to UPDATE tenant_branches metadata
    display_name: str
    first_name: str
    last_name: str
    email: str
    timezone: str
    services: tuple[ServiceSpec, ...]
    working_plan: dict = field(default_factory=lambda: _WORKING_PLAN_DEFAULT)


def _build_fixture() -> list[ProviderSpec]:
    polaris_uuid = UUID("00000000-0000-0000-0000-000000000001")
    tenant_c_uuid = UUID("00000000-0000-0000-0000-00000000000c")
    tenant_d_uuid = UUID("00000000-0000-0000-0000-00000000000d")
    return [
        ProviderSpec(
            polaris_tenant_id=polaris_uuid,
            polaris_branch_slug="polaris-hq",
            display_name="Polaris Consultation",
            first_name="Polaris",
            last_name="HQ",
            email="provider-polaris@e2e.polaris-local.dev",
            timezone="Asia/Kuala_Lumpur",
            services=(
                ServiceSpec(name="30-min Discovery Call", duration_minutes=30),
                ServiceSpec(name="1-hr Strategy", duration_minutes=60),
            ),
        ),
        ProviderSpec(
            polaris_tenant_id=tenant_c_uuid,
            polaris_branch_slug="tenant-c-hq",
            display_name="Tenant C Service",
            first_name="Tenant",
            last_name="C HQ",
            email="provider-tenant-c@e2e.polaris-local.dev",
            timezone="Asia/Kuala_Lumpur",
            services=(ServiceSpec(name="Standard", duration_minutes=45),),
            working_plan={
                "monday":    {"start": "10:00", "end": "20:00", "breaks": []},
                "tuesday":   {"start": "10:00", "end": "20:00", "breaks": []},
                "wednesday": {"start": "10:00", "end": "20:00", "breaks": []},
                "thursday":  {"start": "10:00", "end": "20:00", "breaks": []},
                "friday":    {"start": "10:00", "end": "20:00", "breaks": []},
                "saturday":  {"start": "10:00", "end": "20:00", "breaks": []},
                "sunday":    None,
            },
        ),
        ProviderSpec(
            polaris_tenant_id=tenant_d_uuid,
            polaris_branch_slug="tenant-d-hq",
            display_name="Tenant D HQ",
            first_name="Tenant",
            last_name="D HQ",
            email="provider-tenant-d-hq@e2e.polaris-local.dev",
            timezone="Asia/Kuala_Lumpur",
            services=(ServiceSpec(name="Standard D", duration_minutes=45),),
            working_plan={
                "monday":    {"start": "09:00", "end": "17:00", "breaks": []},
                "tuesday":   {"start": "09:00", "end": "17:00", "breaks": []},
                "wednesday": {"start": "09:00", "end": "17:00", "breaks": []},
                "thursday":  {"start": "09:00", "end": "17:00", "breaks": []},
                "friday":    {"start": "09:00", "end": "17:00", "breaks": []},
                "saturday":  None,
                "sunday":    None,
            },
        ),
        ProviderSpec(
            polaris_tenant_id=tenant_d_uuid,
            polaris_branch_slug="tenant-d-branch-2",
            display_name="Tenant D Branch 2",
            first_name="Tenant",
            last_name="D Branch 2",
            email="provider-tenant-d-b2@e2e.polaris-local.dev",
            timezone="Asia/Kuala_Lumpur",
            services=(ServiceSpec(name="Standard D", duration_minutes=45),),
            working_plan={
                "monday":    None,
                "tuesday":   None,
                "wednesday": {"start": "12:00", "end": "20:00", "breaks": []},
                "thursday":  {"start": "12:00", "end": "20:00", "breaks": []},
                "friday":    {"start": "12:00", "end": "20:00", "breaks": []},
                "saturday":  {"start": "12:00", "end": "20:00", "breaks": []},
                "sunday":    {"start": "12:00", "end": "20:00", "breaks": []},
            },
        ),
    ]


# ---------------------------------------------------------------------------
# JWT helper
# ---------------------------------------------------------------------------


def _make_tenant_jwt(tenant_id: UUID, secret: str, ttl_seconds: int = 3600) -> str:
    """Return a short-lived HS256 JWT with the tenant_id claim EA expects.

    Claims mirror what BearerAuthHook / MY_Controller.from_bearer() validates:
      iss=polaris, aud=ea-fork, tenant_id=<uuid-str>, iat, exp.
    """
    now = int(time.time())
    return jwt.encode(
        {
            "iss": "polaris",
            "aud": "ea-fork",
            "tenant_id": str(tenant_id),
            "iat": now,
            "exp": now + ttl_seconds,
        },
        secret,
        algorithm="HS256",
    )


# ---------------------------------------------------------------------------
# EA REST API client
# ---------------------------------------------------------------------------


class EAClient:
    def __init__(self, base_url: str, token: str) -> None:
        # CI3 front controller pattern -- all routes under /index.php/.
        self.base_url = base_url.rstrip("/") + "/index.php/api/v1"
        self._token = token

    def _hdrs(self, token: str | None = None) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {token or self._token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def list_providers(self, client: httpx.AsyncClient) -> list[dict]:
        r = await client.get(f"{self.base_url}/providers", headers=self._hdrs())
        r.raise_for_status()
        return r.json()

    async def create_provider(
        self,
        client: httpx.AsyncClient,
        spec: ProviderSpec,
        *,
        token: str | None = None,
    ) -> int:
        """POST /providers with a per-tenant JWT so tenant_id is set in EA."""
        body = {
            "firstName": spec.first_name,
            "lastName": spec.last_name,
            "email": spec.email,
            "phone": "+60-00-000-0000",
            "timezone": spec.timezone,
            "language": "english",
            "services": [],
            "settings": {
                "username": spec.email.split("@")[0],
                "password": "E2e-provider-2026!",
                "workingPlan": spec.working_plan,
            },
        }
        r = await client.post(
            f"{self.base_url}/providers",
            headers=self._hdrs(token),
            json=body,
        )
        if r.status_code not in (200, 201):
            raise RuntimeError(
                f"EA POST /providers failed: {r.status_code} {r.text[:300]}"
            )
        provider = r.json()
        return int(provider["id"])

    async def delete_provider(self, client: httpx.AsyncClient, provider_id: int) -> None:
        r = await client.delete(
            f"{self.base_url}/providers/{provider_id}",
            headers=self._hdrs(),
        )
        if r.status_code not in (200, 204, 404):
            raise RuntimeError(
                f"EA DELETE /providers/{provider_id} failed: {r.status_code} {r.text[:300]}"
            )

    async def create_service(
        self,
        client: httpx.AsyncClient,
        spec: ServiceSpec,
        *,
        token: str | None = None,
    ) -> int:
        body = {
            "name": spec.name,
            "duration": spec.duration_minutes,
            "price": spec.price,
            "currency": spec.currency,
            "availabilitiesType": "flexible",
            "attendantsNumber": 1,
        }
        r = await client.post(
            f"{self.base_url}/services",
            headers=self._hdrs(token),
            json=body,
        )
        if r.status_code not in (200, 201):
            raise RuntimeError(
                f"EA POST /services failed: {r.status_code} {r.text[:300]}"
            )
        return int(r.json()["id"])

    async def attach_service_to_provider(
        self,
        client: httpx.AsyncClient,
        provider_id: int,
        service_id: int,
        *,
        token: str | None = None,
    ) -> None:
        # EA assigns services to providers via UPDATE on provider with
        # `services: [ids]`.
        r = await client.get(
            f"{self.base_url}/providers/{provider_id}", headers=self._hdrs(token)
        )
        r.raise_for_status()
        provider = r.json()
        services = set(provider.get("services") or [])
        services.add(service_id)
        body = dict(provider)
        body["services"] = list(services)
        body.pop("id", None)  # don't echo the id on PUT
        r = await client.put(
            f"{self.base_url}/providers/{provider_id}",
            headers=self._hdrs(token),
            json=body,
        )
        if r.status_code not in (200, 204):
            raise RuntimeError(
                f"EA PUT /providers/{provider_id} (attach service) "
                f"failed: {r.status_code} {r.text[:300]}"
            )


# ---------------------------------------------------------------------------
# Polaris-side writeback
# ---------------------------------------------------------------------------


async def _write_back_ea_provider_id(
    conn: asyncpg.Connection,
    tenant_id: UUID,
    branch_slug: str,
    ea_provider_id: int,
    ea_default_service_id: int | None,
) -> None:
    """Write EA's auto-generated provider + default-service ids onto the
    branch's FIRST-CLASS columns via the SAME write path production uses
    (services/booking/provisioning.set_branch_ea_binding_by_slug), so seed
    and prod never drift. Raises EAProvisioningError if no branch matches
    the slug (run seed_e2e_multitenant.py first)."""
    await set_branch_ea_binding_by_slug(
        conn,
        tenant_id=tenant_id,
        branch_slug=branch_slug,
        ea_provider_id=ea_provider_id,
        ea_default_service_id=ea_default_service_id,
    )


# ---------------------------------------------------------------------------
# ea_settings seeder
# ---------------------------------------------------------------------------

# Minimum settings required for the booking wizard to render without JS errors.
# date_format / time_format / first_weekday are read by ui.js getDateFormat() /
# getTimeFormat() — they throw "Invalid date format value." when null.
_EA_SETTINGS_BASE: dict[str, str] = {
    "date_format":          "DMY",
    "time_format":          "regular",
    "first_weekday":        "sunday",
    "future_booking_limit": "90",
    "disable_booking":      "0",
    "display_any_provider": "1",
    "customer_notifications": "1",
    "book_advance_timeout": "30",
    "require_captcha":      "0",
    "require_first_name":   "1",
    "require_last_name":    "1",
    "require_email":        "1",
    "require_phone_number": "1",
    "display_first_name":   "1",
    "display_last_name":    "1",
    "display_email":        "1",
    "display_phone_number": "1",
    "display_login_button": "1",
    "theme":                "default",
    "default_language":     "english",
    "default_timezone":     "Asia/Kuala_Lumpur",
    "company_color":        "#7cbae8",
    "company_link":         "https://polaris-local.dev",
    "privacy_policy_content": "",
    "terms_and_conditions_content": "",
    "cookie_notice_content": "",
    "display_cookie_notice": "0",
    "display_privacy_policy": "0",
    "display_terms_and_conditions": "0",
}


async def _seed_ea_settings(
    providers: list[ProviderSpec],
    mysql_container: str,
) -> None:
    """Upsert ea_settings rows so the booking page JS does not throw.

    DELETE + INSERT per tenant for idempotency — ea_settings has no
    UNIQUE (tenant_id, name) constraint, so ON DUPLICATE KEY is unavailable.
    """
    # Deduplicate tenants; first ProviderSpec wins for per-tenant label fields.
    seen: set[str] = set()
    unique: list[tuple[str, ProviderSpec]] = []
    for spec in providers:
        tid = str(spec.polaris_tenant_id)
        if tid not in seen:
            seen.add(tid)
            unique.append((tid, spec))

    for tenant_id, spec in unique:
        settings = dict(_EA_SETTINGS_BASE)
        settings["company_name"]  = spec.display_name
        settings["company_email"] = spec.email

        # Build a single multi-statement SQL: DELETE + bulk INSERT.
        value_rows = ", ".join(
            f"('{tenant_id}', '{name}', '{val}')"
            for name, val in settings.items()
        )
        sql = (
            f"DELETE FROM ea_settings WHERE tenant_id='{tenant_id}'; "
            f"INSERT INTO ea_settings (tenant_id, name, value) VALUES {value_rows};"
        )
        proc = await asyncio.create_subprocess_exec(
            "docker", "exec", mysql_container,
            "mysql", "-u", "user", "-ppassword", "easyappointments",
            "--execute", sql,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _out, err = await proc.communicate()
        if proc.returncode != 0:
            msg = (err or b"").decode().strip()
            if "warning" not in msg.lower():
                raise RuntimeError(
                    f"ea_settings seed failed for tenant={tenant_id!r}: {msg}"
                )
        print(f"  ea_settings: seeded {len(settings)} rows for tenant {tenant_id}")


# ---------------------------------------------------------------------------
# ea_polaris_tenants seeder
# ---------------------------------------------------------------------------


async def _seed_ea_polaris_tenants(
    providers: list[ProviderSpec],
    mysql_container: str,
) -> None:
    """Upsert ea_polaris_tenants rows so booking slugs resolve to fixture tenants.

    The booking URL /book/{slug} resolves via ea_polaris_tenants.  Each
    provider's email-prefix (e.g. "provider-polaris") becomes the slug for
    its tenant so AC-04-2/3 test URLs work without a live webhook sync.

    NOTE: ea_polaris_tenants has PRIMARY KEY (tenant_id) — exactly ONE booking
    slug per tenant. The public /book/{slug} page is tenant-level: from_slug()
    resolves slug -> tenant (NOT branch). Per-branch routing happens only in the
    conversational booking flow (inbox -> conversation.tenant_branch_id ->
    branch.ea_provider_id). So for a multi-branch tenant we register only the
    FIRST (HQ) provider's slug; registering a second would just overwrite it via
    ON DUPLICATE KEY and leave a dangling slug that 404s. The other branches are
    reachable through chat booking, not a distinct public slug.

    Uses docker exec to write directly to the EA MySQL — no extra Python dep.
    """
    # Build (slug, tenant_id) pairs — one per tenant (first/HQ provider wins).
    seen_tenants: set[str] = set()
    rows: list[tuple[str, str]] = []
    for spec in providers:
        tenant_id = str(spec.polaris_tenant_id)
        if tenant_id in seen_tenants:
            continue  # one slug per tenant (PK is tenant_id)
        seen_tenants.add(tenant_id)
        rows.append((spec.email.split("@")[0], tenant_id))

    for slug, tenant_id in rows:
        sql = (
            "INSERT INTO ea_polaris_tenants (tenant_id, slug, status) "
            f"VALUES ('{tenant_id}', '{slug}', 'active') "
            "ON DUPLICATE KEY UPDATE slug=VALUES(slug), status='active';"
        )
        proc = await asyncio.create_subprocess_exec(
            "docker", "exec", mysql_container,
            "mysql", "-u", "user", "-ppassword", "easyappointments", "-e", sql,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _out, err = await proc.communicate()
        if proc.returncode != 0:
            # mysql always prints the -p warning to stderr; treat it as non-fatal
            msg = (err or b"").decode().strip()
            if "warning" not in msg.lower():
                raise RuntimeError(
                    f"ea_polaris_tenants upsert failed for slug={slug!r}: {msg}"
                )
        print(f"  ea_polaris_tenants: slug={slug!r} -> tenant {tenant_id}")


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


async def main(args: argparse.Namespace) -> int:
    db_url = (
        os.environ.get("POLARIS_DB_URL_SERVICE")
        or os.environ.get("DATABASE_URL")
        or os.environ.get("SUPABASE_DB_URL")
    )
    if not db_url:
        print(
            "ERROR: POLARIS_DB_URL_SERVICE / DATABASE_URL is not set",
            file=sys.stderr,
        )
        return 1

    ea_base = os.environ.get("EA_BASE_URL", "http://localhost:8090")
    ea_token = os.environ.get("EA_ADMIN_API_TOKEN")
    if not ea_token:
        print("ERROR: EA_ADMIN_API_TOKEN is not set", file=sys.stderr)
        return 2

    jwt_secret = os.environ.get("EA_JWT_SIGNING_SECRET")
    if not jwt_secret:
        print("ERROR: EA_JWT_SIGNING_SECRET is not set", file=sys.stderr)
        return 3

    mysql_container = os.environ.get("EA_MYSQL_CONTAINER", "polaris-ea-fork-mysql-1")

    providers = _build_fixture()
    ea = EAClient(ea_base, ea_token)

    ssl_mode = "require" if "supabase.co" in db_url else None
    conn = await asyncpg.connect(db_url, ssl=ssl_mode)
    try:
        async with httpx.AsyncClient(timeout=30.0) as http:
            # Optional reset -- delete fixture providers if present.
            if args.reset:
                print("-> reset: deleting existing fixture providers from EA")
                existing = await ea.list_providers(http)
                fixture_emails = {p.email for p in providers}
                for p in existing:
                    if p.get("email") in fixture_emails:
                        pid = int(p["id"])
                        print(f"  delete provider id={pid} ({p['email']})")
                        await ea.delete_provider(http, pid)

            # Create providers + services + working plans.
            # Each provider is created with a per-tenant JWT so EA records
            # the correct tenant_id in ea_user_settings (admin token leaves it NULL).
            for spec in providers:
                print(f"\n-> {spec.display_name}")
                tenant_jwt = _make_tenant_jwt(spec.polaris_tenant_id, jwt_secret)
                pid = await ea.create_provider(http, spec, token=tenant_jwt)
                print(f"  created EA provider id={pid}")
                first_service_id: int | None = None
                for s in spec.services:
                    sid = await ea.create_service(http, s, token=tenant_jwt)
                    print(f"  created service {s.name!r} id={sid}")
                    if first_service_id is None:
                        first_service_id = sid
                    await ea.attach_service_to_provider(http, pid, sid, token=tenant_jwt)
                    print(f"  attached service {sid} -> provider {pid}")
                # Write back to Polaris (first-class columns, shared prod path).
                await _write_back_ea_provider_id(
                    conn, spec.polaris_tenant_id,
                    spec.polaris_branch_slug, pid, first_service_id,
                )
                print(
                    f"  polaris tenant_branches[{spec.polaris_branch_slug}]"
                    f".ea_provider_id={pid} ea_default_service_id={first_service_id}"
                )

            # Seed ea_polaris_tenants so booking slugs resolve to fixture tenants.
            print("\n-> seeding ea_polaris_tenants")
            await _seed_ea_polaris_tenants(providers, mysql_container)

            # Seed ea_settings so the booking wizard JS can render (date_format etc.).
            print("\n-> seeding ea_settings")
            await _seed_ea_settings(providers, mysql_container)

            # Verify: at least N providers exist in EA.
            all_providers = await ea.list_providers(http)
            fixture_emails = {p.email for p in providers}
            seeded = [p for p in all_providers if p.get("email") in fixture_emails]
            if len(seeded) < len(providers):
                print(
                    f"\nFAIL: expected {len(providers)} fixture providers, "
                    f"found {len(seeded)}",
                    file=sys.stderr,
                )
                return 4
            print(f"\nOK: {len(seeded)} fixture providers in EA")
    finally:
        await conn.close()

    return 0


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument(
        "--reset", action="store_true",
        help="Delete fixture providers from EA before re-creating.",
    )
    p.add_argument(
        "--verbose", "-v", action="store_true",
        help="Verbose HTTP logging.",
    )
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    sys.exit(asyncio.run(main(args)))
