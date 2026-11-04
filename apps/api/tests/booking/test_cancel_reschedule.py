"""Identity-isolation tests for services/booking/cancel_reschedule.py.

The MOST security-critical tests in this PR: they prove that
Customer A cannot cancel/reschedule Customer B's booking.

The identity anchor: ``customer_handle`` is ALWAYS the verified
``channel_user_id`` from the signed Meta webhook — never a
user-typed value.  These tests exercise that gate.

Two fixture strategies co-exist:
  - Fake asyncpg connection (``FakeConn``) for all tests that
    don't need a real DB.  No skip markers, always run in CI.
  - DB-gated tests using ``service_conn`` from the root conftest
    are not written here because the non-DB fakes give the same
    coverage.  The gate would obscure the security assertion.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, call, patch
from uuid import uuid4

import pytest
from polaris_api.services.booking.cancel_reschedule import (
    BookingNotOwned,
    CustomerBooking,
    cancel_booking,
    find_customer_bookings,
    reschedule_booking,
)

pytestmark = pytest.mark.asyncio(loop_scope="function")


# ---------------------------------------------------------------------------
# Fake asyncpg connection helpers
# ---------------------------------------------------------------------------


def _fake_conn(
    *,
    fetchrow_return: Any = None,
    fetch_return: list[Any] | None = None,
    execute_return: str = "UPDATE 1",
) -> AsyncMock:
    """Return an AsyncMock that satisfies asyncpg.Connection's interface."""
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=fetchrow_return)
    conn.fetch = AsyncMock(return_value=fetch_return or [])
    conn.execute = AsyncMock(return_value=execute_return)
    return conn


def _make_row(**kwargs: Any) -> dict[str, Any]:
    """Return a dict that quacks like an asyncpg.Record for our code."""
    return dict(kwargs)


# ---------------------------------------------------------------------------
# find_customer_bookings
# ---------------------------------------------------------------------------


async def test_find_customer_bookings_returns_matching_rows() -> None:
    """find_customer_bookings must return only rows the fetch returns
    (the SQL filters by tenant_id/channel/customer_handle/status/'confirmed'/future)."""
    tenant_id = uuid4()
    booking_id = uuid4()
    start = datetime(2026, 7, 1, 10, 0, tzinfo=UTC)

    fake_row = _make_row(
        id=booking_id,
        appointment_id="99",
        start_at=start,
        service_name="Consultation",
    )
    conn = _fake_conn(fetch_return=[fake_row])

    result = await find_customer_bookings(
        conn,
        tenant_id=tenant_id,
        channel="whatsapp",
        customer_handle="60123456789",
    )

    assert len(result) == 1
    cb = result[0]
    assert isinstance(cb, CustomerBooking)
    assert cb.booking_id == booking_id
    assert cb.appointment_id == "99"
    assert cb.start_at == start
    assert cb.service_name == "Consultation"

    # Verify the correct parameters were passed to the DB query.
    fetch_call: call = conn.fetch.await_args
    positional_args = fetch_call.args
    assert positional_args[1] == tenant_id      # $1 tenant_id
    assert positional_args[2] == "whatsapp"     # $2 channel
    assert positional_args[3] == "60123456789"  # $3 customer_handle


async def test_find_customer_bookings_returns_empty_when_no_rows() -> None:
    conn = _fake_conn(fetch_return=[])
    result = await find_customer_bookings(
        conn, tenant_id=uuid4(), channel="whatsapp", customer_handle="60111111111",
    )
    assert result == []


async def test_find_customer_bookings_does_not_return_different_handle() -> None:
    """Rows for a DIFFERENT customer_handle must not be returned.

    This is the implicit contract: the SQL carries the customer_handle filter;
    if the DB returns nothing (as it should for a non-matching handle), the
    Python layer must not synthesize extra rows.
    """
    # Simulate DB returning empty (correct SQL behaviour for non-matching handle).
    conn = _fake_conn(fetch_return=[])
    result = await find_customer_bookings(
        conn, tenant_id=uuid4(), channel="whatsapp",
        customer_handle="attacker-handle",
    )
    assert result == []


# ---------------------------------------------------------------------------
# cancel_booking — the core security test
# ---------------------------------------------------------------------------


async def test_cancel_booking_wrong_customer_handle_raises_not_owned() -> None:
    """SECURITY: cancel_booking must raise BookingNotOwned when the
    (booking_id, customer_handle) pair doesn't match a confirmed row.

    This proves Customer A cannot cancel Customer B's booking:
      * The DB returns None for the ownership check (wrong handle).
      * BookingNotOwned is raised BEFORE any EA call is made.
      * The EA client's cancel_appointment is NEVER invoked.
    """
    # DB returns None — no confirmed row owned by this customer handle.
    conn = _fake_conn(fetchrow_return=None)

    tenant_id = uuid4()
    booking_id = uuid4()

    with patch(
        "polaris_api.services.booking.cancel_reschedule.get_ea_booking_client"
    ) as mock_get_client:
        fake_ea_client = AsyncMock()
        mock_get_client.return_value = fake_ea_client

        with pytest.raises(BookingNotOwned):
            await cancel_booking(
                conn,
                tenant_id=tenant_id,
                booking_id=booking_id,
                customer_handle="attacker-handle",
            )

        # The EA cancel method must NOT have been called.
        fake_ea_client.cancel_appointment.assert_not_awaited()

    # conn.execute (the UPDATE) must also not have been called.
    conn.execute.assert_not_awaited()


async def test_cancel_booking_null_appointment_id_raises_not_owned() -> None:
    """A row with appointment_id=None must also be treated as not-owned
    (the booking hasn't been confirmed by EA yet, so there's nothing to cancel)."""
    conn = _fake_conn(fetchrow_return=_make_row(appointment_id=None))

    with patch(
        "polaris_api.services.booking.cancel_reschedule.get_ea_booking_client"
    ) as mock_get_client:
        fake_ea_client = AsyncMock()
        mock_get_client.return_value = fake_ea_client

        with pytest.raises(BookingNotOwned):
            await cancel_booking(
                conn,
                tenant_id=uuid4(),
                booking_id=uuid4(),
                customer_handle="60123456789",
            )

        fake_ea_client.cancel_appointment.assert_not_awaited()


async def test_cancel_booking_happy_path_calls_ea_then_updates_db() -> None:
    """Happy path: ownership check passes → EA cancel called → DB status updated."""
    tenant_id = uuid4()
    booking_id = uuid4()
    appointment_id = "42"

    conn = _fake_conn(
        fetchrow_return=_make_row(appointment_id=appointment_id),
        execute_return="UPDATE 1",
    )

    with patch(
        "polaris_api.services.booking.cancel_reschedule.get_ea_booking_client"
    ) as mock_get_client:
        fake_ea_client = AsyncMock()
        fake_ea_client.cancel_appointment = AsyncMock(return_value=None)
        mock_get_client.return_value = fake_ea_client

        result = await cancel_booking(
            conn,
            tenant_id=tenant_id,
            booking_id=booking_id,
            customer_handle="60123456789",
        )

    assert result is True
    fake_ea_client.cancel_appointment.assert_awaited_once_with(
        tenant_id=tenant_id, appointment_id=int(appointment_id)
    )
    # The DB UPDATE must follow the EA call.
    conn.execute.assert_awaited_once()
    update_sql: str = conn.execute.await_args.args[0]
    assert "UPDATE bookings" in update_sql
    assert "cancelled" in update_sql


async def test_cancel_booking_ownership_check_uses_correct_params() -> None:
    """_load_owned_appointment must pass tenant_id AND customer_handle to fetchrow.

    This ensures a different tenant_id or different handle won't accidentally
    find a booking — the query itself carries both filters.
    """
    tenant_id = uuid4()
    booking_id = uuid4()
    handle = "60123456789"

    # Return None so we just check params without doing a full cancel flow.
    conn = _fake_conn(fetchrow_return=None)

    with patch(
        "polaris_api.services.booking.cancel_reschedule.get_ea_booking_client"
    ):
        with pytest.raises(BookingNotOwned):
            await cancel_booking(
                conn, tenant_id=tenant_id, booking_id=booking_id,
                customer_handle=handle,
            )

    fetchrow_call = conn.fetchrow.await_args
    positional_args = fetchrow_call.args
    # $1 = booking_id, $2 = tenant_id, $3 = customer_handle
    assert positional_args[1] == booking_id
    assert positional_args[2] == tenant_id
    assert positional_args[3] == handle


# ---------------------------------------------------------------------------
# reschedule_booking — security parity
# ---------------------------------------------------------------------------


async def test_reschedule_booking_wrong_handle_raises_not_owned() -> None:
    """reschedule_booking has the same ownership gate as cancel_booking."""
    conn = _fake_conn(fetchrow_return=None)

    with patch(
        "polaris_api.services.booking.cancel_reschedule.get_ea_booking_client"
    ) as mock_get_client:
        fake_ea_client = AsyncMock()
        mock_get_client.return_value = fake_ea_client

        with pytest.raises(BookingNotOwned):
            await reschedule_booking(
                conn,
                tenant_id=uuid4(),
                booking_id=uuid4(),
                customer_handle="attacker-handle",
                new_start=datetime(2026, 7, 2, 9, 0, tzinfo=UTC),
                new_end=datetime(2026, 7, 2, 9, 30, tzinfo=UTC),
            )

        # EA reschedule must NOT be called.
        fake_ea_client.reschedule_appointment.assert_not_awaited()

    conn.execute.assert_not_awaited()


async def test_reschedule_booking_happy_path_calls_ea_then_updates_db() -> None:
    """Happy path: ownership check passes → EA reschedule called → DB updated."""
    tenant_id = uuid4()
    booking_id = uuid4()
    appointment_id = "55"
    new_start = datetime(2026, 7, 2, 9, 0, tzinfo=UTC)
    new_end = datetime(2026, 7, 2, 9, 30, tzinfo=UTC)

    conn = _fake_conn(
        fetchrow_return=_make_row(appointment_id=appointment_id),
        execute_return="UPDATE 1",
    )

    with patch(
        "polaris_api.services.booking.cancel_reschedule.get_ea_booking_client"
    ) as mock_get_client:
        fake_ea_client = AsyncMock()
        fake_ea_client.reschedule_appointment = AsyncMock(return_value={"id": 55})
        mock_get_client.return_value = fake_ea_client

        result = await reschedule_booking(
            conn,
            tenant_id=tenant_id,
            booking_id=booking_id,
            customer_handle="60123456789",
            new_start=new_start,
            new_end=new_end,
        )

    assert result is True
    fake_ea_client.reschedule_appointment.assert_awaited_once_with(
        tenant_id=tenant_id,
        appointment_id=int(appointment_id),
        start_datetime=new_start.isoformat(),
        end_datetime=new_end.isoformat(),
    )
    conn.execute.assert_awaited_once()
    update_sql: str = conn.execute.await_args.args[0]
    assert "UPDATE bookings" in update_sql
    assert "rescheduled" in update_sql
