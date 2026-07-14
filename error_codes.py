# error_codes.py
# Single source of truth for every error this server can return.
# One ErrorDef object holds code, message, and HTTP status together.
# Adding a new error = one block. Nothing to keep in sync manually.

from dataclasses import dataclass


@dataclass(frozen=True)
class ErrorDef:
    """
    A single error definition — code, human message, and HTTP status
    in one immutable object. frozen=True means these are constants.

    Usage:
        raise AuthError(Errors.UNAUTHENTICATED)
        e.error.code          → "UNAUTHENTICATED"
        e.error.message       → "Missing or invalid API key."
        e.error.http_status   → 401
    """
    code: str
    message: str
    http_status: int

    def __str__(self) -> str:
        return self.code


class Errors:

    # ── Client errors (caller's fault) ───────────────────────────
    EMPLOYEE_NOT_FOUND = ErrorDef(
        code="EMPLOYEE_NOT_FOUND",
        message="No employee found with that code.",
        http_status=404,
    )
    INVALID_PARAMETER = ErrorDef(
        code="INVALID_PARAMETER",
        message="One or more parameters are invalid.",
        http_status=400,
    )
    INVALID_DATE_FORMAT = ErrorDef(
        code="INVALID_DATE_FORMAT",
        message="Date must be in YYYY-MM-DD format, e.g. 2026-07-07.",
        http_status=400,
    )
    NO_DATA_FOR_DATE = ErrorDef(
        code="NO_DATA_FOR_DATE",
        message="No activity data found for that date.",
        http_status=404,
    )

    # ── Security errors ───────────────────────────────────────────
    UNAUTHENTICATED = ErrorDef(
        code="UNAUTHENTICATED",
        message="Missing or invalid API key.",
        http_status=401,
    )
    FORBIDDEN = ErrorDef(
        code="FORBIDDEN",
        message="You do not have permission to access this resource.",
        http_status=403,
    )
    RATE_LIMITED = ErrorDef(
        code="RATE_LIMITED",
        message="Too many requests. Please wait before trying again.",
        http_status=429,
    )

    # ── Server errors (our fault) ─────────────────────────────────
    SHEETS_UNAVAILABLE = ErrorDef(
        code="SHEETS_UNAVAILABLE",
        message="Could not reach the data source. Please try again shortly.",
        http_status=503,
    )
    INTERNAL_ERROR = ErrorDef(
        code="INTERNAL_ERROR",
        message="An unexpected error occurred.",
        http_status=500,
    )