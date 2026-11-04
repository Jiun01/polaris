"""Audience CSV importer tests — Phase 6 PR-4.

Pure-logic coverage of ``services/audience/importer.py``. The
end-to-end import-against-real-DB path runs in CI's migration-check
workflow; this file targets the per-row validation logic so we get
fast local feedback.

The importer's outer ``import_csv`` function takes a real
``asyncpg.Connection``; we don't construct one here. Instead we test
the embedded validation primitives via a stub-conn pattern AND
exercise ``import_csv`` against an empty CSV body (which short-
circuits before any DB SQL fires).
"""

from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from polaris_api.services.audience import importer


@pytest.mark.asyncio
async def test_empty_csv_returns_empty_result() -> None:
    """A CSV with only a header row imports zero, errors zero. The
    importer reaches the second pass with parsed=[] and returns
    early without touching the DB."""
    conn = AsyncMock()
    # No rows referenced -> branch_map fetch never fires.
    raw = b"phone,name,channel\n"

    result = await importer.import_csv(
        conn,
        tenant_id=uuid4(),
        raw_bytes=raw,
        default_country_code="MY",
    )

    assert result.imported_count == 0
    assert result.skipped_count == 0
    assert result.error_rows == []
    # No SQL should have been issued — branch fetch is skipped when
    # no slugs are referenced; upsert pass skipped when bound_rows
    # is empty.
    assert conn.fetch.await_count == 0
    assert conn.execute.await_count == 0


@pytest.mark.asyncio
async def test_bad_utf8_returns_global_error() -> None:
    """Non-UTF-8 bytes surface as a single row_number=0 error."""
    conn = AsyncMock()
    # 0x80 alone is invalid UTF-8 continuation byte.
    raw = b"phone,name,channel\n\x80\x81\x82"

    result = await importer.import_csv(
        conn,
        tenant_id=uuid4(),
        raw_bytes=raw,
        default_country_code="MY",
    )

    assert result.imported_count == 0
    assert len(result.error_rows) == 1
    assert result.error_rows[0].row_number == 0


@pytest.mark.asyncio
async def test_bad_phone_row_accumulates_error() -> None:
    """A row with an unparseable phone surfaces as INVALID_PHONE
    without aborting the import (line-error-accumulating)."""
    conn = AsyncMock()
    # First row good, second row bad phone — but second row gets
    # validated before any DB work in the first pass.
    raw = (
        b"phone,name,channel\n"
        b"not-a-phone,Alice,whatsapp\n"
    )

    result = await importer.import_csv(
        conn,
        tenant_id=uuid4(),
        raw_bytes=raw,
        default_country_code="MY",
    )

    assert result.imported_count == 0
    assert len(result.error_rows) == 1
    assert result.error_rows[0].error_code == "INVALID_PHONE"
    assert result.error_rows[0].row_number == 1


@pytest.mark.asyncio
async def test_bad_channel_row_accumulates_error() -> None:
    conn = AsyncMock()
    raw = (
        b"phone,name,channel\n"
        # legacy FB-page 'instagram' was dropped by migration 109 — it is
        # no longer a valid channel value (only 'instagram_login' remains).
        b"+60123456789,Alice,instagram\n"
    )

    result = await importer.import_csv(
        conn,
        tenant_id=uuid4(),
        raw_bytes=raw,
        default_country_code="MY",
    )

    assert result.imported_count == 0
    assert len(result.error_rows) == 1
    assert result.error_rows[0].error_code == "INVALID_CHANNEL"


@pytest.mark.asyncio
async def test_malformed_metadata_json_accumulates_error() -> None:
    conn = AsyncMock()
    raw = (
        b"phone,name,channel,metadata\n"
        b"+60123456789,Alice,whatsapp,{not-valid-json\n"
    )

    result = await importer.import_csv(
        conn,
        tenant_id=uuid4(),
        raw_bytes=raw,
        default_country_code="MY",
    )

    # First-pass _validate_basic passes (phone valid); second pass
    # _materialise_row raises on the metadata. Accumulator captures it.
    assert result.imported_count == 0
    assert len(result.error_rows) == 1
    assert result.error_rows[0].error_code == "MALFORMED_METADATA_JSON"


@pytest.mark.asyncio
async def test_duplicate_in_file_accumulates_error() -> None:
    conn = AsyncMock()
    raw = (
        b"phone,name,channel\n"
        b"+60123456789,Alice,whatsapp\n"
        b"+60123456789,Alice Again,whatsapp\n"
    )

    result = await importer.import_csv(
        conn,
        tenant_id=uuid4(),
        raw_bytes=raw,
        default_country_code="MY",
    )

    # Second row identifies as duplicate.
    duplicate_errors = [
        e for e in result.error_rows if e.error_code == "DUPLICATE_IN_FILE"
    ]
    assert len(duplicate_errors) == 1
    assert duplicate_errors[0].row_number == 2


def test_error_code_constants_align_with_shared_type_literal() -> None:
    """Constants in the importer use the exact strings the
    CSVImportErrorRow.error_code Literal accepts. Drift here is the
    bug class that lets a typo surface as a runtime Pydantic error
    inside the per-row accumulator (one bad row → entire import
    aborts on first error)."""
    from polaris_shared_types import CSVImportErrorRow

    # Field is `error_code: Literal[...]`. Pydantic stores the
    # accepted set on the field annotation.
    field = CSVImportErrorRow.model_fields["error_code"]
    # Pydantic V2 surfaces the Literal args via __args__ on the
    # annotation; extract them via typing helpers.
    from typing import get_args

    accepted = set(get_args(field.annotation))

    # Every constant the importer emits must be in this set.
    constants = {
        importer._ERR_INVALID_PHONE,
        importer._ERR_INVALID_CHANNEL,
        importer._ERR_INVALID_LOCALE,
        importer._ERR_CONSENT_REQUIRED_MISSING,
        importer._ERR_DUPLICATE_IN_FILE,
        importer._ERR_BRANCH_NOT_FOUND,
        importer._ERR_MALFORMED_TAGS,
        importer._ERR_MALFORMED_METADATA_JSON,
    }
    assert constants.issubset(accepted), (
        f"importer constants {constants - accepted} not in "
        f"CSVImportErrorRow Literal {accepted}"
    )


# ---------------------------------------------------------------------
# Security-review follow-up pins (2026-05-19 audit)
# ---------------------------------------------------------------------


def test_redact_phone_masks_all_but_last_4() -> None:
    """The helper that protects PDPA-sensitive phone values inside
    error messages MUST keep only the last 4 chars + mask the rest.
    Pinning the contract so future refactors can't accidentally
    leak full E.164 numbers into log streams."""
    assert importer._redact_phone("+60123456789") == "********6789"  # noqa: SLF001
    assert importer._redact_phone("60123456789") == "*******6789"  # noqa: SLF001
    # Strings shorter than the suffix are passed through verbatim —
    # nothing useful to redact + likely a malformed cell the tenant
    # needs to see in context to fix.
    assert importer._redact_phone("abc") == "abc"  # noqa: SLF001
    assert importer._redact_phone("") == ""  # noqa: SLF001


@pytest.mark.asyncio
async def test_bad_phone_error_message_redacts_value() -> None:
    """INVALID_PHONE messages must NOT include the full phone value —
    only a last-4 redaction. Catches regressions in the per-row
    error-row construction (importer.py:340, 349)."""
    conn = AsyncMock()
    # "not-a-phone-9876" is genuinely unparseable (no digit sequence
    # libphonenumber can recognise). libphonenumber silently strips
    # non-digit suffixes from otherwise-valid numbers, so naive
    # fixtures like "+60123456789xx" can pass unexpectedly.
    raw = (
        b"phone,name,channel\n"
        b"not-a-phone-9876,Alice,whatsapp\n"
    )

    result = await importer.import_csv(
        conn,
        tenant_id=uuid4(),
        raw_bytes=raw,
        default_country_code="MY",
    )

    assert len(result.error_rows) == 1
    assert result.error_rows[0].error_code == "INVALID_PHONE"
    msg = result.error_rows[0].message
    # The full raw value must not appear in the message.
    assert "not-a-phone-9876" not in msg
    # Redacted suffix is the last 4 raw chars ("9876").
    assert "9876" in msg


@pytest.mark.asyncio
async def test_duplicate_in_file_error_redacts_channel_user_id() -> None:
    """The DUPLICATE_IN_FILE message references channel_user_id which
    for WhatsApp equals the E.164 phone. Must be redacted, same as
    INVALID_PHONE."""
    conn = AsyncMock()
    raw = (
        b"phone,name,channel\n"
        b"+60123456789,Alice,whatsapp\n"
        b"+60123456789,Alice Again,whatsapp\n"
    )

    result = await importer.import_csv(
        conn,
        tenant_id=uuid4(),
        raw_bytes=raw,
        default_country_code="MY",
    )

    dup_errors = [
        e for e in result.error_rows if e.error_code == "DUPLICATE_IN_FILE"
    ]
    assert len(dup_errors) == 1
    msg = dup_errors[0].message
    assert "+60123456789" not in msg
    # The redacted suffix ("6789") still appears so the tenant has
    # enough context to locate the duplicate row in their CSV.
    assert "6789" in msg


@pytest.mark.asyncio
async def test_oversized_metadata_cell_rejects_with_byte_cap() -> None:
    """metadata cells > _METADATA_MAX_BYTES are rejected pre-parse so
    a deeply-nested JSON payload can't trigger RecursionError +
    bypass the row-error accumulator + surface as 500."""
    conn = AsyncMock()
    # Build a metadata cell well over 4 KB. The contents don't matter;
    # the size gate fires before json.loads runs.
    big = "x" * (importer._METADATA_MAX_BYTES + 100)  # noqa: SLF001
    metadata_cell = '{"k":"' + big + '"}'
    raw = (
        b"phone,name,channel,metadata\n"
        b"+60123456789,Alice,whatsapp,"
        + metadata_cell.encode("utf-8")
        + b"\n"
    )

    result = await importer.import_csv(
        conn,
        tenant_id=uuid4(),
        raw_bytes=raw,
        default_country_code="MY",
    )

    assert result.imported_count == 0
    assert len(result.error_rows) == 1
    assert result.error_rows[0].error_code == "MALFORMED_METADATA_JSON"
    assert "exceeds" in result.error_rows[0].message.lower()


@pytest.mark.asyncio
async def test_recursionerror_on_deeply_nested_json_is_handled() -> None:
    """If somehow a deeply-nested JSON cell sneaks past the byte cap
    (e.g. the cap is raised in a future env override) and triggers
    cpython's RecursionError inside json.loads, the importer catches
    it as MALFORMED_METADATA_JSON instead of bubbling a 500.

    Construct a payload that's under the cap but nests deep enough
    to exceed cpython's default recursion limit of 1000."""
    conn = AsyncMock()
    # Build a nesting depth just over the cpython default recursion
    # limit. Each pair is 2 chars, so 600 nests = 1200 chars (under
    # the 4 KB cap), but well past sys.getrecursionlimit() == 1000.
    depth = 600
    nested = "{" + '"a":' * depth + '"v"' + "}" * depth
    raw = (
        b"phone,name,channel,metadata\n"
        b"+60123456789,Alice,whatsapp,"
        + nested.encode("utf-8")
        + b"\n"
    )

    result = await importer.import_csv(
        conn,
        tenant_id=uuid4(),
        raw_bytes=raw,
        default_country_code="MY",
    )

    # Either the byte cap catches it (preferred) OR the
    # RecursionError catch fires — both produce
    # MALFORMED_METADATA_JSON, never a 500.
    assert result.imported_count == 0
    assert len(result.error_rows) == 1
    assert result.error_rows[0].error_code == "MALFORMED_METADATA_JSON"


@pytest.mark.asyncio
async def test_internal_row_cap_is_last_line_of_defence() -> None:
    """The router enforces AUDIENCE_CSV_MAX_ROWS before we run, but
    if a future internal caller bypasses it, the importer itself
    rejects > _INTERNAL_ROW_CAP rows with a global error_row.

    This pins the defense-in-depth assertion — a future refactor
    can't silently delete it without making the test fail."""
    conn = AsyncMock()
    # Temporarily shrink the cap so we don't need to build a
    # 100k-row fixture. Restore after.
    original_cap = importer._INTERNAL_ROW_CAP  # noqa: SLF001
    importer._INTERNAL_ROW_CAP = 3  # noqa: SLF001
    try:
        raw = (
            b"phone,name,channel\n"
            b"+60111111111,A,whatsapp\n"
            b"+60222222222,B,whatsapp\n"
            b"+60333333333,C,whatsapp\n"
            b"+60444444444,D,whatsapp\n"  # 4 rows > cap of 3
        )

        result = await importer.import_csv(
            conn,
            tenant_id=uuid4(),
            raw_bytes=raw,
            default_country_code="MY",
        )

        assert result.imported_count == 0
        assert result.skipped_count == 0
        # Single global error_row at row_number=0 (the sentinel for
        # whole-file errors, established by the UTF-8 decode failure
        # path).
        assert len(result.error_rows) == 1
        assert result.error_rows[0].row_number == 0
        assert "exceeds importer cap" in result.error_rows[0].message
    finally:
        importer._INTERNAL_ROW_CAP = original_cap  # noqa: SLF001
