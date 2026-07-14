# sheets_client.py
# Layer 6 — Data Access
# Connects to Google Sheets using service account credentials.
# Provides clean functions for reading employee and activity data.
# Everything else in the system calls these functions — never the Google API directly.
#
# FIXES APPLIED IN THIS VERSION:
# Fix T-5  — Startup schema validation: warns immediately if any expected
#             column is missing or renamed in the sheet, instead of silently
#             returning EMPLOYEE_NOT_FOUND for every employee.
# Fix T-8  — Duplicate date rows: when two rows exist for the same employee
#             on the same date, the LAST row (most recently entered) wins,
#             not the first by sheet order.
# Fix T-9  — Corrupted date formats: rows with dates that cannot be parsed
#             as YYYY-MM-DD are skipped cleanly rather than corrupting the
#             sort order and returning the wrong record silently.
# Fix T-7  — Handled in server.py (staleness warning) — no change needed here.

import os
import logging
from datetime import datetime
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from dotenv import load_dotenv
from error_codes import ErrorCode, ErrorMessage

load_dotenv()

# ── Configuration ─────────────────────────────────────────────────────
# ── Configuration ─────────────────────────────────────────────────────
SHEET_ID         = os.environ.get("SHEET_ID", "")
CREDENTIALS_FILE = os.environ.get("GOOGLE_CREDENTIALS_FILE", "credentials.json")

# ── Write credentials.json from base64 env var if present ────────────
# Railway cannot mount local files, so the service account JSON is
# stored as a base64-encoded environment variable and written to disk
# at startup. Safe — the container filesystem is ephemeral and isolated.
_creds_b64 = os.environ.get("GOOGLE_CREDENTIALS_BASE64", "")
if _creds_b64 and not os.path.exists(CREDENTIALS_FILE):
    import base64
    with open(CREDENTIALS_FILE, "wb") as _f:
        _f.write(base64.b64decode(_creds_b64))

SCOPES           = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
TAB_EMPLOYEES    = "Employees"
TAB_ACTIVITY     = "DailyActivity"

# ── Logger for schema warnings ────────────────────────────────────────
_log = logging.getLogger("workwitness-mcp")


# ── Sheets API client ─────────────────────────────────────────────────

def _build_service():
    if not SHEET_ID:
        raise RuntimeError("SHEET_ID is not set in your .env file.")
    credentials = service_account.Credentials.from_service_account_file(
        CREDENTIALS_FILE,
        scopes=SCOPES
    )
    return build("sheets", "v4", credentials=credentials)


try:
    _service = _build_service()
except Exception as e:
    raise RuntimeError(f"Failed to connect to Google Sheets API: {e}")


# ── Error class ───────────────────────────────────────────────────────

class SheetsError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


# ── Internal helpers ──────────────────────────────────────────────────

def _read_tab(tab_name: str, cell_range: str) -> list:
    """
    Reads raw rows from a specific tab and cell range.
    Returns a list of rows where each row is a list of cell values.
    Returns an empty list if the tab has no data.

    Handles the most common Google Sheets API errors:
    - 403: service account has no permission → FORBIDDEN
    - 404: sheet or tab not found → SHEETS_UNAVAILABLE
    - 400: tab name is wrong (deleted or renamed) → SHEETS_UNAVAILABLE
    - other: generic unavailable
    """
    try:
        result = (
            _service.spreadsheets()
            .values()
            .get(
                spreadsheetId=SHEET_ID,
                range=f"{tab_name}!{cell_range}"
            )
            .execute()
        )
        return result.get("values", [])

    except HttpError as e:
        if e.resp.status == 403:
            raise SheetsError(
                code=ErrorCode.FORBIDDEN,
                message="Service account does not have access to this sheet."
            )
        if e.resp.status == 404:
            raise SheetsError(
                code=ErrorCode.SHEETS_UNAVAILABLE,
                message="Sheet not found. Check your SHEET_ID in .env."
            )
        if e.resp.status == 400:
            # Fix — specific message when a tab is deleted or renamed
            # Previously returned the generic "try again" message which
            # made users think it was a temporary outage, not a config problem.
            raise SheetsError(
                code=ErrorCode.SHEETS_UNAVAILABLE,
                message=(
                    f"Tab '{tab_name}' was not found in the sheet. "
                    f"Please check that the tab name has not been renamed or deleted."
                )
            )
        raise SheetsError(
            code=ErrorCode.SHEETS_UNAVAILABLE,
            message=ErrorMessage.SHEETS_UNAVAILABLE
        )


def _parse_date_safe(date_str: str):
    """
    Fix T-9 — Safely parses a date string in YYYY-MM-DD format.

    Returns a datetime object if valid, None if the format is wrong.
    Used to:
    1. Skip rows with corrupted date formats during activity lookup
    2. Sort activity records by date correctly

    Without this, a row with "07-01-2026" instead of "2026-07-01"
    sorts to the wrong position and the wrong record is returned silently.
    """
    try:
        return datetime.strptime(date_str.strip(), "%Y-%m-%d")
    except (ValueError, AttributeError):
        return None


# ── Fix T-5 — Startup schema validation ──────────────────────────────
#
# Reads the header rows of both tabs when the server starts.
# If any expected column is missing or renamed, logs a WARNING immediately.
#
# Without this fix, a renamed column causes every employee lookup
# to return EMPLOYEE_NOT_FOUND silently — looks identical to a
# "code does not exist" error but is actually a configuration problem.
# An engineer could spend hours debugging before realising a column
# was renamed. This warning catches it the moment the server starts.

REQUIRED_EMPLOYEE_COLUMNS = {
    "employee_code", "name", "department", "joining_date"
}
REQUIRED_ACTIVITY_COLUMNS = {
    "date", "employee_code", "hours_worked", "stage",
    "focus_score", "tasks_completed", "blockers", "daily_brief"
}


def validate_sheet_schema() -> None:
    """
    Reads sheet headers at startup and checks all expected columns exist.
    Logs a WARNING for any missing or renamed columns.

    Called once when this module is first imported (at server start).
    Never raises — a schema warning should not prevent the server from
    starting, it should just alert the engineer.
    """
    try:
        emp_rows = _read_tab(TAB_EMPLOYEES, "A1:D1")
        if emp_rows:
            actual = {h.strip().lower() for h in emp_rows[0]}
            missing = REQUIRED_EMPLOYEE_COLUMNS - actual
            if missing:
                _log.warning(
                    f"SCHEMA WARNING: Employees tab is missing expected columns: "
                    f"{missing}. Employee lookups will fail silently until fixed."
                )
    except Exception:
        pass  # schema validation failure never prevents server startup

    try:
        act_rows = _read_tab(TAB_ACTIVITY, "A1:H1")
        if act_rows:
            actual = {h.strip().lower() for h in act_rows[0]}
            missing = REQUIRED_ACTIVITY_COLUMNS - actual
            if missing:
                _log.warning(
                    f"SCHEMA WARNING: DailyActivity tab is missing expected columns: "
                    f"{missing}. Activity lookups may return incomplete data."
                )
    except Exception:
        pass


# Run schema validation once when this module is imported (server startup)
validate_sheet_schema()


# ── Public functions called by tool handlers ──────────────────────────

def get_all_employees() -> list:
    """
    Reads all rows from the Employees tab.
    Returns a list of employee dicts with keys:
    employee_code, name, department, joining_date

    Headers are normalised with .strip().lower() so accidental spaces
    in column names do not cause silent lookup failures.
    Short rows are padded with empty strings to match header count.
    """
    rows = _read_tab(TAB_EMPLOYEES, "A1:D1000")
    if not rows or len(rows) < 2:
        return []
    headers = [h.strip().lower() for h in rows[0]]
    employees = []
    for row in rows[1:]:
        padded = row + [""] * (len(headers) - len(row))
        employees.append(dict(zip(headers, padded)))
    return employees


def get_employee_by_code(employee_code: str):
    """
    Finds one employee by their employee code.
    Returns the employee dict, or None if not found.

    Comparison is case-insensitive and strips whitespace on both sides
    so "e001", "E001", " E001 " all match "E001" in the sheet.
    """
    employees = get_all_employees()
    for emp in employees:
        if emp.get("employee_code", "").strip().upper() == employee_code.strip().upper():
            return emp
    return None


def get_activity_for_date(date_str: str) -> list:
    """
    Reads all activity rows for a specific date from the DailyActivity tab.
    Returns a list of activity dicts for that date.

    date_str must be in YYYY-MM-DD format, e.g. "2026-07-07"
    """
    rows = _read_tab(TAB_ACTIVITY, "A1:H1000")
    if not rows or len(rows) < 2:
        return []
    headers = [h.strip().lower() for h in rows[0]]
    activities = []
    for row in rows[1:]:
        padded = row + [""] * (len(headers) - len(row))
        record = dict(zip(headers, padded))
        if record.get("date", "").strip() == date_str.strip():
            activities.append(record)
    return activities


def get_activity_for_employee(employee_code: str, days: int = 7) -> list:
    """
    Reads the last N days of activity for one specific employee.
    Returns activity records sorted newest first.

    employee_code: e.g. "E001" — case-insensitive match
    days         : how many days of history to return (default 7)

    Fix T-9 — rows with corrupted date formats are excluded from
    the results entirely rather than corrupting the sort order.
    String sorting of "07-01-2026" vs "2026-07-01" produces wrong
    order — only valid YYYY-MM-DD dates are included.
    """
    rows = _read_tab(TAB_ACTIVITY, "A1:H1000")
    if not rows or len(rows) < 2:
        return []
    headers = [h.strip().lower() for h in rows[0]]
    matching = []
    for row in rows[1:]:
        padded = row + [""] * (len(headers) - len(row))
        record = dict(zip(headers, padded))
        if record.get("employee_code", "").strip().upper() == employee_code.strip().upper():
            # Fix T-9 — only include rows with valid YYYY-MM-DD date format
            if _parse_date_safe(record.get("date", "")):
                matching.append(record)

    # Sort by parsed date descending — safe because all dates are valid here
    matching.sort(
        key=lambda r: _parse_date_safe(r.get("date", "")),
        reverse=True
    )
    return matching[:days]


def get_latest_activity_for_employee(employee_code: str):
    """
    Returns the single most recent activity record for one employee.
    Used by get_employee_status to show current state.

    Fix T-8 — when two rows exist for the same employee on the same
    date (data entry error or afternoon update), the LAST row by sheet
    order wins. Previously the first row was returned, which might be
    an outdated morning entry rather than the updated afternoon one.

    Fix T-9 — rows with corrupted date formats are excluded entirely
    via get_activity_for_employee, which now validates all dates before
    sorting. A row with "07-01-2026" no longer pollutes the sort order.

    Returns None if no valid activity records exist for this employee.
    """
    rows = _read_tab(TAB_ACTIVITY, "A1:H1000")
    if not rows or len(rows) < 2:
        return None

    headers = [h.strip().lower() for h in rows[0]]
    matching = []

    for row in rows[1:]:
        padded = row + [""] * (len(headers) - len(row))
        record = dict(zip(headers, padded))
        if record.get("employee_code", "").strip().upper() == employee_code.strip().upper():
            # Fix T-9 — skip rows with corrupted date formats
            if _parse_date_safe(record.get("date", "")):
                matching.append(record)

    if not matching:
        return None

    # Fix T-8 — group by date, take LAST row per date (not first)
    # This means an afternoon update correctly overwrites a morning entry
    # when both have the same date. Sheet row order determines "last".
    by_date = {}
    for record in matching:
        date_key = record.get("date", "")
        by_date[date_key] = record  # later rows overwrite earlier ones

    # Sort the date keys and return the record for the most recent date
    sorted_dates = sorted(
        by_date.keys(),
        key=lambda d: _parse_date_safe(d),
        reverse=True
    )

    return by_date[sorted_dates[0]]