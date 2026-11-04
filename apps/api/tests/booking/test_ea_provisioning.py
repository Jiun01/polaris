"""Tests for branch -> EA provider/service write-back (v2.1).

Covers the shared provisioning write path used by both the admin sync
endpoint and seed_e2e_ea_providers.py: idempotent first-class-column
writes, provider->branch matching, and default-service selection. Pure
unit tests with a fake asyncpg connection + fake EA client — no DB.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from polaris_api.services.booking.branch_bindings import (
    EAProvisioningError,
    _match_provider_to_branch,
    _norm,
    _provider_service_id,
    set_branch_ea_binding,
    set_branch_ea_binding_by_slug,
    sync_tenant_branch_ea_bindings,
)

# asyncio_mode=auto runs the async tests; sync helper tests stay unmarked.


class _FakeConn:
    """Fake asyncpg connection. fetch() returns branches; fetchrow()
    returns a configurable row (the UPDATE ... RETURNING id result)."""

    def __init__(self, branches=None, *, update_row=None):
        self._branches = branches or []
        self._update_row = update_row
        self.fetchrow_calls = 0

    async def fetch(self, _sql, *_args):
        return [dict(b) for b in self._branches]

    async def fetchrow(self, _sql, *_args):
        self.fetchrow_calls += 1
        return self._update_row


class _FakeEA:
    def __init__(self, providers, services):
        self._providers = providers
        self._services = services

    async def list_providers(self, *, tenant_id):
        return self._providers

    async def list_services(self, *, tenant_id):
        return self._services


# --- low-level writers -----------------------------------------------------


async def test_set_branch_ea_binding_returns_true_on_change():
    conn = _FakeConn(update_row={"id": uuid4()})
    changed = await set_branch_ea_binding(
        conn, tenant_id=uuid4(), branch_id=uuid4(),
        ea_provider_id=5, ea_default_service_id=7,
    )
    assert changed is True
    assert conn.fetchrow_calls == 1


async def test_set_branch_ea_binding_returns_false_on_noop():
    # UPDATE matched nothing-to-change (IS DISTINCT FROM guard) -> None.
    conn = _FakeConn(update_row=None)
    changed = await set_branch_ea_binding(
        conn, tenant_id=uuid4(), branch_id=uuid4(),
        ea_provider_id=5, ea_default_service_id=7,
    )
    assert changed is False


async def test_set_branch_ea_binding_by_slug_returns_id():
    bid = uuid4()
    conn = _FakeConn(update_row={"id": bid})
    got = await set_branch_ea_binding_by_slug(
        conn, tenant_id=uuid4(), branch_slug="hq",
        ea_provider_id=5, ea_default_service_id=7,
    )
    assert got == bid


async def test_set_branch_ea_binding_by_slug_raises_when_missing():
    conn = _FakeConn(update_row=None)
    with pytest.raises(EAProvisioningError):
        await set_branch_ea_binding_by_slug(
            conn, tenant_id=uuid4(), branch_slug="nope",
            ea_provider_id=5, ea_default_service_id=7,
        )


# --- helpers ---------------------------------------------------------------


def test_norm_strips_non_alnum_and_lowercases():
    assert _norm("Tenant-C HQ!") == "tenantchq"
    assert _norm(None) == ""


def test_provider_service_id_picks_first_valid():
    p = {"services": [99, 7, 8]}
    assert _provider_service_id(p, {7, 8}) == 7  # 99 invalid, 7 first valid


def test_provider_service_id_none_when_no_valid():
    assert _provider_service_id({"services": [99]}, {7, 8}) is None
    assert _provider_service_id({}, {7}) is None


def test_match_provider_to_branch_single_hit():
    branches = [
        {"id": uuid4(), "slug": "polaris-hq", "name": "Polaris HQ"},
        {"id": uuid4(), "slug": "kl-branch", "name": "KL Branch"},
    ]
    p = {"email": "x@y", "firstName": "Polaris", "lastName": "HQ"}
    assert _match_provider_to_branch(p, branches) is branches[0]


def test_match_provider_to_branch_no_hit_returns_none():
    branches = [{"id": uuid4(), "slug": "kl", "name": "KL"}]
    p = {"email": "z@y", "firstName": "Zzz", "lastName": "Nomatch"}
    assert _match_provider_to_branch(p, branches) is None


# --- sync ------------------------------------------------------------------


async def test_sync_single_branch_autobind():
    bid = uuid4()
    branches = [
        {"id": bid, "slug": "hq", "name": "HQ",
         "ea_provider_id": None, "ea_default_service_id": None},
    ]
    conn = _FakeConn(branches, update_row={"id": bid})
    ea = _FakeEA(
        providers=[{"id": 5, "email": "p@x", "services": [7]}],
        services=[{"id": 7, "name": "Consult"}],
    )
    report = await sync_tenant_branch_ea_bindings(
        conn, tenant_id=uuid4(), ea_client=ea
    )
    assert len(report.bound) == 1
    assert report.bound[0]["ea_provider_id"] == 5
    assert report.bound[0]["ea_default_service_id"] == 7
    assert report.unmatched_providers == []
    assert report.unmatched_branches == []


async def test_sync_idempotent_already_correct_is_unchanged():
    bid = uuid4()
    branches = [
        {"id": bid, "slug": "hq", "name": "HQ",
         "ea_provider_id": 5, "ea_default_service_id": 7},
    ]
    conn = _FakeConn(branches)  # update_row=None — must NOT be called
    ea = _FakeEA(
        providers=[{"id": 5, "email": "p@x", "services": [7]}],
        services=[{"id": 7}],
    )
    report = await sync_tenant_branch_ea_bindings(
        conn, tenant_id=uuid4(), ea_client=ea
    )
    assert len(report.unchanged) == 1
    assert report.bound == []
    assert conn.fetchrow_calls == 0  # no write attempted


async def test_sync_multi_branch_name_match_and_unmatched():
    b1, b2, b3 = uuid4(), uuid4(), uuid4()
    branches = [
        {"id": b1, "slug": "polaris-hq", "name": "Polaris HQ",
         "ea_provider_id": None, "ea_default_service_id": None},
        {"id": b2, "slug": "kl-branch", "name": "KL Branch",
         "ea_provider_id": None, "ea_default_service_id": None},
        {"id": b3, "slug": "lonely", "name": "Lonely",
         "ea_provider_id": None, "ea_default_service_id": None},
    ]
    conn = _FakeConn(branches, update_row={"id": b1})
    ea = _FakeEA(
        providers=[
            {"id": 10, "email": "a@x", "firstName": "Polaris", "lastName": "HQ", "services": [7]},
            {"id": 20, "email": "b@x", "firstName": "KL", "lastName": "Branch", "services": [8]},
            {"id": 30, "email": "c@x", "firstName": "Zzz", "lastName": "Nomatch", "services": [9]},
        ],
        services=[{"id": 7}, {"id": 8}],  # 9 is not a valid service
    )
    report = await sync_tenant_branch_ea_bindings(
        conn, tenant_id=uuid4(), ea_client=ea
    )
    bound_ids = {(e["branch_id"], e["ea_provider_id"]) for e in report.bound}
    assert (str(b1), 10) in bound_ids
    assert (str(b2), 20) in bound_ids
    # provider 30 matched no branch:
    assert any(p["id"] == 30 for p in report.unmatched_providers)
    # branch b3 (lonely) got no provider:
    assert any(b["branch_id"] == str(b3) for b in report.unmatched_branches)
