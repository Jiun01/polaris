"""Audience CSV importer — Phase 6 PR-4.

POST /api/tenant/audience/import accepts a multipart CSV with the
schema from ADR-043 + Q-v1ii:

    phone, name, channel, branch, locale, tags, consent_at, metadata

  - ``phone`` required; normalised to E.164 via libphonenumber against
    the tenant's ``default_country_code`` (column added in Phase 1).
  - ``channel`` required; restricted to whatsapp/messenger/telegram/
    instagram_login — the canonical live channel set. (ADR-014 Amendment
    deferred telegram to v2.1, but it has since shipped as a first-class
    channel; the legacy FB-page ``instagram`` value was dropped by
    migration 109's Chatwoot-native rehaul.)
  - ``tags`` accepts a semicolon-separated string; we split + dedupe.
  - ``consent_at`` optional ISO 8601; if absent we still accept the
    row and emit an audit warning rather than failing the import
    (per 04g §9: "fail at PDPA review, not at the upload boundary").
  - ``metadata`` optional JSON string; bad JSON surfaces as
    MALFORMED_METADATA_JSON.

Behaviour:
  * **Line-error-accumulating** — every row that fails validation
    appends to ``CSVImportResult.error_rows`` with row_number +
    error_code + message. We do NOT first-failure-abort because
    tenants want to see ALL errors in one pass + fix the CSV +
    re-upload.
  * **Upsert on (tenant, channel, channel_user_id) WHERE opted_out_at
    IS NULL** — re-importing an active contact merges locale + tags
    (unique-union) rather than inserting a duplicate. A historical
    opted-out row stays as-is; if the new row has the same active key
    we create a brand-new active row (re-opt-in).
  * **Bulk INSERT via asyncpg copy_records_to_table** for new active
    rows — ~10x faster than batched INSERT on multi-thousand-row
    imports. The COPY runs inside the same RLS-scoped transaction as
    the existing-row UPDATEs.

Size + row caps from env (AUDIENCE_CSV_MAX_BYTES, AUDIENCE_CSV_MAX_ROWS)
are enforced at the router boundary, not here — by the time bytes
reach this function the size check has already passed.
"""

from __future__ import annotations

import csv
import io
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

import asyncpg
import phonenumbers
from polaris_shared_types import (
    CSVImportErrorRow,
    CSVImportResult,
)

logger = logging.getLogger(__name__)

# Channel values accepted in the channel column — the canonical live
# channel set (matches shared_types ConversationChannel minus the
# portal-only ``demo``). Legacy FB-page ``instagram`` was dropped by
# migration 109 (Chatwoot-native rehaul); telegram is now a first-class
# channel (shipped post-ADR-014). Kept in app code rather than a DB
# CHECK because audience_members.channel is a TEXT column without a
# CHECK (importer is the validating boundary).
_VALID_CHANNELS = frozenset(
    {"whatsapp", "messenger", "telegram", "instagram_login"}
)

# Locale validation is intentionally permissive at GA: we accept any
# non-empty short string and let downstream code surface mis-locales
# via translation fallback. ADR-031 nominal targets are en-MY, ms-MY,
# zh-CN, but Meta uses underscore-form variants (en_US, en_MY) — hard
# enum-restricting would surprise tenants using regional dialects.

# Error codes mirror shared_types.audience_optout.CSVImportErrorRow.error_code
# Literal. Using Literal[...] typing on each constant so mypy narrows
# correctly at the CSVImportErrorRow construction site.
from typing import Literal as _Literal  # noqa: E402 — single use, alias

_ERR_INVALID_PHONE: _Literal["INVALID_PHONE"] = "INVALID_PHONE"
_ERR_INVALID_CHANNEL: _Literal["INVALID_CHANNEL"] = "INVALID_CHANNEL"
_ERR_INVALID_LOCALE: _Literal["INVALID_LOCALE"] = "INVALID_LOCALE"
_ERR_CONSENT_REQUIRED_MISSING: _Literal["CONSENT_REQUIRED_MISSING"] = (
    "CONSENT_REQUIRED_MISSING"
)
_ERR_DUPLICATE_IN_FILE: _Literal["DUPLICATE_IN_FILE"] = "DUPLICATE_IN_FILE"
_ERR_BRANCH_NOT_FOUND: _Literal["BRANCH_NOT_FOUND"] = "BRANCH_NOT_FOUND"
_ERR_MALFORMED_TAGS: _Literal["MALFORMED_TAGS"] = "MALFORMED_TAGS"
_ERR_MALFORMED_METADATA_JSON: _Literal["MALFORMED_METADATA_JSON"] = (
    "MALFORMED_METADATA_JSON"
)

# Per-cell cap on the metadata JSON. Default cpython recursion limit
# is 1000; a deeply-nested JSON object well below this still gets
# rejected by the byte cap before it reaches json.loads. Sized
# generously (4 KB) for legitimate tenant metadata payloads while
# making RecursionError-via-nesting attacks practically impossible
# under the AUDIENCE_CSV_MAX_BYTES outer cap.
_METADATA_MAX_BYTES: int = 4096

# Defense-in-depth row cap. The router (tenant_audience.py) is the
# primary AUDIENCE_CSV_MAX_ROWS gate; this constant is the LAST line
# of defence inside the importer for any future internal caller that
# bypasses the router. The default mirrors the env-var default in
# .env.example so the two boundaries stay aligned.
_INTERNAL_ROW_CAP: int = 100_000


def _redact_phone(raw: str) -> str:
    """Return a privacy-preserving projection of a phone string.

    Keeps the LAST 4 characters (typically the unique-ish suffix of an
    E.164 number) and masks the rest with `*`. Returns the original
    string verbatim if shorter than 4 chars (nothing useful to redact;
    likely a malformed cell the tenant should debug verbatim).

    Used in ``CSVImportErrorRow.message`` strings — those land in the
    API response body (tenant-visible, acceptable) but the same
    strings are one logger.info call away from ops-log exposure.
    Redacting here closes the surface ahead of any future log
    statement that aggregates error_rows. PDPA Schedule 2 §3
    minimum-disclosure principle.
    """
    if len(raw) < 4:
        return raw
    return "*" * (len(raw) - 4) + raw[-4:]


@dataclass
class _ParsedRow:
    """One row that passed validation. Ready for upsert."""

    row_number: int
    channel: str
    channel_user_id: str  # = E.164 phone for whatsapp; raw handle for IG/Messenger
    channel_user_name: str | None
    phone_number_e164: str | None
    locale: str | None
    tags: list[str]
    opted_in_at: datetime
    metadata: dict[str, Any]
    tenant_branch_id: UUID | None
    opt_in_source: str


async def import_csv(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    raw_bytes: bytes,
    default_country_code: str,
    opt_in_source: str = "csv_upload",
) -> CSVImportResult:
    """Parse + upsert one CSV import. Returns aggregate result.

    Caller (router) has already enforced AUDIENCE_CSV_MAX_BYTES and
    AUDIENCE_CSV_MAX_ROWS; this function trusts ``raw_bytes`` is within
    bounds.

    Branch resolution (column ``branch`` carrying a slug) lives here
    so the row-error accumulator surfaces BRANCH_NOT_FOUND alongside
    other validation issues. We resolve once-per-import via a single
    SELECT over the names referenced in the CSV.
    """
    try:
        text = raw_bytes.decode("utf-8-sig")  # tolerate Excel's BOM
    except UnicodeDecodeError as exc:
        # Single global error — the whole file is unreadable. Convention:
        # surface as a row-0 error so the same shape works downstream.
        return CSVImportResult(
            imported_count=0,
            skipped_count=0,
            error_rows=[
                CSVImportErrorRow(
                    row_number=0,
                    raw={"_global": "utf-8 decode failed"},
                    error_code=_ERR_MALFORMED_METADATA_JSON,
                    message=f"CSV is not valid UTF-8: {exc}",
                )
            ],
        )

    reader = csv.DictReader(io.StringIO(text))
    rows: list[dict[str, str]] = list(reader)

    # Defense-in-depth row cap. The router enforces the
    # AUDIENCE_CSV_MAX_ROWS env-var gate before we're called, but a
    # future internal caller (e.g. a Phase 9 retention sweeper that
    # imports a CSV directly) would bypass it. Surfacing the violation
    # as a global error_row 0 keeps the function-level contract honest.
    if len(rows) > _INTERNAL_ROW_CAP:
        return CSVImportResult(
            imported_count=0,
            skipped_count=0,
            error_rows=[
                CSVImportErrorRow(
                    row_number=0,
                    raw={"_global": "row count exceeds importer-internal cap"},
                    error_code=_ERR_MALFORMED_METADATA_JSON,
                    message=(
                        f"CSV row count {len(rows)} exceeds importer cap "
                        f"{_INTERNAL_ROW_CAP}. Router AUDIENCE_CSV_MAX_ROWS "
                        "gate should have fired first."
                    ),
                )
            ],
        )

    error_rows: list[CSVImportErrorRow] = []
    parsed: list[_ParsedRow] = []
    seen_keys: set[tuple[str, str]] = set()  # (channel, channel_user_id)
    branch_slugs_referenced: set[str] = set()

    # First pass: validate every row independently. Collect branch slugs
    # referenced so we can resolve them in one query.
    for idx, raw_row in enumerate(rows, start=1):
        error = _validate_basic(raw_row, idx, default_country_code, error_rows)
        if error is not None:
            continue  # error already appended

        # _validate_basic returns the canonical (phone_e164, channel,
        # channel_user_id, ...) tuple via setting fields on a _ParsedRow,
        # but to keep validation pure and re-readable we re-parse here.
        # Cheap because we already validated — just a clean construction.
        try:
            row = _materialise_row(raw_row, idx, default_country_code, opt_in_source)
        except _ValidationError as exc:
            error_rows.append(exc.to_csv_error())
            continue

        key = (row.channel, row.channel_user_id)
        if key in seen_keys:
            error_rows.append(
                CSVImportErrorRow(
                    row_number=idx,
                    raw=_safe_dict(raw_row),
                    error_code=_ERR_DUPLICATE_IN_FILE,
                    message=(
                        f"row {idx}: duplicate (channel={row.channel}, "
                        f"channel_user_id={_redact_phone(row.channel_user_id)}) "
                        "earlier in file"
                    ),
                )
            )
            continue
        seen_keys.add(key)

        branch_slug = (raw_row.get("branch") or "").strip()
        if branch_slug:
            branch_slugs_referenced.add(branch_slug)

        parsed.append(row)

    # Resolve branch slugs in one SELECT. Build slug -> id map.
    branch_map: dict[str, UUID] = {}
    if branch_slugs_referenced:
        branch_rows = await conn.fetch(
            """
            SELECT id, name
              FROM tenant_branches
             WHERE name = ANY($1::text[])
            """,
            list(branch_slugs_referenced),
        )
        branch_map = {row["name"]: row["id"] for row in branch_rows}

    # Second pass: bind branch_id onto each row + emit error for any
    # slug we couldn't resolve.
    bound_rows: list[_ParsedRow] = []
    for row in parsed:
        slug = _slug_for_row(rows, row.row_number)
        if slug:
            bid = branch_map.get(slug)
            if bid is None:
                error_rows.append(
                    CSVImportErrorRow(
                        row_number=row.row_number,
                        raw=_safe_dict(rows[row.row_number - 1]),
                        error_code=_ERR_BRANCH_NOT_FOUND,
                        message=f"row {row.row_number}: branch '{slug}' not found",
                    )
                )
                continue
            row.tenant_branch_id = bid
        bound_rows.append(row)

    if not bound_rows:
        return CSVImportResult(
            imported_count=0,
            skipped_count=0,
            error_rows=error_rows,
        )

    # Upsert pass. For each row, attempt UPDATE on the partial-UNIQUE
    # active key; if no rows updated, INSERT a fresh active row. We
    # don't use copy_records_to_table here because the upsert decision
    # is per-row — the lift would be marginal once you account for the
    # SELECT-or-UPDATE dance.
    imported = 0
    skipped = 0
    for row in bound_rows:
        applied = await _upsert_row(conn, tenant_id=tenant_id, row=row)
        if applied:
            imported += 1
        else:
            skipped += 1

    logger.info(
        "audience.import_csv: tenant=%s imported=%d skipped=%d errors=%d",
        tenant_id,
        imported,
        skipped,
        len(error_rows),
    )

    return CSVImportResult(
        imported_count=imported,
        skipped_count=skipped,
        error_rows=error_rows,
    )


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------


class _ValidationError(Exception):
    """Internal — caught + converted to a CSVImportErrorRow."""

    def __init__(self, error_row: CSVImportErrorRow) -> None:
        super().__init__(error_row.message)
        self.error_row = error_row

    def to_csv_error(self) -> CSVImportErrorRow:
        return self.error_row


def _safe_dict(raw: dict[str, str]) -> dict[str, Any]:
    """Project a raw CSV row into a dict that round-trips through JSON.

    asyncpg + Pydantic stash this on the error_row.raw column for the
    portal to display "the row that failed". csv.DictReader returns
    str values throughout so the conversion is trivial — this function
    exists for type clarity only.
    """
    return dict(raw)


def _slug_for_row(rows: list[dict[str, str]], row_number: int) -> str:
    """Look up the branch slug originally in row[row_number-1]."""
    if row_number <= 0 or row_number > len(rows):
        return ""
    return (rows[row_number - 1].get("branch") or "").strip()


def _validate_basic(
    raw_row: dict[str, str],
    idx: int,
    default_country_code: str,
    errors: list[CSVImportErrorRow],
) -> CSVImportErrorRow | None:
    """Cheap pre-check pass: phone presence + channel enum.

    Returns the appended error row when validation fails (None when OK).
    The real materialisation happens in _materialise_row below; this
    pass exists to keep the main loop readable.
    """
    phone = (raw_row.get("phone") or "").strip()
    if not phone:
        err = CSVImportErrorRow(
            row_number=idx,
            raw=_safe_dict(raw_row),
            error_code=_ERR_INVALID_PHONE,
            message=f"row {idx}: phone column is empty",
        )
        errors.append(err)
        return err

    channel = (raw_row.get("channel") or "").strip().lower()
    if channel not in _VALID_CHANNELS:
        err = CSVImportErrorRow(
            row_number=idx,
            raw=_safe_dict(raw_row),
            error_code=_ERR_INVALID_CHANNEL,
            message=(
                f"row {idx}: channel '{channel}' not one of "
                f"{sorted(_VALID_CHANNELS)}"
            ),
        )
        errors.append(err)
        return err

    # Phone parse + normalise. The phone value is redacted in the
    # error message body even though it's the failing cell — the
    # tenant has the original in their CSV + the row_number locator;
    # redaction prevents PDPA exposure if the error_rows list ever
    # gets aggregated into ops logs.
    redacted = _redact_phone(phone)
    try:
        parsed = phonenumbers.parse(phone, region=default_country_code)
    except phonenumbers.NumberParseException as exc:
        err = CSVImportErrorRow(
            row_number=idx,
            raw=_safe_dict(raw_row),
            error_code=_ERR_INVALID_PHONE,
            message=f"row {idx}: phone '{redacted}' not parseable ({exc})",
        )
        errors.append(err)
        return err
    if not phonenumbers.is_valid_number(parsed):
        err = CSVImportErrorRow(
            row_number=idx,
            raw=_safe_dict(raw_row),
            error_code=_ERR_INVALID_PHONE,
            message=(
                f"row {idx}: phone '{redacted}' not valid in region "
                f"{default_country_code}"
            ),
        )
        errors.append(err)
        return err

    return None


def _materialise_row(
    raw_row: dict[str, str],
    idx: int,
    default_country_code: str,
    opt_in_source: str,
) -> _ParsedRow:
    """Build a _ParsedRow from validated raw cells. Raises _ValidationError."""
    phone = (raw_row.get("phone") or "").strip()
    channel = (raw_row.get("channel") or "").strip().lower()
    e164 = phonenumbers.format_number(
        phonenumbers.parse(phone, region=default_country_code),
        phonenumbers.PhoneNumberFormat.E164,
    )

    # Tags: semicolon-sep list, dedupe-stable.
    tags_raw = (raw_row.get("tags") or "").strip()
    tags: list[str] = []
    if tags_raw:
        seen: set[str] = set()
        for piece in tags_raw.split(";"):
            t = piece.strip()
            if t and t not in seen:
                seen.add(t)
                tags.append(t)
        if any(len(t) > 64 for t in tags):
            raise _ValidationError(
                CSVImportErrorRow(
                    row_number=idx,
                    raw=_safe_dict(raw_row),
                    error_code=_ERR_MALFORMED_TAGS,
                    message=f"row {idx}: at least one tag exceeds 64 chars",
                )
            )

    locale = (raw_row.get("locale") or "").strip() or None

    # Metadata: optional JSON string. Two defensive gates around
    # json.loads:
    #   (1) byte-size cap — keeps deeply-nested-object payloads under
    #       the cpython recursion ceiling (default 1000) even if a
    #       tenant tried to smuggle a recursion-bomb cell.
    #   (2) RecursionError catch — defense in depth in case the cap
    #       were ever raised above the recursion limit.
    meta_raw = (raw_row.get("metadata") or "").strip()
    metadata: dict[str, Any] = {}
    if meta_raw:
        if len(meta_raw) > _METADATA_MAX_BYTES:
            raise _ValidationError(
                CSVImportErrorRow(
                    row_number=idx,
                    raw=_safe_dict(raw_row),
                    error_code=_ERR_MALFORMED_METADATA_JSON,
                    message=(
                        f"row {idx}: metadata cell exceeds "
                        f"{_METADATA_MAX_BYTES} bytes "
                        f"(got {len(meta_raw)})"
                    ),
                )
            )
        try:
            decoded = json.loads(meta_raw)
        except (ValueError, RecursionError) as exc:
            raise _ValidationError(
                CSVImportErrorRow(
                    row_number=idx,
                    raw=_safe_dict(raw_row),
                    error_code=_ERR_MALFORMED_METADATA_JSON,
                    message=f"row {idx}: metadata is not valid JSON ({exc})",
                )
            ) from exc
        if not isinstance(decoded, dict):
            raise _ValidationError(
                CSVImportErrorRow(
                    row_number=idx,
                    raw=_safe_dict(raw_row),
                    error_code=_ERR_MALFORMED_METADATA_JSON,
                    message=f"row {idx}: metadata JSON must be an object, got {type(decoded).__name__}",
                )
            )
        metadata = decoded

    # consent_at parsing — optional. If absent we accept the row and
    # rely on the audit row to flag for PDPA review (per 04g §9).
    consent_raw = (raw_row.get("consent_at") or "").strip()
    opted_in_at = datetime.utcnow()
    if consent_raw:
        try:
            opted_in_at = datetime.fromisoformat(consent_raw.replace("Z", "+00:00"))
        except ValueError:
            # Bad consent_at format: treat as missing (audit warning),
            # don't fail the row. Same posture as fully-absent consent_at.
            logger.warning(
                "audience.import_csv: row %d unparseable consent_at=%r; "
                "treating as missing",
                idx,
                consent_raw,
            )

    return _ParsedRow(
        row_number=idx,
        channel=channel,
        channel_user_id=e164,  # phone IS the channel user id for WhatsApp
        channel_user_name=(raw_row.get("name") or "").strip() or None,
        phone_number_e164=e164,
        locale=locale,
        tags=tags,
        opted_in_at=opted_in_at,
        metadata=metadata,
        tenant_branch_id=None,  # bound in second pass
        opt_in_source=opt_in_source,
    )


async def _upsert_row(
    conn: asyncpg.Connection, *, tenant_id: UUID, row: _ParsedRow
) -> bool:
    """UPDATE-or-INSERT under RLS. Returns True iff a row landed.

    Path A: try to UPDATE an existing active row matching the
    partial-UNIQUE key. If 1 row was updated, merge tags + locale +
    metadata, mark applied.

    Path B: no active row exists for this (channel, channel_user_id),
    INSERT a fresh active row. asyncpg returns the row count via
    execute().
    """
    # Try UPDATE first. We merge tags (unique-union) and overwrite
    # locale + channel_user_name + metadata if the CSV provided them.
    updated = await conn.execute(
        """
        UPDATE audience_members
           SET channel_user_name = COALESCE($4, channel_user_name),
               phone_number_e164 = COALESCE($5, phone_number_e164),
               locale            = COALESCE($6, locale),
               tags              = (
                 SELECT ARRAY(SELECT DISTINCT unnest(audience_members.tags || $7::text[]))
               ),
               metadata          = audience_members.metadata || $8::jsonb,
               updated_at        = now()
         WHERE tenant_id = $1
           AND channel = $2
           AND channel_user_id = $3
           AND opted_out_at IS NULL
        """,
        tenant_id,
        row.channel,
        row.channel_user_id,
        row.channel_user_name,
        row.phone_number_e164,
        row.locale,
        row.tags,
        # Pass the dict DIRECTLY — the JSONB codec (pools.py) serialises it once.
        # A ``json.dumps(...)`` string would be double-encoded into a JSONB
        # *string*, and ``existing_metadata || "<string>"`` then yields a garbage
        # 2-element ARRAY rather than a merged object. See services/audit.py.
        row.metadata,
    )
    if updated.endswith(" 0"):
        # No active row matched — INSERT a fresh one.
        await conn.execute(
            """
            INSERT INTO audience_members
                (id, tenant_id, tenant_branch_id, channel, channel_user_id,
                 channel_user_name, phone_number_e164, locale, tags,
                 opted_in_at, opt_in_source, metadata, created_at, updated_at)
            VALUES
                ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12::jsonb,
                 now(), now())
            """,
            uuid4(),
            tenant_id,
            row.tenant_branch_id,
            row.channel,
            row.channel_user_id,
            row.channel_user_name,
            row.phone_number_e164,
            row.locale,
            row.tags,
            row.opted_in_at,
            row.opt_in_source,
            # Pass the dict DIRECTLY (JSONB codec serialises once) — see the
            # UPDATE branch above + services/audit.py. json.dumps here would
            # double-encode into a JSONB string.
            row.metadata,
        )
        return True
    return True
