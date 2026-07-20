# validators.py
# Layer 5 — Input Validation
# Pydantic models define the exact shape of valid input for each tool.
# Every tool call is validated against these models before any business
# logic runs. Bad input is rejected here with a clear error.

import re
from datetime import datetime
from pydantic import BaseModel, Field, field_validator
from error_codes import Errors, ErrorDef


# ── Shared helpers ────────────────────────────────────────────────────

# Employee identifier can be either a code (E001) or a name ("Vaidehi Gupta").
# Allows letters, digits, spaces, hyphens, apostrophes and dots — enough for
# most human names without allowing injection characters.
_EMPLOYEE_CODE_PATTERN = r"^[A-Za-z0-9\-\s\.']+$"
_EMPLOYEE_CODE_MAX = 60

def _validate_date_format(date_str: str) -> str:
    """
    Validates that a date string is either 'today' or in YYYY-MM-DD format.
    Raises ValueError if the format is wrong.
    """
    if not date_str or date_str.lower() == "today":
        return date_str

    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date_str):
        raise ValueError(
            f"Date '{date_str}' must be in YYYY-MM-DD format, e.g. 2026-07-07"
        )

    try:
        datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        raise ValueError(f"Date '{date_str}' is not a valid calendar date.")

    return date_str


# ── Tool input models ─────────────────────────────────────────────────

class EmployeeStatusInput(BaseModel):
    """Input for get_employee_status."""
    employee_code: str = Field(
        min_length=1,
        max_length=_EMPLOYEE_CODE_MAX,
        pattern=_EMPLOYEE_CODE_PATTERN,
        description="Employee code (e.g. E001) or employee name (e.g. Mohit)",
    )

    @field_validator("employee_code")
    @classmethod
    def strip_whitespace(cls, v: str) -> str:
        return v.strip()


class DailyBriefInput(BaseModel):
    """Input for get_daily_brief."""
    employee_code: str = Field(
        min_length=1,
        max_length=_EMPLOYEE_CODE_MAX,
        pattern=_EMPLOYEE_CODE_PATTERN,
        description="Employee code (e.g. E001) or employee name (e.g. Mohit)",
    )
    date: str = Field(
        default="today",
        description="Date in YYYY-MM-DD format, or 'today'. Defaults to today.",
    )

    @field_validator("employee_code")
    @classmethod
    def strip_employee_code(cls, v: str) -> str:
        return v.strip()

    @field_validator("date")
    @classmethod
    def validate_date(cls, v: str) -> str:
        return _validate_date_format(v)


class TopPerformersInput(BaseModel):
    """
    Input for get_top_performers.

    Accepts:
      - "daily", "weekly", "monthly"
      - a single date "YYYY-MM-DD"
      - a range "YYYY-MM-DD to YYYY-MM-DD"

    max_length raised to 30 to accommodate the range format.
    """
    period: str = Field(
        min_length=1,
        max_length=30,
        description=(
            "Period: 'daily', 'weekly', 'monthly', a specific date like "
            "'2026-07-16', or a range like '2026-07-01 to 2026-07-16'."
        ),
    )

    @field_validator("period")
    @classmethod
    def strip_period(cls, v: str) -> str:
        return v.strip()


# ── Validation entry point used by server.py ─────────────────────────

class ValidationError(Exception):
    """
    Raised when input validation fails.
    Caught by server.py and converted into a clean 400 response.
    """
    def __init__(self, error: ErrorDef, message: str | None = None):
        self.error = error
        self.message = message or error.message
        super().__init__(self.message)


def validate_input(model_class, raw_input: dict) -> object:
    """
    Validate raw input against a Pydantic model.
    Returns the validated model instance if valid.
    Raises ValidationError with a clean message if invalid.
    """
    try:
        return model_class(**raw_input)
    except Exception as e:
        error_detail = str(e)
        # Prefer the specific date error when a date field is at fault.
        # We check for both the field name and the format string to avoid
        # false positives on the word "date" appearing incidentally.
        lower = error_detail.lower()
        is_date_error = (
            "yyyy-mm-dd" in lower
            or "not a valid calendar date" in lower
        )
        if is_date_error:
            raise ValidationError(Errors.INVALID_DATE_FORMAT, message=error_detail)

        raise ValidationError(
            Errors.INVALID_PARAMETER,
            message=f"{Errors.INVALID_PARAMETER.message} {error_detail}",
        )