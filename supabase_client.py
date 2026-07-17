# supabase_client.py
# Layer 6 — Data Fetching (Supabase PostgreSQL)
# Replaces sheets_client.py. Same interface, same function signatures.

import os
import logging
from supabase import create_client, Client
from error_codes import Errors

_log = logging.getLogger("workwitness-mcp")

# ── Configuration ─────────────────────────────────────────────────────
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip()
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()

if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
    raise RuntimeError(
        "SUPABASE_URL and SUPABASE_SERVICE_KEY must be set. "
        "The server cannot start without database configuration."
    )

_supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


# ── Error class ───────────────────────────────────────────────────────

class SheetsError(Exception):
    """
    Kept as SheetsError for backwards compatibility with server.py imports.
    Raised when a database query fails.
    """
    def __init__(self, error, message: str | None = None):
        self.error = error
        self.message = message or error.message
        super().__init__(self.message)


# ── Data functions ────────────────────────────────────────────────────

def get_employee_by_code(employee_code: str) -> dict | None:
    """
    Returns employee dict or None if not found.
    Case-insensitive match on employee_code.
    """
    try:
        result = _supabase.table("employees") \
            .select("*") \
            .ilike("employee_code", employee_code) \
            .execute()
        if result.data and len(result.data) > 0:
            return result.data[0]
        return None
    except Exception as e:
        _log.error(f"Supabase employees query failed: {e}")
        raise SheetsError(Errors.SHEETS_UNAVAILABLE, message=str(e))

def get_employee_by_name(name: str) -> dict | None:
    """
    Searches for an employee by name (case-insensitive partial match).
    Returns the first match or None.
    """
    try:
        result = _supabase.table("employees") \
            .select("*") \
            .ilike("name", f"%{name}%") \
            .execute()
        if result.data and len(result.data) > 0:
            return result.data[0]
        return None
    except Exception as e:
        _log.error(f"Supabase name search failed: {e}")
        return None


def get_latest_activity_for_employee(employee_code: str) -> dict | None:
    """
    Returns the most recent activity record for the employee, or None.
    """
    try:
        result = _supabase.table("daily_activity") \
            .select("*") \
            .ilike("employee_code", employee_code) \
            .order("date", desc=True) \
            .limit(1) \
            .execute()
        if result.data and len(result.data) > 0:
            row = result.data[0]
            # Convert date to string format for compatibility
            if row.get("date"):
                row["date"] = str(row["date"])
            return row
        return None
    except Exception as e:
        _log.error(f"Supabase activity query failed: {e}")
        raise SheetsError(Errors.SHEETS_UNAVAILABLE, message=str(e))


def get_user_permissions(clerk_user_id: str) -> dict | None:
    """
    Returns permissions for a user from the user_permissions table.
    Returns None if user is not in the table (will use defaults).
    """
    try:
        result = _supabase.table("user_permissions") \
            .select("*") \
            .eq("clerk_user_id", clerk_user_id) \
            .execute()
        if result.data and len(result.data) > 0:
            return result.data[0]
        return None
    except Exception as e:
        _log.error(f"Supabase permissions query failed: {e}")
        return None

def get_activity_for_date(employee_code: str, date: str) -> dict | None:
    """
    Returns activity for a specific employee on a specific date.
    Returns None if no activity found for that date.
    """
    try:
        result = _supabase.table("daily_activity") \
            .select("*") \
            .ilike("employee_code", employee_code) \
            .eq("date", date) \
            .execute()
        if result.data and len(result.data) > 0:
            row = result.data[0]
            if row.get("date"):
                row["date"] = str(row["date"])
            return row
        return None
    except Exception as e:
        _log.error(f"Supabase activity query failed: {e}")
        raise SheetsError(Errors.SHEETS_UNAVAILABLE, message=str(e))


def get_top_performers(start_date: str, end_date: str) -> list:
    """
    Returns all activity rows within a date range for ranking.
    """
    try:
        result = _supabase.table("daily_activity") \
            .select("*, employees(name, department)") \
            .gte("date", start_date) \
            .lte("date", end_date) \
            .order("date", desc=True) \
            .execute()
        return result.data or []
    except Exception as e:
        _log.error(f"Supabase top performers query failed: {e}")
        raise SheetsError(Errors.SHEETS_UNAVAILABLE, message=str(e))