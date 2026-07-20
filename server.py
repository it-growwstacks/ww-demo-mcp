# server.py
# WW-Demo MCP Server
# Production-grade — all 8 layers active (schema v2 multi-tenant)
#
# CHANGES vs previous version:
#   - Fail-closed permissions (no row -> FORBIDDEN, never defaults)
#   - Real company_id from user_permissions row (never from JWT sub)
#   - scope-based visibility (self / direct_reports / department / all)
#   - Every data-layer call is tenant-scoped with company_id
#   - Rate limiting keyed on Clerk sub, not company (per-user, not per-tenant)
#   - Removed DEBUG backdoor (was leaking token claims)
#   - Fixed missing tool_name / start_time in get_top_performers

import os
import sys
import time
from datetime import datetime, timezone, datetime as dt
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP, Context

from auth import verify_token, extract_bearer_token, AuthError
from rate_limiter import check_rate_limit, RateLimitError
from validators import validate_input, ValidationError, EmployeeStatusInput, DailyBriefInput, TopPerformersInput
from supabase_client import (
    get_employee_by_code,
    get_employee_by_name,
    get_latest_activity_for_employee,
    get_user_permissions,
    get_activity_for_date,
    get_top_performers as fetch_top_performers,
    get_visible_employee_codes,
    SheetsError,
)
from audit_logger import log_tool_call
from error_codes import Errors

load_dotenv()

mcp = FastMCP("workwitness-sheets-mcp", host="0.0.0.0", port=8000)


# ── Expanded no-blocker values
NO_BLOCKER_VALUES = {
    "none", "n/a", "nil", "no", "no blockers",
    "no blocker", "-", "—", "", "null", "na", "false", "0"
}


# ── Helper functions ────────────────────────────────────────────────

def _parse_int(value, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (ValueError, TypeError):
        return default


def _parse_float(value, default: float = 0.0) -> float:
    try:
        return float(str(value).strip())
    except (ValueError, TypeError):
        return default


def _parse_focus_score(value):
    if value is None:
        return None
    raw = str(value).strip()
    if raw == "" or not raw.lstrip("-").isdigit():
        return None
    return int(raw)


def _parse_hours(value):
    try:
        hours = float(str(value).strip())
        if hours > 24:
            return None
        return hours
    except (ValueError, TypeError):
        return 0.0


def _compute_performance_signal(focus_score, tasks_completed: int, blockers: str) -> str:
    has_blocker = (
        blockers and
        blockers.strip().lower() not in NO_BLOCKER_VALUES
    )
    if has_blocker:
        return "blocked"
    if focus_score is None:
        return "data_missing"
    if focus_score >= 80 and tasks_completed >= 4:
        return "strong"
    if focus_score >= 60:
        return "steady"
    return "needs_attention"


def _build_error_response(error, message: str | None = None) -> dict:
    return {
        "error": True,
        "code": error.code,
        "message": message or error.message,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def _safe_log(api_key: str, tool: str, inputs: dict, outcome: str, duration_ms: int):
    try:
        log_tool_call(
            api_key=api_key,
            tool=tool,
            inputs=inputs,
            outcome=outcome,
            duration_ms=duration_ms,
        )
    except Exception as log_error:
        print(f"AUDIT LOG FAILURE: {log_error}", file=sys.stderr)


# ── Permission helpers ──────────────────────────────────────────────

def _get_permissions(claims: dict) -> dict:
    """
    Look up user permissions from Supabase.
    FAIL CLOSED. No row = FORBIDDEN. Never grant default access.
    """
    user_sub = claims.get("sub", "")
    perms = get_user_permissions(user_sub)
    if perms is None:
        raise AuthError(
            Errors.FORBIDDEN,
            message="Your account is not authorised to use this service. "
                    "Contact your administrator."
        )
    return perms


def _check_tool_permission(user_permissions: dict, tool_name: str) -> None:
    allowed_tools = user_permissions.get("allowed_tools") or []
    if tool_name not in allowed_tools:
        raise AuthError(
            Errors.FORBIDDEN,
            message=f"Your account does not have permission to use '{tool_name}'. "
                    f"Contact your administrator for access."
        )


def _check_employee_permission(user_permissions: dict, employee_code: str) -> None:
    """
    Resolve scope into the actual set of visible employee codes,
    then check membership. Returns the same error for invisible-and-existent
    as for non-existent — never leaks which case it is.
    """
    company_id = user_permissions["company_id"]
    scope = user_permissions.get("scope") or "self"
    user_emp = user_permissions.get("employee_code")

    visible = get_visible_employee_codes(company_id, scope, user_emp)
    if employee_code.upper() not in [c.upper() for c in visible]:
        raise AuthError(
            Errors.FORBIDDEN,
            message=f"You do not have permission to view employee '{employee_code}'."
        )


# ── Tool 1 — get_employee_status ────────────────────────────────────

@mcp.tool()
def get_employee_status(
    employee_code: str,
    ctx: Context
) -> dict:
    """
    Returns the current activity status for one employee.

    Use this tool when someone asks about a specific employee —
    their current productivity stage, focus score, hours worked,
    tasks completed, any blockers, and overall performance signal.

    Requires:
    - employee_code : the employee's code (e.g. E001) or name (e.g. Mohit)

    Authentication is handled automatically via the connection's
    Bearer token — never ask the user for an API key.

    If the employee does not exist, returns EMPLOYEE_NOT_FOUND.
    If the employee exists but has no activity data, returns their
    profile with a status of no_activity_data.

    Never mention the tool name or internal function names to the user.
    Present the data naturally as if you already know it.
    """
    start_time = time.time()
    tool_name = "get_employee_status"
    company_id = "unauthenticated"

    try:
        # LAYER 2 — AUTHENTICATION
        request = ctx.request_context.request
        request_headers = dict(request.headers) if request is not None else {}
        api_key = extract_bearer_token(request_headers)
        claims = verify_token(api_key)

        # LAYER 3 — IDENTITY EXTRACTION (sub only; real company comes next)
        user_sub = claims.get("sub")
        if not user_sub:
            raise AuthError(
                Errors.UNAUTHENTICATED,
                message="Token is missing required identity claims."
            )

        # LAYER 3.5 — PERMISSION LOOKUP (fail-closed, real company_id)
        user_permissions = _get_permissions(claims)
        company_id = user_permissions["company_id"]
        _check_tool_permission(user_permissions, tool_name)

        # LAYER 3.6 — EMPLOYEE DATA SCOPING
        _check_employee_permission(user_permissions, employee_code)

        # LAYER 4 — RATE LIMITING (per-user, not per-tenant)
        check_rate_limit(user_sub)

        # LAYER 5 — INPUT VALIDATION
        validated = validate_input(
            EmployeeStatusInput,
            {"employee_code": employee_code}
        )

        # LAYER 6 — DATA FETCH (tenant-scoped)
        employee = get_employee_by_code(company_id, validated.employee_code)
        if not employee:
            employee = get_employee_by_name(company_id, employee_code)

        if not employee:
            duration = int((time.time() - start_time) * 1000)
            _safe_log(
                api_key=user_sub,
                tool=tool_name,
                inputs={"employee_code": employee_code},
                outcome=Errors.EMPLOYEE_NOT_FOUND.code,
                duration_ms=duration,
            )
            return _build_error_response(
                Errors.EMPLOYEE_NOT_FOUND,
                message=f"No employee found with code '{validated.employee_code}'. "
                        f"Please check the employee code and try again."
            )

        activity = get_latest_activity_for_employee(company_id, validated.employee_code)

        # LAYER 7 — AUDIT LOGGING
        duration = int((time.time() - start_time) * 1000)
        _safe_log(
            api_key=user_sub,
            tool=tool_name,
            inputs={"employee_code": employee_code},
            outcome="success",
            duration_ms=duration,
        )

        # LAYER 8 — RESPONSE SHAPING
        base = {
            "employee_code": employee.get("employee_code", "").upper(),
            "name": employee.get("name", "Unknown"),
            "department": employee.get("department", "Unknown"),
            "joining_date": employee.get("joining_date", "Unknown"),
        }

        if not activity:
            return {
                **base,
                "status": "no_activity_data",
                "message": (
                    f"{base['name']} is registered in the system "
                    f"but has no recorded activity data yet."
                ),
            }

        focus_score = _parse_focus_score(activity.get("focus_score"))
        hours_worked = _parse_hours(activity.get("hours_worked"))
        tasks_completed = _parse_int(activity.get("tasks_completed"), default=0)

        raw_blockers = (activity.get("blockers") or "").strip()
        blockers = (
            "None"
            if raw_blockers.lower() in NO_BLOCKER_VALUES
            else raw_blockers
        )

        data_warning = None
        last_date_str = activity.get("date", "Unknown")
        try:
            last_date = dt.strptime(last_date_str, "%Y-%m-%d")
            days_old = (dt.now() - last_date).days
            if days_old > 7:
                data_warning = (
                    f"This data is {days_old} days old. "
                    f"Activity tracking may not be current."
                )
        except (ValueError, TypeError):
            pass

        response = {
            **base,
            "status": "active",
            "current_stage": activity.get("stage") or "Not recorded",
            "focus_score": focus_score,
            "hours_worked": hours_worked,
            "tasks_completed": tasks_completed,
            "blockers": blockers,
            "last_active_date": last_date_str,
            "performance_signal": _compute_performance_signal(
                focus_score=focus_score,
                tasks_completed=tasks_completed,
                blockers=blockers,
            ),
        }

        if data_warning:
            response["data_warning"] = data_warning

        if hours_worked is None:
            response["data_warning"] = (
                "Hours worked value appears incorrect — please check the data."
            )

        return response

    except AuthError as e:
        duration = int((time.time() - start_time) * 1000)
        _safe_log(api_key="unauthenticated", tool=tool_name, inputs={}, outcome=e.error.code, duration_ms=duration)
        return _build_error_response(e.error, message=e.message)

    except RateLimitError as e:
        duration = int((time.time() - start_time) * 1000)
        _safe_log(api_key=company_id, tool=tool_name, inputs={"employee_code": employee_code}, outcome=e.error.code, duration_ms=duration)
        response = _build_error_response(e.error)
        response["retry_after_seconds"] = e.retry_after
        return response

    except ValidationError as e:
        duration = int((time.time() - start_time) * 1000)
        _safe_log(api_key=company_id, tool=tool_name, inputs={"employee_code": employee_code}, outcome=e.error.code, duration_ms=duration)
        return _build_error_response(e.error, message=e.message)

    except SheetsError as e:
        duration = int((time.time() - start_time) * 1000)
        _safe_log(api_key=company_id, tool=tool_name, inputs={"employee_code": employee_code}, outcome=e.error.code, duration_ms=duration)
        return _build_error_response(e.error, message=e.message)

    except Exception:
        duration = int((time.time() - start_time) * 1000)
        _safe_log(api_key=company_id, tool=tool_name, inputs={"employee_code": employee_code}, outcome=Errors.INTERNAL_ERROR.code, duration_ms=duration)
        return _build_error_response(Errors.INTERNAL_ERROR)


# ── Tool 2 — get_daily_brief ────────────────────────────────────────

@mcp.tool()
def get_daily_brief(
    employee_code: str,
    date: str,
    ctx: Context
) -> dict:
    """
    Returns the detailed daily activity brief for one employee on a specific date.

    Use this tool when someone asks about what an employee did on a particular
    day, their activity on a specific date, or their daily summary.

    Requires:
    - employee_code : the employee's code (e.g. E001) or name (e.g. Mohit)
    - date : the date in YYYY-MM-DD format, e.g. 2026-07-01

    If no activity is found for that date, returns a clear message.

    Never mention the tool name or internal function names to the user.
    """
    start_time = time.time()
    tool_name = "get_daily_brief"
    company_id = "unauthenticated"

    try:
        # LAYER 2 — AUTHENTICATION
        request = ctx.request_context.request
        request_headers = dict(request.headers) if request is not None else {}
        api_key = extract_bearer_token(request_headers)
        claims = verify_token(api_key)

        # LAYER 3 — IDENTITY EXTRACTION
        user_sub = claims.get("sub")
        if not user_sub:
            raise AuthError(Errors.UNAUTHENTICATED, message="Token is missing required identity claims.")

        # LAYER 3.5 — PERMISSION LOOKUP
        user_permissions = _get_permissions(claims)
        company_id = user_permissions["company_id"]
        _check_tool_permission(user_permissions, tool_name)

        # LAYER 3.6 — EMPLOYEE DATA SCOPING
        _check_employee_permission(user_permissions, employee_code)

        # LAYER 4 — RATE LIMITING
        check_rate_limit(user_sub)

        # LAYER 5 — INPUT VALIDATION
        validated = validate_input(DailyBriefInput, {"employee_code": employee_code, "date": date})

        # LAYER 6 — DATA FETCH (tenant-scoped)
        employee = get_employee_by_code(company_id, validated.employee_code)
        if not employee:
            employee = get_employee_by_name(company_id, employee_code)
            if employee:
                validated.employee_code = employee.get("employee_code", validated.employee_code)

        if not employee:
            duration = int((time.time() - start_time) * 1000)
            _safe_log(api_key=user_sub, tool=tool_name, inputs={"employee_code": employee_code}, outcome="EMPLOYEE_NOT_FOUND", duration_ms=duration)
            return _build_error_response(Errors.EMPLOYEE_NOT_FOUND, message=f"No employee found with code '{validated.employee_code}'.")

        activity = get_activity_for_date(company_id, validated.employee_code, validated.date)

        # LAYER 7 — AUDIT LOGGING
        duration = int((time.time() - start_time) * 1000)
        _safe_log(api_key=user_sub, tool=tool_name, inputs={"employee_code": employee_code, "date": date}, outcome="success", duration_ms=duration)

        # LAYER 8 — RESPONSE SHAPING
        if not activity:
            return {
                "employee_code": employee.get("employee_code", "").upper(),
                "name": employee.get("name", "Unknown"),
                "date": validated.date,
                "status": "no_activity_for_date",
                "message": f"No activity recorded for {employee.get('name', 'this employee')} on {validated.date}.",
            }

        focus_score = _parse_focus_score(activity.get("focus_score"))
        hours_worked = _parse_hours(activity.get("hours_worked"))
        tasks_completed = _parse_int(activity.get("tasks_completed"), default=0)
        raw_blockers = (activity.get("blockers") or "").strip()
        blockers = "None" if raw_blockers.lower() in NO_BLOCKER_VALUES else raw_blockers

        return {
            "employee_code": employee.get("employee_code", "").upper(),
            "name": employee.get("name", "Unknown"),
            "department": employee.get("department", "Unknown"),
            "date": validated.date,
            "stage": activity.get("stage") or "Not recorded",
            "focus_score": focus_score,
            "hours_worked": hours_worked,
            "tasks_completed": tasks_completed,
            "blockers": blockers,
            "daily_brief": activity.get("daily_brief") or "No brief recorded.",
            "performance_signal": _compute_performance_signal(focus_score=focus_score, tasks_completed=tasks_completed, blockers=blockers),
        }

    except AuthError as e:
        duration = int((time.time() - start_time) * 1000)
        _safe_log(api_key="unauthenticated", tool=tool_name, inputs={}, outcome=e.error.code, duration_ms=duration)
        return _build_error_response(e.error, message=e.message)

    except RateLimitError as e:
        duration = int((time.time() - start_time) * 1000)
        _safe_log(api_key=company_id, tool=tool_name, inputs={"employee_code": employee_code}, outcome=e.error.code, duration_ms=duration)
        response = _build_error_response(e.error)
        response["retry_after_seconds"] = e.retry_after
        return response

    except ValidationError as e:
        duration = int((time.time() - start_time) * 1000)
        _safe_log(api_key=company_id, tool=tool_name, inputs={"employee_code": employee_code}, outcome=e.error.code, duration_ms=duration)
        return _build_error_response(e.error, message=e.message)

    except SheetsError as e:
        duration = int((time.time() - start_time) * 1000)
        _safe_log(api_key=company_id, tool=tool_name, inputs={"employee_code": employee_code}, outcome=e.error.code, duration_ms=duration)
        return _build_error_response(e.error, message=e.message)

    except Exception:
        duration = int((time.time() - start_time) * 1000)
        _safe_log(api_key=company_id, tool=tool_name, inputs={"employee_code": employee_code}, outcome=Errors.INTERNAL_ERROR.code, duration_ms=duration)
        return _build_error_response(Errors.INTERNAL_ERROR)



# ── Tool 3 — get_top_performers ─────────────────────────────────────

@mcp.tool()
def get_top_performers(
    period: str,
    ctx: Context
) -> dict:
    """
    Returns employees ranked by performance for a given period.

    Use this when someone asks who performed best, who are the top performers,
    or who had the highest scores.

    Requires:
    - period : one of "daily", "weekly", "monthly", or a specific date like "2026-07-16"

    Returns a ranked list sorted by performance_score = avg_focus*0.6 + avg_tasks*0.4.
    Permission-aware: viewers only see rankings for employees they have access to.

    Never mention tool names or internal function names to the user.
    """
    start_time = time.time()
    tool_name = "get_top_performers"
    company_id = "unauthenticated"

    try:
        # LAYER 2 — AUTHENTICATION
        request = ctx.request_context.request
        request_headers = dict(request.headers) if request is not None else {}
        api_key = extract_bearer_token(request_headers)
        claims = verify_token(api_key)

        # LAYER 3 — IDENTITY EXTRACTION
        user_sub = claims.get("sub")
        if not user_sub:
            raise AuthError(Errors.UNAUTHENTICATED, message="Token is missing required identity claims.")

        # LAYER 3.5 — PERMISSION LOOKUP
        user_permissions = _get_permissions(claims)
        company_id = user_permissions["company_id"]
        _check_tool_permission(user_permissions, tool_name)

        # LAYER 3.6 — VISIBILITY FOR AGGREGATION
        # scope=all -> no filter (all tenant employees). Any other scope -> restrict.
        scope = user_permissions.get("scope") or "self"
        user_emp = user_permissions.get("employee_code")

        if scope == "all":
            visible_codes = None
        else:
            visible_codes = get_visible_employee_codes(company_id, scope, user_emp)

        # LAYER 4 — RATE LIMITING
        check_rate_limit(user_sub)

        # LAYER 5 — INPUT VALIDATION
        validated = validate_input(TopPerformersInput, {"period": period})

        # ── Date range calculation (before Layer 6 fetch)
        from datetime import timedelta
        today = datetime.now(timezone.utc).date()
        period_label = validated.period.lower().strip()

        if period_label == "daily":
            start_date = today
            end_date = today
        elif period_label == "weekly":
            start_date = today - timedelta(days=7)
            end_date = today
        elif period_label == "monthly":
            start_date = today - timedelta(days=30)
            end_date = today
        else:
            try:
                specific_date = datetime.strptime(period_label, "%Y-%m-%d").date()
                start_date = specific_date
                end_date = specific_date
            except ValueError:
                return _build_error_response(
                    Errors.INVALID_PARAMETER,
                    message=f"Invalid period '{validated.period}'. Use 'daily', 'weekly', 'monthly', or a date like '2026-07-16'."
                )

        # LAYER 6 — DATA FETCH (tenant-scoped + visibility-scoped)
        rows = fetch_top_performers(
            company_id,
            str(start_date),
            str(end_date),
            visible_codes=visible_codes,
        )

        if not rows:
            duration = int((time.time() - start_time) * 1000)
            _safe_log(api_key=user_sub, tool=tool_name, inputs={"period": period}, outcome="no_data", duration_ms=duration)
            return {
                "period": validated.period,
                "start_date": str(start_date),
                "end_date": str(end_date),
                "message": f"No activity data found for the {validated.period} period.",
                "rankings": [],
            }

        # LAYER 7 — AUDIT LOGGING
        duration = int((time.time() - start_time) * 1000)
        _safe_log(api_key=user_sub, tool=tool_name, inputs={"period": period}, outcome="success", duration_ms=duration)

        # LAYER 8 — RESPONSE SHAPING (aggregate + rank)
        from collections import defaultdict
        employee_stats = defaultdict(lambda: {
            "total_focus": 0, "total_tasks": 0, "total_hours": 0,
            "days_active": 0, "name": "", "department": "",
        })

        for row in rows:
            code = row.get("employee_code", "").upper()
            stats = employee_stats[code]
            stats["total_focus"] += (row.get("focus_score") or 0)
            stats["total_tasks"] += (row.get("tasks_completed") or 0)
            stats["total_hours"] += float(row.get("hours_worked") or 0)
            stats["days_active"] += 1
            emp_info = row.get("employees") or {}
            if emp_info:
                stats["name"] = emp_info.get("name", stats["name"])
                stats["department"] = emp_info.get("department", stats["department"])

        rankings = []
        for code, stats in employee_stats.items():
            days = stats["days_active"]
            avg_focus = round(stats["total_focus"] / days, 1) if days > 0 else 0
            avg_tasks = round(stats["total_tasks"] / days, 1) if days > 0 else 0
            avg_hours = round(stats["total_hours"] / days, 1) if days > 0 else 0
            performance_score = round(avg_focus * 0.6 + avg_tasks * 0.4, 1)
            rankings.append({
                "employee_code": code,
                "name": stats["name"],
                "department": stats["department"],
                "days_active": days,
                "avg_focus_score": avg_focus,
                "avg_tasks_per_day": avg_tasks,
                "avg_hours_per_day": avg_hours,
                "total_tasks": stats["total_tasks"],
                "performance_score": performance_score,
            })

        # Sort by score desc; break ties deterministically by employee_code
        # so E005 and E006 always come back in the same order.
        rankings.sort(key=lambda x: (-x["performance_score"], x["employee_code"]))
        for i, r in enumerate(rankings):
            r["rank"] = i + 1

        return {
            "period": validated.period,
            "start_date": str(start_date),
            "end_date": str(end_date),
            "total_employees_ranked": len(rankings),
            "rankings": rankings,
        }

    except AuthError as e:
        duration = int((time.time() - start_time) * 1000)
        _safe_log(api_key="unauthenticated", tool=tool_name, inputs={}, outcome=e.error.code, duration_ms=duration)
        return _build_error_response(e.error, message=e.message)

    except RateLimitError as e:
        duration = int((time.time() - start_time) * 1000)
        _safe_log(api_key=company_id, tool=tool_name, inputs={"period": period}, outcome=e.error.code, duration_ms=duration)
        response = _build_error_response(e.error)
        response["retry_after_seconds"] = e.retry_after
        return response

    except ValidationError as e:
        duration = int((time.time() - start_time) * 1000)
        _safe_log(api_key=company_id, tool=tool_name, inputs={"period": period}, outcome=e.error.code, duration_ms=duration)
        return _build_error_response(e.error, message=e.message)

    except SheetsError as e:
        duration = int((time.time() - start_time) * 1000)
        _safe_log(api_key=company_id, tool=tool_name, inputs={"period": period}, outcome=e.error.code, duration_ms=duration)
        return _build_error_response(e.error, message=e.message)

    except Exception:
        duration = int((time.time() - start_time) * 1000)
        _safe_log(api_key=company_id, tool=tool_name, inputs={"period": period}, outcome=Errors.INTERNAL_ERROR.code, duration_ms=duration)
        return _build_error_response(Errors.INTERNAL_ERROR)


# ── OAuth endpoints ─────────────────────────────────────────────────

@mcp.custom_route("/.well-known/oauth-authorization-server", methods=["GET"])
async def discovery(request):
    from starlette.responses import JSONResponse
    clerk_issuer = os.environ.get("CLERK_ISSUER", "")
    mcp_base = os.environ.get("MCP_BASE_URL", "http://localhost:8000")
    return JSONResponse({
        "issuer": clerk_issuer,
        "authorization_endpoint": f"{clerk_issuer}/oauth/authorize",
        "token_endpoint": f"{clerk_issuer}/oauth/token",
        "registration_endpoint": f"{mcp_base}/oauth/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["client_secret_basic", "none"],
    })


@mcp.custom_route("/oauth/register", methods=["POST"])
async def oauth_register(request):
    from starlette.responses import JSONResponse
    client_id = os.environ.get("CLERK_OAUTH_CLIENT_ID", "")
    client_secret = os.environ.get("CLERK_OAUTH_CLIENT_SECRET", "")
    return JSONResponse({
        "client_id": client_id,
        "client_secret": client_secret,
        "client_id_issued_at": 0,
        "client_secret_expires_at": 0,
    })


if __name__ == "__main__":
    mcp.run(transport="streamable-http")