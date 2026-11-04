"""Structured JSON logging per docs/Polaris V2/07_TESTING_OPS_AND_COMPLIANCE.md §4.1.

Field list (Phase 0 baseline; Phase 1+ extends with tenant_id, request_id, etc.):
  timestamp, level, logger, message, environment
"""

from __future__ import annotations

import logging
import sys

from pythonjsonlogger import jsonlogger

from polaris_api.config import get_settings


class _PolarisJsonFormatter(jsonlogger.JsonFormatter):
    """Adds the environment field to every record.

    The environment value is captured once at formatter construction (in
    `configure_logging`) instead of via `get_settings()` per record — log
    records can fire at any rate, and the per-record env lookup is wasted
    work since `environment` is immutable for the process lifetime.
    """

    def __init__(self, *args: object, environment: str = "local", **kwargs: object) -> None:
        # Pull `environment` out of kwargs before delegating; the parent
        # JsonFormatter doesn't accept it.
        self._environment = environment
        # python-json-logger's JsonFormatter __init__ is untyped; we already
        # ignore-missing-imports on `pythonjsonlogger.*`. Suppress the call too.
        super().__init__(*args, **kwargs)  # type: ignore[no-untyped-call]

    def add_fields(
        self,
        log_record: dict[str, object],
        record: logging.LogRecord,
        message_dict: dict[str, object],
    ) -> None:
        super().add_fields(log_record, record, message_dict)
        log_record.setdefault("environment", self._environment)
        log_record.setdefault("logger", record.name)
        if "level" not in log_record:
            log_record["level"] = record.levelname


# Module-level guard so `configure_logging()` is genuinely idempotent under
# concurrent startup (uvicorn's worker init + lifespan + test-suite setup can
# all race for the first call). Without this guard, the handler-removal loop
# could clear handlers added by another caller and produce double-emitted lines.
_configured: bool = False


def configure_logging() -> None:
    """Configure root logger for structured JSON output. Idempotent."""
    global _configured
    if _configured:
        return

    settings = get_settings()

    root = logging.getLogger()
    # Clear any previous handlers (e.g. uvicorn's default text handler).
    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.StreamHandler(stream=sys.stdout)
    # Our subclass now has a typed __init__; no ignore needed at this call site.
    formatter = _PolarisJsonFormatter(
        fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
        rename_fields={"asctime": "timestamp", "levelname": "level"},
        environment=settings.environment,
    )
    handler.setFormatter(formatter)
    root.addHandler(handler)
    root.setLevel(settings.log_level)

    # Quiet uvicorn's access logger noise; keep error logs.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

    # Pin httpx/httpcore to WARNING even under a global DEBUG log level: their
    # DEBUG logs emit the full request URL, which for the Telegram Bot API
    # carries the bot token in the path. Suppressing here removes that leak
    # vector regardless of LOG_LEVEL (off-Meta channels — security review).
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    _configured = True


def reset_logging_for_tests() -> None:
    """Reset the idempotency guard. Test fixtures only — never call in prod."""
    global _configured
    _configured = False
