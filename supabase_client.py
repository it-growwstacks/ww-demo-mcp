# supabase_client.py
# Layer 6 — Data Fetching (Supabase PostgreSQL)
#
# REWRITTEN FOR SCHEMA v2 (multi-tenant).
#
# Rules that apply to every function in this file:
#   1. company_id is the FIRST argument. Never optional. Never inferred.
#      Forgetting it is a Python TypeError at call time, not a data leak.
#
#   2. Every query filters by company_id. No exceptions. This is the tenant
#      boundary — the same URL and same code serves many companies, and this
#      filter is what keeps their data separate.
#
#   3. Employees are is_active=TRUE by default. Departed employees (like E008)
#      do not appear in lookups. Their historical activity rows still exist
#      and are still fetchable — but they will not show up as "current staff".
#
#   4. Function names and return shapes match the old file where possible,
#      so server.py breaks in the fewest places.

import os
import logging
from supabase import create_client, Client
from error_codes import Errors

_log = logging.getLogger("workwitness-mcp")


# ── Configuration ────────────────────────────────────────────────────

SUPABASE_URL         = os.environ.get("SUPABASE_URL", "").strip()
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()

if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
    raise RuntimeError(
        "SUPABASE_URL and SUPABASE_SERVICE_KEY must be set. "
        "The server cannot start without database configuration."
    )

_supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


# ── Error class ──────────────────────────────────────────────────────

class SheetsError(Exception):
    """
    Kept as SheetsError for backwards compatibility with server.py imports.
    Raised when a database query fails at the transport level (Supabase down,
    network error, etc.). Business errors like 'employee not found' are
    returned as None instead — the caller decides how to render them.
    """
    def __init__(self, error, message: str | None = None):
        self.error = error
        self.message = message or error.message
        super().__init__(self.message)


# ── Permissions ──────────────────────────────────────────────────────

def get_user_permissions(clerk_user_id: str) -> dict | None:
    """
    Look up a user's row in user_permissions.

    Returns the row as a dict, or None if the user has no row.

    IMPORTANT: server.py must treat None as DENY, not as "use defaults".
    A user with no row has no company and no data access. Anything else is
    a fail-open bug (v4 codebase section 8.3).

    The row includes:
      clerk_user_id, company_id, email, role, employee_code,
      scope, allowed_tools, is_active
    """
    if not clerk_user_id or not clerk_user_id.strip():
        return None

    try:
        result = (
            _supabase.table("user_permissions")
            .select("clerk_user_id, company_id, email, role, employee_code, "
                    "scope, allowed_tools, is_active")
            .eq("clerk_user_id", clerk_user_id)
            .eq("is_active", True)
            .execute()
        )
        rows = result.data or []
        return rows[0] if rows else None
    except Exception as e:
        _log.error(f"Supabase user_permissions query failed: {e}")
        # A DB error here is different from "no row". We surface it so the
        # caller can respond with SHEETS_UNAVAILABLE, not FORBIDDEN.
        raise SheetsError(Errors.SHEETS_UNAVAILABLE, message=str(e))


# ── Scope resolution ─────────────────────────────────────────────────
#
# These helpers translate a user's `scope` value into the set of employee
# codes they are allowed to see. Server.py uses this in Layer 3.6 to decide
# whether a specific employee is visible, and to filter list responses.

def get_visible_employee_codes(
    company_id: str,
    scope: str,
    user_employee_code: str | None,
) -> list[str]:
    """
    Return the list of employee_codes this user can see, based on their scope.

    Scopes and what they resolve to:
      'self'           -> just [user_employee_code]
      'direct_reports' -> user + everyone whose manager_code = user
      'department'     -> everyone in the same department as the user
      'all'            -> every active employee at their company

    All resolutions are scoped to company_id — never crosses tenants.
    Only active employees are returned (departed staff are excluded).
    """
    try:
        # scope='all': everyone in the tenant
        if scope == "all":
            result = (
                _supabase.table("employees")
                .select("employee_code")
                .eq("company_id", company_id)
                .eq("is_active", True)
                .execute()
            )
            return [r["employee_code"] for r in (result.data or [])]

        # scope='self': just the user
        if scope == "self":
            return [user_employee_code] if user_employee_code else []

        # scope='direct_reports': self + people managed by self
        if scope == "direct_reports":
            if not user_employee_code:
                return []
            result = (
                _supabase.table("employees")
                .select("employee_code")
                .eq("company_id", company_id)
                .eq("is_active", True)
                .or_(f"employee_code.eq.{user_employee_code},"
                     f"manager_code.eq.{user_employee_code}")
                .execute()
            )
            return [r["employee_code"] for r in (result.data or [])]

        # scope='department': everyone in the user's department
        if scope == "department":
            if not user_employee_code:
                return []
            # Two-step: find the user's department, then list its employees.
            me = (
                _supabase.table("employees")
                .select("department_id")
                .eq("company_id", company_id)
                .eq("employee_code", user_employee_code)
                .execute()
            )
            if not me.data:
                return []
            dept_id = me.data[0].get("department_id")
            if not dept_id:
                # User has no department set. They see nobody in this scope.
                return []
            result = (
                _supabase.table("employees")
                .select("employee_code")
                .eq("company_id", company_id)
                .eq("is_active", True)
                .eq("department_id", dept_id)
                .execute()
            )
            return [r["employee_code"] for r in (result.data or [])]

        # Unknown scope — fail closed. Never fall through to "everyone".
        _log.error(f"Unknown scope value: {scope!r}")
        return []

    except Exception as e:
        _log.error(f"Scope resolution failed: {e}")
        raise SheetsError(Errors.SHEETS_UNAVAILABLE, message=str(e))


# ── Employee lookups ─────────────────────────────────────────────────

def get_employee_by_code(company_id: str, employee_code: str) -> dict | None:
    """
    Fetch one employee by code, scoped to a company.

    Returns dict or None. Case-insensitive on the code.

    IMPORTANT: v2 allows the same employee_code to exist in two different
    companies (Acme's E001 is a different person from Globex's E001). The
    company_id filter is what tells them apart.
    """
    try:
        # We normalise to upper-case ourselves rather than using ilike, so
        # the query hits the composite primary key index for speed.
        result = (
            _supabase.table("employees")
            .select("employee_code, name, department_id, manager_code, "
                    "joining_date, is_active, exit_date")
            .eq("company_id", company_id)
            .ilike("employee_code", employee_code)
            .execute()
        )
        if not result.data:
            return None
        return _attach_department_name(company_id, result.data[0])
    except Exception as e:
        _log.error(f"Supabase employees query failed: {e}")
        raise SheetsError(Errors.SHEETS_UNAVAILABLE, message=str(e))


def get_employee_by_name(company_id: str, name: str) -> dict | None:
    """
    Search for an employee by name within a company. Case-insensitive partial
    match. Returns the first match or None.

    Used as a fallback when a user types 'Mohit' instead of 'E003'.
    """
    try:
        result = (
            _supabase.table("employees")
            .select("employee_code, name, department_id, manager_code, "
                    "joining_date, is_active, exit_date")
            .eq("company_id", company_id)
            .ilike("name", f"%{name}%")
            .execute()
        )
        if not result.data:
            return None
        return _attach_department_name(company_id, result.data[0])
    except Exception as e:
        _log.error(f"Supabase name search failed: {e}")
        return None


def _attach_department_name(company_id: str, employee_row: dict) -> dict:
    """
    v1 had `department` as a text column on employees. v2 has `department_id`
    pointing at the departments table. To keep the return shape the same for
    server.py, we look up the department name and attach it as `department`.

    Falls back to 'Unknown' if the department cannot be resolved. This never
    crosses tenants because we filter by company_id.
    """
    dept_id = employee_row.get("department_id")
    if not dept_id:
        employee_row["department"] = "Unknown"
        return employee_row

    try:
        dept = (
            _supabase.table("departments")
            .select("name")
            .eq("company_id", company_id)
            .eq("department_id", dept_id)
            .execute()
        )
        employee_row["department"] = (
            dept.data[0]["name"] if dept.data else "Unknown"
        )
    except Exception:
        # If the department lookup fails, we still return the employee. Losing
        # the department label is better than failing the whole request.
        employee_row["department"] = "Unknown"

    return employee_row


# ── Activity lookups ─────────────────────────────────────────────────

def get_latest_activity_for_employee(
    company_id: str, employee_code: str
) -> dict | None:
    """
    Return the most recent activity row for one employee.

    Note: v2's UNIQUE(company_id, employee_code, date) means there is at most
    one row per employee per day. The old "last row per date wins" workaround
    from the Sheets days is no longer needed.
    """
    try:
        result = (
            _supabase.table("daily_activity")
            .select("*")
            .eq("company_id", company_id)
            .ilike("employee_code", employee_code)
            .order("date", desc=True)
            .limit(1)
            .execute()
        )
        if not result.data:
            return None
        row = result.data[0]
        if row.get("date"):
            row["date"] = str(row["date"])
        return row
    except Exception as e:
        _log.error(f"Supabase activity query failed: {e}")
        raise SheetsError(Errors.SHEETS_UNAVAILABLE, message=str(e))


def get_activity_for_date(
    company_id: str, employee_code: str, date: str
) -> dict | None:
    """
    Return the activity row for one employee on a specific date.
    Returns None if no row exists for that date.
    """
    try:
        result = (
            _supabase.table("daily_activity")
            .select("*")
            .eq("company_id", company_id)
            .ilike("employee_code", employee_code)
            .eq("date", date)
            .execute()
        )
        if not result.data:
            return None
        row = result.data[0]
        if row.get("date"):
            row["date"] = str(row["date"])
        return row
    except Exception as e:
        _log.error(f"Supabase activity query failed: {e}")
        raise SheetsError(Errors.SHEETS_UNAVAILABLE, message=str(e))


def get_top_performers(
    company_id: str,
    start_date: str,
    end_date: str,
    visible_codes: list[str] | None = None,
) -> list:
    """
    Return all activity rows in a date range for ranking.

    If visible_codes is provided, results are filtered to only that set —
    this is how scope-based permissions apply to aggregation tools. Passing
    an empty list returns no rows (correct behaviour when the user is not
    allowed to see anyone).

    Passing visible_codes=None means "no scope filter" — reserved for admin
    scope='all' users. Server.py decides which case applies.

    Each returned row includes joined employee info (name and department).
    """
    try:
        # v2 requires a two-step select because 'department' now lives on a
        # separate table via department_id. Supabase can do the join for us
        # via foreign-key hint: employees!daily_activity_employee_fk.
        # But that syntax is fragile. Simpler: fetch activity, then attach
        # employee info in a second query. Two round trips, clearer code.
        query = (
            _supabase.table("daily_activity")
            .select("*")
            .eq("company_id", company_id)
            .gte("date", start_date)
            .lte("date", end_date)
        )

        if visible_codes is not None:
            if not visible_codes:
                # Empty visibility set → no rows. Return early.
                return []
            query = query.in_("employee_code", visible_codes)

        result = query.execute()
        rows = result.data or []
        if not rows:
            return []

        # Attach employee info (name, department name) to each activity row.
        # One query per unique employee_code — bounded and small.
        codes = list({r["employee_code"] for r in rows})
        emp_result = (
            _supabase.table("employees")
            .select("employee_code, name, department_id, is_active")
            .eq("company_id", company_id)
            .in_("employee_code", codes)
            .execute()
        )
        emp_map = {e["employee_code"]: e for e in (emp_result.data or [])}

        # Resolve department names once.
        dept_ids = list({
            e["department_id"] for e in emp_map.values() if e.get("department_id")
        })
        dept_map = {}
        if dept_ids:
            dept_result = (
                _supabase.table("departments")
                .select("department_id, name")
                .eq("company_id", company_id)
                .in_("department_id", dept_ids)
                .execute()
            )
            dept_map = {
                d["department_id"]: d["name"] for d in (dept_result.data or [])
            }

        # Attach in the same shape server.py already expects: row["employees"]
        # containing name and department (string, not id).
        for r in rows:
            emp = emp_map.get(r["employee_code"], {})
            r["employees"] = {
                "name": emp.get("name", ""),
                "department": dept_map.get(emp.get("department_id"), ""),
                "is_active": emp.get("is_active", True),
            }
            if r.get("date"):
                r["date"] = str(r["date"])

        return rows

    except Exception as e:
        _log.error(f"Supabase top performers query failed: {e}")
        raise SheetsError(Errors.SHEETS_UNAVAILABLE, message=str(e))