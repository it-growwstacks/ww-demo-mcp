# validators.py
# Layer 5 — Input Validation
# Pydantic models define the exact shape of valid input for each tool.
# Every tool call is validated against these models before any
# business logic runs. Bad input is rejected here with a clear error —
# it never reaches the Google Sheets API.

import re
from datetime import datetime
from pydantic import BaseModel, Field, field_validator
from error_codes import Errors, ErrorDef


# ── Date format validator (reused across multiple tools) ──────────────

def validate_date_format(date_str: str) -> str:
    """
    Validates that a date string is in YYYY-MM-DD format.
    Raises ValueError if the format is wrong.
    """
    if not date_str or date_str.lower() == "today":
        return date_str

    pattern = r"^\d{4}-\d{2}-\d{2}$"
    if not re.match(pattern, date_str):
        raise ValueError(
            f"Date '{date_str}' must be in YYYY-MM-DD format, e.g. 2026-07-07"
        )

    try:
        datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        raise ValueError(f"Date '{date_str}' is not a valid calendar date.")

    return date_str


# ── Input model for get_employee_status ──────────────────────────────

class EmployeeStatusInput(BaseModel):
    employee_code: str = Field(
        min_length=1,
        max_length=20,
        pattern=r'^[A-Za-z0-9\-]+$',
        description="Employee code from the Employees sheet, e.g. E001"
    )
    model_config = {"populate_by_name": True}

    @field_validator("employee_code")
    @classmethod
    def strip_whitespace(cls, v: str) -> str:
        return v.strip()


# ── Input model for get_team_summary ─────────────────────────────────

class TeamSummaryInput(BaseModel):
    date: str = Field(default="today", description="Date in YYYY-MM-DD format. Defaults to today.")

    @field_validator("date")
    @classmethod
    def validate_date(cls, v: str) -> str:
        return validate_date_format(v)


# ── Input model for get_daily_brief ──────────────────────────────────

class DailyBriefInput(BaseModel):
    employee_code: str = Field(min_length=1, max_length=20, description="Employee code, e.g. E001")
    date: str = Field(default="today", description="Date in YYYY-MM-DD format. Defaults to today.")

    @field_validator("employee_code")
    @classmethod
    def strip_employee_code(cls, v: str) -> str:
        return v.strip()

    @field_validator("date")
    @classmethod
    def validate_date(cls, v: str) -> str:
        return validate_date_format(v)


# ── Input model for get_employee_history ─────────────────────────────

class EmployeeHistoryInput(BaseModel):
    employee_code: str = Field(min_length=1, max_length=20, description="Employee code, e.g. E001")
    days: int = Field(default=7, ge=1, le=30, description="Number of days of history. Min 1, max 30.")

    @field_validator("employee_code")
    @classmethod
    def strip_employee_code(cls, v: str) -> str:
        return v.strip()


# ── Validation helper used by server.py ──────────────────────────────

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
    Validates raw input against a Pydantic model.
    Returns the validated model instance if valid.
    Raises ValidationError with a clean message if invalid.
    """
    try:
        return model_class(**raw_input)
    except Exception as e:
        error_detail = str(e)
        if "date" in error_detail.lower():
            raise ValidationError(Errors.INVALID_DATE_FORMAT)
        raise ValidationError(
            Errors.INVALID_PARAMETER,
            message=f"{Errors.INVALID_PARAMETER.message} {error_detail}"
        )