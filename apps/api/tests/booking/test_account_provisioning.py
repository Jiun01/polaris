"""Tests for EA account provisioning (create/deprovision + HMAC signing).

Pure unit tests: a fake EA client + a fake asyncpg connection. No DB, no HTTP.
Covers the graceful-failure contract (EA errors never raise into the caller),
the settings.ea_account write, and the client-side HMAC signer / config gate.
"""

from __future__ import annotations

import hashlib
import hmac
from uuid import uuid4

import pytest
from polaris_api.services.booking.account_provisioning import (
    create_ea_account,
    deprovision_ea_account,
)
from polaris_api.services.booking.client import (
    EABookingClient,
    EABookingConfigError,
    EABookingStructuralError,
    EABookingTransientError,
)


class _FakeConn:
    """Captures execute() SQL + args so we can assert the settings merge."""

    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple]] = []

    async def execute(self, sql, *args):
        self.executed.append((sql, args))
        return "UPDATE 1"


class _FakeEA:
    def __init__(self, *, provision=None, raises=None, sync_raises=None):
        self._provision = provision or {
            "admin_user_id": 42,
            "provider_id": 7,
            "service_id": 3,
        }
        self._raises = raises
        self._sync_raises = sync_raises
        self.provision_calls = 0
        self.deprovision_calls = 0
        self.sync_calls = 0
        self.sync_kwargs: dict = {}
        self.last_kwargs: dict = {}

    async def provision_admin(self, **_kwargs):
        self.provision_calls += 1
        self.last_kwargs = _kwargs
        if self._raises:
            raise self._raises
        body = dict(self._provision)
        if _kwargs.get("username"):
            body["username"] = _kwargs["username"]
        return body

    async def sync_tenant(self, **_kwargs):
        self.sync_calls += 1
        self.sync_kwargs = _kwargs
        if self._sync_raises:
            raise self._sync_raises
        return {
            "ok": True,
            "tenant_id": _kwargs.get("tenant_id"),
            "slug": _kwargs.get("slug"),
            "status": _kwargs.get("status"),
        }

    async def deprovision(self, **_kwargs):
        self.deprovision_calls += 1
        if self._raises:
            raise self._raises


# --- create_ea_account -----------------------------------------------------


async def test_create_success_writes_settings_and_returns_ids():
    conn = _FakeConn()
    ea = _FakeEA()
    result = await create_ea_account(
        conn, tenant_id=uuid4(), slug="acme", email="a@acme.my",
        first_name="Jane", last_name="Tan", ea_client=ea,
    )
    assert result.provisioned is True
    # One settings write executed, binding the ea_account dict (not json text);
    # the ids live in the persisted block (the source of truth), not the result.
    assert len(conn.executed) == 1
    sql, args = conn.executed[0]
    assert "ea_account" in sql
    assert isinstance(args[1], dict)
    assert args[1]["status"] == "active"
    assert args[1]["admin_user_id"] == 42
    assert args[1]["provider_id"] == 7
    assert args[1]["service_id"] == 3


async def test_create_passes_username_and_echoes_it():
    # Optional operator-chosen username flows to the client and back into the
    # result + persisted ea_account block.
    conn = _FakeConn()
    ea = _FakeEA()
    result = await create_ea_account(
        conn, tenant_id=uuid4(), slug="acme", email="a@acme.my",
        first_name="Jane", last_name="Tan", username="acme-admin", ea_client=ea,
    )
    assert result.provisioned is True
    assert ea.last_kwargs["username"] == "acme-admin"
    assert result.username == "acme-admin"
    _, args = conn.executed[0]
    assert args[1]["username"] == "acme-admin"


async def test_create_forwards_company_fields_to_client():
    # company_name / company_link / timezone enrich the seeded EA settings and
    # must reach the client verbatim (the client adds them to the wire payload
    # only when non-empty — see test_ea_client).
    conn = _FakeConn()
    ea = _FakeEA()
    result = await create_ea_account(
        conn, tenant_id=uuid4(), slug="acme", email="a@acme.my",
        first_name="Jane", last_name="Tan",
        company_name="Acme Spa", company_link="https://acme.my",
        timezone="Asia/Kuala_Lumpur", ea_client=ea,
    )
    assert result.provisioned is True
    assert ea.last_kwargs["company_name"] == "Acme Spa"
    assert ea.last_kwargs["company_link"] == "https://acme.my"
    assert ea.last_kwargs["timezone"] == "Asia/Kuala_Lumpur"


async def test_create_company_fields_default_empty():
    # Backward-compat: omitted company fields pass through as empty strings,
    # which the client drops from the signed payload.
    conn = _FakeConn()
    ea = _FakeEA()
    await create_ea_account(
        conn, tenant_id=uuid4(), slug="acme", email="a@acme.my",
        first_name="Jane", last_name="Tan", ea_client=ea,
    )
    assert ea.last_kwargs["company_name"] == ""
    assert ea.last_kwargs["company_link"] == ""
    assert ea.last_kwargs["timezone"] == ""


async def test_create_fires_initial_ea_sync():
    # After a successful provision, create_ea_account mirrors EA's freshly-seeded
    # settings into tenants.settings (ea_calendar + booking_config) so the bot
    # enforces EA's policy from day one. Best-effort, but on the happy path it
    # results in a second write after the ea_account block.
    class _SyncConn:
        def __init__(self) -> None:
            self.executed: list[tuple[str, tuple]] = []

        async def execute(self, sql, *args):
            self.executed.append((sql, args))
            return "UPDATE 1"

        async def fetchrow(self, sql, *_args):
            # Reflects the just-written active ea_account so the sync proceeds.
            # audit Batch B (B7-a): settings_sync now issues a SECOND fetchrow
            # (phase 3, re-reading just settings->'booking_config') — route by
            # SQL fragment so both phases see a consistent shape.
            if "booking_config" in sql:
                return {"booking_config": {}}
            return {
                "settings": {
                    "ea_account": {"status": "active"},
                    "booking_config": {},
                }
            }

    class _SyncEA(_FakeEA):
        async def get_settings(self, *, tenant_id):
            return [
                {"name": "book_advance_timeout", "value": "1440"},
                {"name": "default_timezone", "value": "Asia/Kuala_Lumpur"},
            ]

        async def list_blocked_periods(self, *, tenant_id):
            return []

    conn = _SyncConn()
    ea = _SyncEA()
    result = await create_ea_account(
        conn, tenant_id=uuid4(), slug="acme", email="a@acme.my",
        first_name="Jane", last_name="Tan", ea_client=ea,
    )
    assert result.provisioned is True
    # 1st write = ea_account block; 2nd write = the EA mirror.
    assert len(conn.executed) == 2
    mirror_sql, _ = conn.executed[1]
    assert "ea_calendar" in mirror_sql


async def test_create_initial_sync_failure_does_not_fail_provision():
    # The initial sync is best-effort: a sync-side error (here, the fake conn
    # has no fetchrow so sync_ea_calendar raises) must NOT fail provisioning.
    conn = _FakeConn()  # only .execute — sync_ea_calendar's fetchrow will raise
    ea = _FakeEA()
    result = await create_ea_account(
        conn, tenant_id=uuid4(), slug="acme", email="a@acme.my",
        first_name="Jane", last_name="Tan", ea_client=ea,
    )
    assert result.provisioned is True
    # Only the ea_account write landed; the mirror write never happened.
    assert len(conn.executed) == 1


async def test_create_syncs_tenant_slug():
    # The slug→tenant routing row must be registered in EA at provision, else the
    # public booking page /book/{slug} 404s. create_ea_account calls sync_tenant
    # with the tenant's real slug.
    conn = _FakeConn()
    ea = _FakeEA()
    result = await create_ea_account(
        conn, tenant_id=uuid4(), slug="acme", email="a@acme.my",
        first_name="Jane", last_name="Tan", ea_client=ea,
    )
    assert result.provisioned is True
    assert ea.sync_calls == 1
    assert ea.sync_kwargs["slug"] == "acme"
    assert ea.sync_kwargs["status"] == "active"


async def test_create_sync_tenant_not_called_on_provision_failure():
    # If provisioning fails, we never reach the slug-sync.
    conn = _FakeConn()
    ea = _FakeEA(raises=EABookingTransientError("EA 502"))
    result = await create_ea_account(
        conn, tenant_id=uuid4(), slug="acme", email="a@acme.my",
        first_name="Jane", last_name="Tan", ea_client=ea,
    )
    assert result.provisioned is False
    assert ea.sync_calls == 0


async def test_create_sync_tenant_failure_is_non_fatal():
    # A slug-sync error must NOT fail provisioning (re-provision backfills it).
    conn = _FakeConn()
    ea = _FakeEA(sync_raises=EABookingTransientError("EA sync down"))
    result = await create_ea_account(
        conn, tenant_id=uuid4(), slug="acme", email="a@acme.my",
        first_name="Jane", last_name="Tan", ea_client=ea,
    )
    assert result.provisioned is True  # provision succeeded despite sync failure
    assert ea.sync_calls == 1


async def test_create_username_taken_maps_friendly_reason():
    # EA's 409 {error:"username_taken"} surfaces as the failure_reason (not the
    # generic exception class name), so the admin UI can show a clear message.
    conn = _FakeConn()
    ea = _FakeEA(
        raises=EABookingStructuralError(
            "That username is already in use.",
            status_code=409,
            ea_code="username_taken",
        )
    )
    result = await create_ea_account(
        conn, tenant_id=uuid4(), slug="acme", email="a@acme.my",
        first_name="Jane", last_name="Tan", username="taken", ea_client=ea,
    )
    assert result.provisioned is False
    assert result.failure_reason == "username_taken"
    assert conn.executed == []  # nothing persisted on failure


async def test_create_ea_down_is_graceful_no_write():
    conn = _FakeConn()
    ea = _FakeEA(raises=EABookingTransientError("EA 502"))
    result = await create_ea_account(
        conn, tenant_id=uuid4(), slug="acme", email="a@acme.my",
        first_name="Jane", last_name="Tan", ea_client=ea,
    )
    assert result.provisioned is False
    assert result.failure_reason == "EABookingTransientError"
    assert conn.executed == []  # nothing persisted on failure


# --- deprovision_ea_account ------------------------------------------------


async def test_deprovision_success_marks_settings():
    conn = _FakeConn()
    ea = _FakeEA()
    result = await deprovision_ea_account(conn, tenant_id=uuid4(), ea_client=ea)
    assert ea.deprovision_calls == 1
    assert result.failure_reason is None
    sql, args = conn.executed[0]
    assert args[1]["status"] == "deprovisioned"


async def test_deprovision_ea_error_is_graceful():
    conn = _FakeConn()
    ea = _FakeEA(raises=EABookingTransientError("EA down"))
    result = await deprovision_ea_account(conn, tenant_id=uuid4(), ea_client=ea)
    assert result.failure_reason == "EABookingTransientError"
    assert conn.executed == []


async def test_deprovision_write_settings_false_skips_update():
    # hard-delete path: the tenant row is about to be purged, so no marker write.
    conn = _FakeConn()
    ea = _FakeEA()
    result = await deprovision_ea_account(
        conn, tenant_id=uuid4(), ea_client=ea, write_settings=False
    )
    assert ea.deprovision_calls == 1
    assert result.failure_reason is None
    assert conn.executed == []  # no settings UPDATE


# --- client HMAC signer / config gate --------------------------------------


def _client(secret: str) -> EABookingClient:
    return EABookingClient(
        base_url="http://ea-nginx:80",
        jwt_signing_secret="x",
        jwt_audience="ea-fork",
        jwt_issuer="polaris",
        jwt_ttl_seconds=60,
        timeout_s=8.0,
        max_retries=0,
        provisioning_hmac_secret=secret,
    )


def test_sign_matches_reference_hmac():
    secret = "s3cr3t-value-32-bytes-long-aaaaaa"
    raw = b'{"tenant_id":"t","slug":"acme"}'
    expected = "sha256=" + hmac.new(
        secret.encode(), raw, hashlib.sha256
    ).hexdigest()
    assert _client(secret)._sign(raw) == expected


@pytest.mark.parametrize("bad", ["", "placeholder-ea-provisioning-hmac-secret"])
def test_check_provisioning_config_fails_closed(bad):
    with pytest.raises(EABookingConfigError):
        _client(bad)._check_provisioning_config()
