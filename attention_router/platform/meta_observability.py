"""Privacy-safe observability helpers for Meta provider identifiers."""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Any


_SAFE_CODE = re.compile(r"[A-Za-z0-9_]{1,120}")
_SAFE_SQLSTATE = re.compile(r"[A-Z0-9]{5}")


def provider_message_id_fingerprint(value: Any) -> str | None:
    """Return the canonical irreversible fingerprint used in audit metadata."""

    if not isinstance(value, str) or not value:
        return None
    return hashlib.sha256(value.encode()).hexdigest()


def safe_exception_metadata(exc: BaseException) -> tuple[str, str | None, str | None]:
    """Extract only allowlisted exception metadata; never stringify the exception."""

    error_type = type(exc).__name__
    if _SAFE_CODE.fullmatch(error_type) is None:
        error_type = "Exception"
    original = getattr(exc, "orig", None)
    sqlstate = getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)
    if not isinstance(sqlstate, str) or _SAFE_SQLSTATE.fullmatch(sqlstate) is None:
        sqlstate = None
    diagnostic = getattr(original, "diag", None)
    constraint = getattr(diagnostic, "constraint_name", None)
    if not isinstance(constraint, str) or _SAFE_CODE.fullmatch(constraint) is None:
        constraint = None
    return error_type, sqlstate, constraint


def log_sanitized_exception(
    target: logging.Logger,
    message: str,
    *message_args: object,
    exc: BaseException,
) -> None:
    """Log a failure without exception text, parameters, traceback, or raw payload."""

    error_type, sqlstate, constraint = safe_exception_metadata(exc)
    target.error(
        message + " error_type=%s sqlstate=%s constraint=%s",
        *message_args,
        error_type,
        sqlstate or "NONE",
        constraint or "NONE",
    )


__all__ = [
    "log_sanitized_exception",
    "provider_message_id_fingerprint",
    "safe_exception_metadata",
]
