# sheets_client.py
# Layer 6 — Data Access
# Connects to Google Sheets using service account credentials.
# Provides clean functions for reading employee and activity data.
# Everything else in the system calls these functions — never the Google API directly.
#
# FIXES APPLIED IN THIS VERSION:
# Fix T-5  — Startup schema validation: warns immediately if any expected
#             column is missing or renamed in the sheet.
# Fix T-8  — Duplicate date rows: last row per date wins.
# Fix T-9  — Corrupted date formats: skipped cleanly.

import os
import logging
from datetime import datetime
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from dotenv import load_dotenv
from error_codes import Errors

load_dotenv()

# ── Configuration ─────────────────────────────────────────────────────
SHEET_ID         = os.environ.get("SHEET_ID", "")
CREDENTIALS_FILE = os.environ.get("GOOGLE_CREDENTIALS_FILE", "/app/credentials.json")

# ── Write credentials.json from base64 env var if present ────────────
_creds_b64 = os.environ.get("GOOGLE_CREDENTIALS_BASE64", "")
if _creds_b64 and not os.path.exists(CREDENTIALS_FILE):
    import base64
    with open(CREDENTIALS_FILE, "wb") as _f:
        _f.write(base64.b64decode(_creds_b64))

SCOPES        = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
TAB_EMPLOYEES = "Employees"
TAB_ACTIVITY  = "DailyActivity"

_log = logging.getLogger("workwitness-mcp")


# ── Sheets API client ─────────────────────────────────────────────────

def _build_service():
    if not SHEET_ID:
        raise RuntimeError("SHEET_ID is not set in your .env file.")
    credentials = service_account.Credentials.from_service_account_file(
        CREDENTIALS_FILE, scopes=SCOPES
    )
    return build("sheets", "v4", credentials=credentials)


try:
    _service = _build_service()
except Exception as e:
    raise RuntimeError(f"Failed to connect to Google Sheets API: {e}")


# ── Error class ───────────────────────────────────────────────────────

class SheetsError(Exception):
    """
    Raised when the Google Sheets API call fails.
    Carries the full ErrorDef so server.py has code, message, and HTTP status.
    """
    def __init__(self, error, message: str | None = None):
        self.error = error
        self.message = message or error.message
        super().__init__(self.message)


# ── Internal helpers ──────────────────────────────────────────────────

def _read_tab(tab_name: str, cell_range: str) -> list:
    """
    Reads raw rows from a specific tab and cell range.
    Returns a list of rows. Returns empty list if no data.
    """
    try:
        result = (
            _service.spreadsheets()
            .values()
            .get(spreadsheetId=SHEET_ID, range=f"{tab_name}!{cell_range}")
            .execute()
        )
        return result.get("values", [])

    except HttpError as e:
        if e.resp.status == 403:
            raise SheetsError(Errors.FORBIDDEN, "Service account does not have access to this sheet.")
        if e.resp.status == 404:
            raise SheetsError(Errors.SHEETS_UNAVAILABLE, "Sheet not found. Check your SHEET_ID in .env.")
        if e.resp.status == 400:
            raise SheetsError(
                Errors.SHEETS_UNAVAILABLE,
                f"Tab '{tab_name}' was not found in the sheet. "
                f"Please check that the tab name has not been renamed or deleted."
            )
        raise SheetsError(Errors.SHEETS_UNAVAILABLE)


def _parse_date_safe(date_str: str):
    """Fix T-9 — Safely parses YYYY-MM-DD. Returns None if invalid."""
    try:
        return datetime.strptime(date_str.strip(), "%Y-%m-%d")
    except (ValueError, AttributeError):
        return None


# ── Startup schema validation (Fix T-5) ──────────────────────────────

REQUIRED_EMPLOYEE_COLUMNS = {"employee_code", "name", "department", "joining_date"}
REQUIRED_ACTIVITY_COLUMNS = {
    "date", "employee_code", "hours_worked", "stage",
    "focus_score", "tasks_completed", "blockers", "daily_brief"
}


def validate_sheet_schema() -> None:
    try:
        emp_rows = _read_tab(TAB_EMPLOYEES, "A1:D1")
        if emp_rows:
            missing = REQUIRED_EMPLOYEE_COLUMNS - {h.strip().lower() for h in emp_rows[0]}
            if missing:
                _log.warning(f"SCHEMA WARNING: Employees tab missing columns: {missing}")
    except Exception:
        pass

    try:
        act_rows = _read_tab(TAB_ACTIVITY, "A1:H1")
        if act_rows:
            missing = REQUIRED_ACTIVITY_COLUMNS - {h.strip().lower() for h in act_rows[0]}
            if missing:
                _log.warning(f"SCHEMA WARNING: DailyActivity tab missing columns: {missing}")
    except Exception:
        pass


validate_sheet_schema()


# ── Public functions ──────────────────────────────────────────────────

def get_all_employees() -> list:
    rows = _read_tab(TAB_EMPLOYEES, "A1:D1000")
    if not rows or len(rows) < 2:
        return []
    headers = [h.strip().lower() for h in rows[0]]
    return [dict(zip(headers, row + [""] * (len(headers) - len(row)))) for row in rows[1:]]


def get_employee_by_code(employee_code: str):
    for emp in get_all_employees():
        if emp.get("employee_code", "").strip().upper() == employee_code.strip().upper():
            return emp
    return None


def get_activity_for_date(date_str: str) -> list:
    rows = _read_tab(TAB_ACTIVITY, "A1:H1000")
    if not rows or len(rows) < 2:
        return []
    headers = [h.strip().lower() for h in rows[0]]
    activities = []
    for row in rows[1:]:
        record = dict(zip(headers, row + [""] * (len(headers) - len(row))))
        if record.get("date", "").strip() == date_str.strip():
            activities.append(record)
    return activities


def get_activity_for_employee(employee_code: str, days: int = 7) -> list:
    rows = _read_tab(TAB_ACTIVITY, "A1:H1000")
    if not rows or len(rows) < 2:
        return []
    headers = [h.strip().lower() for h in rows[0]]
    matching = []
    for row in rows[1:]:
        record = dict(zip(headers, row + [""] * (len(headers) - len(row))))
        if record.get("employee_code", "").strip().upper() == employee_code.strip().upper():
            if _parse_date_safe(record.get("date", "")):  # Fix T-9
                matching.append(record)
    matching.sort(key=lambda r: _parse_date_safe(r.get("date", "")), reverse=True)
    return matching[:days]


def get_latest_activity_for_employee(employee_code: str):
    rows = _read_tab(TAB_ACTIVITY, "A1:H1000")
    if not rows or len(rows) < 2:
        return None
    headers = [h.strip().lower() for h in rows[0]]
    matching = []
    for row in rows[1:]:
        record = dict(zip(headers, row + [""] * (len(headers) - len(row))))
        if record.get("employee_code", "").strip().upper() == employee_code.strip().upper():
            if _parse_date_safe(record.get("date", "")):  # Fix T-9
                matching.append(record)
    if not matching:
        return None
    by_date = {}
    for record in matching:
        by_date[record.get("date", "")] = record  # Fix T-8: last row per date wins
    sorted_dates = sorted(by_date.keys(), key=lambda d: _parse_date_safe(d), reverse=True)
    return by_date[sorted_dates[0]]