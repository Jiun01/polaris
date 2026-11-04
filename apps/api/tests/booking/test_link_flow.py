"""Tests for services/booking/link_flow.py (v2.1 PR-1).

All tests fake the service pool (``get_service_pool``) so no real DB is
required.  The fake pool returns a fake asyncpg connection whose
``fetchrow`` / ``execute`` return canned values.

Coverage:
  * create_pending_booking: inserts a row and returns PendingBooking with
    non-empty jti and a UUID booking_id.
  * confirm_booking: first delivery → "confirmed"; re-delivery (jti not
    found, appointment found) → "already_confirmed"; neither found →
    "not_found".
  * sweep_expired_pending: calls UPDATE and parses the returned tag count.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from polaris_api.services.booking.link_flow import (
    ConfirmResult,
    PendingBooking,
    confirm_booking,
    create_pending_booking,
    sweep_expired_pending,
)

pytestmark = pytest.mark.asyncio(loop_scope="function")


# ---------------------------------------------------------------------------
# Fake pool / connection helpers
# ---------------------------------------------------------------------------


def _make_fake_conn(
    *,
    fetchrow_side_effect: list[Any] | None = None,
    execute_return: str = "UPDATE 0",
) -> AsyncMock:
    """Return an AsyncMock that looks like asyncpg.Connection."""
    conn = AsyncMock()
    if fetchrow_side_effect is not None:
        conn.fetchrow = AsyncMock(side_effect=fetchrow_side_effect)
    else:
        conn.fetchrow = AsyncMock(return_value=None)
    conn.execute = AsyncMock(return_value=execute_return)
    return conn


def _make_fake_pool(conn: AsyncMock) -> MagicMock:
    """Return a fake pool whose acquire() context manager yields ``conn``."""
    pool = MagicMock()
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=conn)
    cm.__aexit__ = AsyncMock(return_value=None)
    pool.acquire = MagicMock(return_value=cm)
    return pool


# ---------------------------------------------------------------------------
# create_pending_booking
# ---------------------------------------------------------------------------


async def test_create_pending_booking_returns_pending_booking_with_jti() -> None:
    """create_pending_booking must INSERT and return a PendingBooking
    with a non-empty jti and the booking_id from the RETURNING clause."""
    booking_id = uuid4()
    conn = _make_fake_conn(fetchrow_side_effect=[{"id": booking_id}])
    fake_pool = _make_fake_pool(conn)

    with patch(
        "polaris_api.services.booking.link_flow.get_service_pool",
        return_value=fake_pool,
    ):
        result = await create_pending_booking(
            tenant_id=uuid4(),
            conversation_id=uuid4(),
            channel="whatsapp",
            customer_handle="60123456789",
            service_name="Consultation",
        )

    assert isinstance(result, PendingBooking)
    assert result.booking_id == booking_id
    assert isinstance(result.jti, str)
    assert len(result.jti) > 0


async def test_create_pending_booking_persists_ea_ids() -> None:
    """ea_service_id / ea_provider_id must reach the INSERT so the /b/{jti}
    redirect can rebuild the EA wizard URL deterministically."""
    booking_id = uuid4()
    conn = _make_fake_conn(fetchrow_side_effect=[{"id": booking_id}])
    fake_pool = _make_fake_pool(conn)

    with patch(
        "polaris_api.services.booking.link_flow.get_service_pool",
        return_value=fake_pool,
    ):
        await create_pending_booking(
            tenant_id=uuid4(),
            conversation_id=uuid4(),
            channel="whatsapp",
            customer_handle="60123456789",
            ea_service_id=3,
            ea_provider_id=7,
        )

    args = conn.fetchrow.call_args.args
    assert 3 in args  # ea_service_id
    assert 7 in args  # ea_provider_id


async def test_create_pending_booking_passes_customer_handle_to_db() -> None:
    """The customer_handle (the verified identity anchor) must reach the
    INSERT query verbatim — any mismatch would break the cancel/reschedule
    ownership gate downstream."""
    booking_id = uuid4()
    conn = _make_fake_conn(fetchrow_side_effect=[{"id": booking_id}])
    fake_pool = _make_fake_pool(conn)
    handle = "60123456789"

    with patch(
        "polaris_api.services.booking.link_flow.get_service_pool",
        return_value=fake_pool,
    ):
        await create_pending_booking(
            tenant_id=uuid4(),
            conversation_id=uuid4(),
            channel="whatsapp",
            customer_handle=handle,
        )

    fetchrow_call = conn.fetchrow.await_args
    # asyncpg positional params: $4 is customer_handle in the INSERT.
    # Exact position: (sql, $1 tenant_id, $2 conversation_id, $3 channel,
    #                  $4 customer_handle, $5 service_name, $6 jti, $7 expires_at)
    positional_args = fetchrow_call.args
    assert handle in positional_args  # must appear somewhere in the params


async def test_create_pending_booking_distinct_jti_per_call() -> None:
    """Each call to create_pending_booking must generate a fresh jti."""
    booking_id_a = uuid4()
    booking_id_b = uuid4()

    conn_a = _make_fake_conn(fetchrow_side_effect=[{"id": booking_id_a}])
    conn_b = _make_fake_conn(fetchrow_side_effect=[{"id": booking_id_b}])

    with patch(
        "polaris_api.services.booking.link_flow.get_service_pool",
        return_value=_make_fake_pool(conn_a),
    ):
        r1 = await create_pending_booking(
            tenant_id=uuid4(), conversation_id=uuid4(),
            channel="whatsapp", customer_handle="60111",
        )

    with patch(
        "polaris_api.services.booking.link_flow.get_service_pool",
        return_value=_make_fake_pool(conn_b),
    ):
        r2 = await create_pending_booking(
            tenant_id=uuid4(), conversation_id=uuid4(),
            channel="whatsapp", customer_handle="60222",
        )

    assert r1.jti != r2.jti


# ---------------------------------------------------------------------------
# confirm_booking
# ---------------------------------------------------------------------------


async def test_confirm_booking_first_delivery_returns_confirmed() -> None:
    """First delivery: UPDATE hits the pending row → outcome='confirmed'."""
    booking_id = uuid4()
    # First fetchrow (the UPDATE ... RETURNING id) returns the row.
    # Second fetchrow (the SELECT fallback) is NOT called.
    conn = _make_fake_conn(fetchrow_side_effect=[{"id": booking_id}])
    fake_pool = _make_fake_pool(conn)

    # Isolate the outcome logic from reminder scheduling (its own tests below).
    with (
        patch(
            "polaris_api.services.booking.link_flow.get_service_pool",
            return_value=fake_pool,
        ),
        patch(
            "polaris_api.services.booking.link_flow._schedule_reminder_for_confirmed",
            new=AsyncMock(),
        ),
    ):
        result = await confirm_booking(
            tenant_id=uuid4(),
            jti="test-jti",
            appointment_id="42",
            start_at=datetime(2026, 7, 1, 9, 0, tzinfo=UTC),
            end_at=datetime(2026, 7, 1, 9, 30, tzinfo=UTC),
            service_name="Consultation",
        )

    assert isinstance(result, ConfirmResult)
    assert result.outcome == "confirmed"
    assert result.booking_id == booking_id


# ---------------------------------------------------------------------------
# confirm_booking → reminder scheduling wiring (Phase A)
# ---------------------------------------------------------------------------


async def test_confirm_booking_schedules_reminder_on_first_confirm() -> None:
    """A first-delivery confirm must schedule the pre-appointment reminder
    with the confirmed booking_id, tenant_id, and start_at."""
    booking_id = uuid4()
    tenant_id = uuid4()
    start_at = datetime(2026, 7, 1, 9, 0, tzinfo=UTC)
    conn = _make_fake_conn(fetchrow_side_effect=[{"id": booking_id}])
    fake_pool = _make_fake_pool(conn)
    sched = AsyncMock()

    with (
        patch(
            "polaris_api.services.booking.link_flow.get_service_pool",
            return_value=fake_pool,
        ),
        patch(
            "polaris_api.services.booking.link_flow._schedule_reminder_for_confirmed",
            new=sched,
        ),
    ):
        await confirm_booking(
            tenant_id=tenant_id,
            jti="test-jti",
            appointment_id="42",
            start_at=start_at,
            end_at=None,
        )

    sched.assert_awaited_once()
    kwargs = sched.await_args.kwargs
    assert kwargs["tenant_id"] == tenant_id
    assert kwargs["booking_id"] == booking_id
    assert kwargs["start_at"] == start_at


async def test_confirm_booking_no_reminder_on_already_confirmed() -> None:
    """Idempotent re-delivery (already_confirmed) must NOT schedule a second
    reminder — only the first confirm does."""
    conn = _make_fake_conn(fetchrow_side_effect=[None, {"id": uuid4()}])
    fake_pool = _make_fake_pool(conn)
    sched = AsyncMock()

    with (
        patch(
            "polaris_api.services.booking.link_flow.get_service_pool",
            return_value=fake_pool,
        ),
        patch(
            "polaris_api.services.booking.link_flow._schedule_reminder_for_confirmed",
            new=sched,
        ),
    ):
        result = await confirm_booking(
            tenant_id=uuid4(),
            jti="already-consumed-jti",
            appointment_id="42",
            start_at=datetime(2026, 7, 1, 9, 0, tzinfo=UTC),
            end_at=None,
        )

    assert result.outcome == "already_confirmed"
    sched.assert_not_awaited()


async def test_confirm_booking_no_reminder_on_not_found() -> None:
    """A confirm that matches nothing must not schedule a reminder."""
    conn = _make_fake_conn(fetchrow_side_effect=[None, None])
    fake_pool = _make_fake_pool(conn)
    sched = AsyncMock()

    with (
        patch(
            "polaris_api.services.booking.link_flow.get_service_pool",
            return_value=fake_pool,
        ),
        patch(
            "polaris_api.services.booking.link_flow._schedule_reminder_for_confirmed",
            new=sched,
        ),
    ):
        result = await confirm_booking(
            tenant_id=uuid4(),
            jti="ghost-jti",
            appointment_id="99999",
            start_at=datetime(2026, 7, 1, 9, 0, tzinfo=UTC),
            end_at=None,
        )

    assert result.outcome == "not_found"
    sched.assert_not_awaited()


async def test_schedule_reminder_for_confirmed_reads_offset_and_schedules() -> None:
    """_schedule_reminder_for_confirmed reads booking_config.reminder_offset_hours
    and forwards it to schedule_reminder."""
    from polaris_api.services.booking.link_flow import (
        _schedule_reminder_for_confirmed,
    )

    tenant_id = uuid4()
    booking_id = uuid4()
    start_at = datetime(2026, 7, 1, 9, 0, tzinfo=UTC)
    conn = AsyncMock()
    sched = AsyncMock()

    with (
        patch(
            "polaris_api.services.booking.config.read_booking_config",
            new=AsyncMock(return_value={"reminder_offset_hours": 48}),
        ),
        patch(
            "polaris_api.services.booking_reminders.schedule_reminder",
            new=sched,
        ),
    ):
        await _schedule_reminder_for_confirmed(
            conn,
            tenant_id=tenant_id,
            booking_id=booking_id,
            start_at=start_at,
        )

    sched.assert_awaited_once()
    # conn must be forwarded as the first positional arg (idempotent insert
    # has to run on the same open service-pool connection as the confirm).
    assert sched.await_args.args[0] is conn
    kwargs = sched.await_args.kwargs
    assert kwargs["offset_hours"] == 48
    assert kwargs["booking_id"] == booking_id
    assert kwargs["tenant_id"] == tenant_id


async def test_schedule_reminder_for_confirmed_defaults_offset_when_unset() -> None:
    """Missing reminder_offset_hours falls back to the 24h default."""
    from polaris_api.services.booking.link_flow import (
        _schedule_reminder_for_confirmed,
    )

    conn = AsyncMock()
    sched = AsyncMock()

    with (
        patch(
            "polaris_api.services.booking.config.read_booking_config",
            new=AsyncMock(return_value={}),
        ),
        patch(
            "polaris_api.services.booking_reminders.schedule_reminder",
            new=sched,
        ),
    ):
        await _schedule_reminder_for_confirmed(
            conn,
            tenant_id=uuid4(),
            booking_id=uuid4(),
            start_at=datetime(2026, 7, 1, 9, 0, tzinfo=UTC),
        )

    assert sched.await_args.kwargs["offset_hours"] == 24


async def test_schedule_reminder_for_confirmed_defaults_offset_when_uncastable() -> None:
    """A non-numeric reminder_offset_hours falls back to the 24h default
    rather than propagating a ValueError."""
    from polaris_api.services.booking.link_flow import (
        _schedule_reminder_for_confirmed,
    )

    conn = AsyncMock()
    sched = AsyncMock()

    with (
        patch(
            "polaris_api.services.booking.config.read_booking_config",
            new=AsyncMock(return_value={"reminder_offset_hours": "not-a-number"}),
        ),
        patch(
            "polaris_api.services.booking_reminders.schedule_reminder",
            new=sched,
        ),
    ):
        await _schedule_reminder_for_confirmed(
            conn,
            tenant_id=uuid4(),
            booking_id=uuid4(),
            start_at=datetime(2026, 7, 1, 9, 0, tzinfo=UTC),
        )

    assert sched.await_args.kwargs["offset_hours"] == 24


async def test_schedule_reminder_for_confirmed_swallows_errors() -> None:
    """A failure while scheduling must not propagate (best-effort) — the
    confirm has to ack even if the reminder can't be scheduled."""
    from polaris_api.services.booking.link_flow import (
        _schedule_reminder_for_confirmed,
    )

    conn = AsyncMock()

    with (
        patch(
            "polaris_api.services.booking.config.read_booking_config",
            new=AsyncMock(side_effect=RuntimeError("db down")),
        ),
    ):
        # Must not raise.
        await _schedule_reminder_for_confirmed(
            conn,
            tenant_id=uuid4(),
            booking_id=uuid4(),
            start_at=datetime(2026, 7, 1, 9, 0, tzinfo=UTC),
        )


async def test_confirm_booking_redelivery_returns_already_confirmed() -> None:
    """Re-delivery: UPDATE returns None (jti already cleared), SELECT finds
    confirmed row → outcome='already_confirmed'."""
    existing_id = uuid4()
    # First fetchrow = None (no pending row found by jti).
    # Second fetchrow = confirmed row found by appointment_id.
    conn = _make_fake_conn(
        fetchrow_side_effect=[None, {"id": existing_id}]
    )
    fake_pool = _make_fake_pool(conn)

    with patch(
        "polaris_api.services.booking.link_flow.get_service_pool",
        return_value=fake_pool,
    ):
        result = await confirm_booking(
            tenant_id=uuid4(),
            jti="already-consumed-jti",
            appointment_id="42",
            start_at=datetime(2026, 7, 1, 9, 0, tzinfo=UTC),
            end_at=None,
        )

    assert result.outcome == "already_confirmed"
    assert result.booking_id == existing_id


async def test_confirm_booking_unknown_jti_and_appointment_returns_not_found() -> None:
    """Both the UPDATE and the SELECT fallback find nothing → 'not_found'."""
    conn = _make_fake_conn(fetchrow_side_effect=[None, None])
    fake_pool = _make_fake_pool(conn)

    with patch(
        "polaris_api.services.booking.link_flow.get_service_pool",
        return_value=fake_pool,
    ):
        result = await confirm_booking(
            tenant_id=uuid4(),
            jti="ghost-jti",
            appointment_id="99999",
            start_at=datetime(2026, 7, 1, 9, 0, tzinfo=UTC),
            end_at=None,
        )

    assert result.outcome == "not_found"
    assert result.booking_id is None


# ---------------------------------------------------------------------------
# sweep_expired_pending
# ---------------------------------------------------------------------------


async def test_sweep_expired_pending_returns_count() -> None:
    """sweep_expired_pending must parse the asyncpg UPDATE tag and return
    the integer count of rows cancelled."""
    conn = AsyncMock()
    conn.execute = AsyncMock(return_value="UPDATE 5")
    fake_pool = _make_fake_pool(conn)

    with patch(
        "polaris_api.services.booking.link_flow.get_service_pool",
        return_value=fake_pool,
    ):
        count = await sweep_expired_pending()

    assert count == 5


async def test_sweep_expired_pending_zero_rows() -> None:
    """sweep_expired_pending with no expired rows returns 0."""
    conn = AsyncMock()
    conn.execute = AsyncMock(return_value="UPDATE 0")
    fake_pool = _make_fake_pool(conn)

    with patch(
        "polaris_api.services.booking.link_flow.get_service_pool",
        return_value=fake_pool,
    ):
        count = await sweep_expired_pending()

    assert count == 0
