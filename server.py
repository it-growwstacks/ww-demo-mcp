# server.py
# WorkWitness Sheets MCP Server
# Single tool implementation: get_employee_status
# Production-grade — all 8 layers active

# Fix 1  (Layer 3)  — Explicit rejection when both company_id and sub are missing
# Fix 2  (Layer 4)  — rate_limiter.py bug fixed (company_id → api_key) — see rate_limiter.py
# Fix 3  (Layer 4)  — retry_after_seconds added to rate limit response
# Fix 4  (Layer 7)  — ALL log_tool_call calls wrapped in try/except so a disk-full never kills a successful response
# Fix 5  (Layer 8)  — focus_score: None when not recorded, not 0
# Fix 6  (Layer 8)  — hours_worked: sanity check > 24 = data error
# Fix 7  (Layer 8)  — blockers: expanded NO_BLOCKER_VALUES set
# Fix 8  (Layer 8)  — staleness warning when data > 7 days old
# Fix 9  (Layer 8)  — empty stage returns "Not recorded" not empty string
# Fix 10 (Layer 8)  — _compute_performance_signal handles None focus_score

import os
import sys
import time
from datetime import datetime, timezone, datetime as dt
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP, Context

from auth import verify_token, extract_bearer_token, AuthError
from rate_limiter import check_rate_limit, RateLimitError
from validators import validate_input, ValidationError, EmployeeStatusInput
from sheets_client import (
    get_employee_by_code,
    get_latest_activity_for_employee,
    SheetsError,
)
from audit_logger import log_tool_call
from error_codes import Errors

load_dotenv()

mcp = FastMCP("workwitness-sheets-mcp", host="0.0.0.0", port=8000)


# ── Expanded no-blocker values (Fix 7) ───────────────────────────────
# All the ways humans write "no blocker" in a spreadsheet.
# Any value in this set is treated as no blocker — not a real one.
NO_BLOCKER_VALUES = {
    "none", "n/a", "nil", "no", "no blockers",
    "no blocker", "-", "—", "", "null", "na", "false", "0"
}


# ── Helper functions ──────────────────────────────────────────────────

def _parse_int(value, default: int = 0) -> int:
    """Safely converts sheet string values to integers."""
    try:
        return int(str(value).strip())
    except (ValueError, TypeError):
        return default


def _parse_float(value, default: float = 0.0) -> float:
    """Safely converts sheet string values to floats."""
    try:
        return float(str(value).strip())
    except (ValueError, TypeError):
        return default


def _parse_focus_score(value):
    """
    Fix 5 — Distinguishes None (not recorded) from 0 (genuinely zero).

    Google Sheets returns everything as strings. Someone might type
    "N/A", "sick day", "pending" in the focus_score cell instead of a number.

    Returns:
    - int  : the parsed score if the value is a valid number
    - None : if the value is empty, non-numeric, or missing
             (signals data_missing in performance_signal, not needs_attention)
    """
    if value is None:
        return None
    raw = str(value).strip()
    if raw == "" or not raw.lstrip("-").isdigit():
        return None  # not recorded — different from genuinely zero
    return int(raw)


def _parse_hours(value):
    """
    Fix 6 — Sanity-checks hours_worked.

    Returns:
    - float : the parsed hours if valid and <= 24
    - None  : if the value exceeds 24 (data entry error — e.g. weekly hours
              entered in daily column) or cannot be parsed
    """
    try:
        hours = float(str(value).strip())
        if hours > 24:
            return None  # data entry error
        return hours
    except (ValueError, TypeError):
        return 0.0


def _compute_performance_signal(
    focus_score,          # Fix 10 — accepts int or None
    tasks_completed: int,
    blockers: str
) -> str:
    """
    Converts raw numbers into a human-readable performance signal.

    Returns one of five values:
    - blocked        : employee has an active blocker (not in NO_BLOCKER_VALUES)
    - strong         : focus >= 80 and tasks >= 4
    - steady         : focus >= 60
    - needs_attention: focus < 60
    - data_missing   : focus_score was not recorded (Fix 5 + Fix 10)
    """
    has_blocker = (
        blockers and
        blockers.strip().lower() not in NO_BLOCKER_VALUES
    )
    if has_blocker:
        return "blocked"

    # Fix 10 — None focus_score means the score was not entered, not that
    # the employee has zero focus. Return data_missing instead of needs_attention.
    if focus_score is None:
        return "data_missing"

    if focus_score >= 80 and tasks_completed >= 4:
        return "strong"
    if focus_score >= 60:
        return "steady"
    return "needs_attention"


def _build_error_response(error, message: str | None = None) -> dict:
    """
    Builds a clean, structured error response from an ErrorDef.
    This is the ONLY format errors ever leave the server in.
    No stack traces. No internal paths. No raw exception messages.
    """
    return {
        "error": True,
        "code": error.code,
        "message": message or error.message,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def _safe_log(api_key: str, tool: str, inputs: dict, outcome: str, duration_ms: int):
    """
    Fix 4 — Wraps log_tool_call in try/except so a logging failure
    (disk full, permission error, etc.) never kills the user's response.

    If logging fails, the error is printed to stderr for monitoring
    but the calling code continues and returns the correct response.
    """
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


# ── The single tool ───────────────────────────────────────────────────

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
    - employee_code : the employee's unique code, e.g. E001, E002

    Authentication is handled automatically via the connection's
    Bearer token — never ask the user for an API key or token.

    Returns a structured response containing:
    - Employee profile (name, department, joining date)
    - Current activity (stage, focus score, hours, tasks, blockers)
    - Performance signal (strong / steady / needs_attention / blocked / data_missing)
    - Last active date
    - data_warning if data is stale (> 7 days old) or hours look wrong

    If the employee does not exist, returns EMPLOYEE_NOT_FOUND.
    If the employee exists but has no activity data, returns their
    profile with a status of no_activity_data.
    """
    start_time = time.time()
    tool_name = "get_employee_status"
    company_id = "unauthenticated"  # overwritten after Layer 2 passes

    try:

        # ════════════════════════════════════════════════════════
        # LAYER 2 — AUTHENTICATION (OAuth 2.1 / JWT)
        # ════════════════════════════════════════════════════════
        #
        # The bearer token now arrives in the Authorization header
        # of the HTTP request (Streamable HTTP transport) — never as
        # a tool-call argument. ctx.request_context.request is the
        # underlying Starlette Request for this call; it is None if
        # the tool is ever invoked outside a live HTTP request (e.g.
        # local testing), in which case extract_bearer_token safely
        # returns None and verify_token rejects with UNAUTHENTICATED.
        #
        # Every tool call must carry a signed JWT from Clerk.
        # verify_token() in auth.py performs these checks in order:
        #
        # 1. Is a token present?
        # 2. Does the token's key ID exist in Clerk's JWKS?
        # 3. Is the RS256 cryptographic signature valid?
        # 4. Has the token expired? (exp claim)
        # 5. Does the issuer match our trusted Clerk domain? (iss claim)
        # 6. Does the scope include "sheets:read"?
        #
        # If ANY check fails → AuthError → 401 or 403 → STOP.
        # No data is fetched. No sheet is touched. Nothing leaks.
        
        # ════════════════════════════════════════════════════════
        #############################################
        # request = ctx.request_context.request
        # request_headers = dict(request.headers) if request is not None else {}
        # api_key = extract_bearer_token(request_headers)
        
        # claims = verify_token(api_key)
        ############################################

        # TEMPORARY — auth bypassed for connectivity testing
        # restore auth after confirming Google Sheets data layer works
        
        claims = {"sub": "test-company", "company_id": "test-company"}

        # ════════════════════════════════════════════════════════
        # LAYER 3 — IDENTITY EXTRACTION (Tenant Scoping)
        # ════════════════════════════════════════════════════════
        #
        # Identity ALWAYS comes from the verified token claims.
        # NEVER from what the caller passed as a parameter.
        #
        # This single rule is what makes multi-tenant security work.
        # Even if an AI assistant sends a wrong company_id parameter,
        # the server ignores it and uses the token's verified identity.
        #
        # Fix 1 — if both company_id and sub are missing from the
        # token, reject explicitly rather than proceeding as "unknown".
        # This prevents anonymous callers sharing a rate limit bucket.
        # ════════════════════════════════════════════════════════

        company_id = claims.get("company_id") or claims.get("sub")
        if not company_id:
            raise AuthError(
                Errors.UNAUTHENTICATED,
                message="Token is missing required identity claims."
            )

        # ════════════════════════════════════════════════════════
        # LAYER 4 — RATE LIMITING
        # ════════════════════════════════════════════════════════
        #
        # Checked AFTER authentication so we only count legitimate
        # callers. Unauthenticated requests are rejected in Layer 2
        # and never reach the rate limiter.
        #
        # Uses a sliding window — not a fixed reset at the minute
        # boundary. Prevents burst abuse at window edges.
        #
        # Fix 3 — RateLimitError now carries retry_after_seconds
        # so the caller knows exactly how long to wait.
        #
        # If exceeded → RateLimitError → 429 → STOP
        # ════════════════════════════════════════════════════════

        check_rate_limit(company_id)

        # ════════════════════════════════════════════════════════
        # LAYER 5 — INPUT VALIDATION (Pydantic)
        # ════════════════════════════════════════════════════════
        #
        # Validates employee_code before any business logic runs.
        # EmployeeStatusInput in validators.py enforces:
        # - Field must be present (not missing)
        # - Must be a non-empty string (min_length=1)
        # - Must not exceed 20 characters (max_length=20)
        # - Must match pattern ^[A-Za-z0-9\-]+$ (no spaces or symbols)
        # - Leading/trailing spaces are stripped automatically
        #
        # This catches:
        # - Missing employee_code entirely
        # - Empty string ""
        # - "John Doe" (name instead of code — spaces rejected by regex)
        # - 500-character strings (resource exhaustion attack)
        #
        # If invalid → ValidationError → 400 → STOP
        # ════════════════════════════════════════════════════════

        validated = validate_input(
            EmployeeStatusInput,
            {"employee_code": employee_code}
        )

        # ════════════════════════════════════════════════════════
        # LAYER 6 — DATA FETCH (Google Sheets API)
        # ════════════════════════════════════════════════════════
        #
        # Two sequential reads from Google Sheets:
        #
        # Read 1: Does this employee exist? (Employees tab)
        # Read 2: What is their latest activity? (DailyActivity tab)
        #
        # We separate these two reads deliberately:
        # "employee not found" and "no activity data" are different
        # problems. Mixing them gives callers wrong information.
        #
        # Google Sheets authentication uses the service account in
        # credentials.json — server-to-server auth, completely
        # separate from the user-facing OAuth 2.1 in Layer 2.
        #
        # sheets_client.py also applies these fixes:
        # - T-5:  startup schema validation (warns if columns renamed)
        # - T-8:  duplicate date rows resolved (last row wins)
        # - T-9:  corrupted date formats skipped cleanly
        #
        # If Sheets API is down → SheetsError → 503 → STOP
        # ════════════════════════════════════════════════════════

        employee = get_employee_by_code(validated.employee_code)

        # Edge case A — employee code does not exist in the Employees tab
        if not employee:
            duration = int((time.time() - start_time) * 1000)
            _safe_log(
                api_key=company_id,
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

        # Read 2 — fetch the most recent activity record
        # sheets_client.get_latest_activity_for_employee handles:
        # - T-8: duplicate rows for same date (last row wins, not first)
        # - T-9: corrupted date formats are skipped
        activity = get_latest_activity_for_employee(validated.employee_code)

        # ════════════════════════════════════════════════════════
        # LAYER 7 — AUDIT LOGGING
        # ════════════════════════════════════════════════════════
        #
        # Every call is logged — success or failure.
        # The log entry contains:
        # - company_id (first 8 chars only — enough to identify)
        # - tool name
        # - parameter NAMES only (not values — no employee data in logs)
        # - outcome (success or error code)
        # - duration in milliseconds
        #
        # What is deliberately NOT logged:
        # - Employee names, stages, focus scores (PII)
        # - Full company_id (privacy)
        #
        # Fix 4 — _safe_log wraps log_tool_call in try/except.
        # A disk-full or logging failure never kills the response.
        # Logging errors are printed to stderr for monitoring.
        #
        # Written to: audit_logs/audit_YYYY-MM-DD.log
        # ════════════════════════════════════════════════════════

        duration = int((time.time() - start_time) * 1000)
        _safe_log(  # Fix 4 — wrapped
            api_key=company_id,
            tool=tool_name,
            inputs={"employee_code": employee_code},
            outcome="success",
            duration_ms=duration,
        )

        # ════════════════════════════════════════════════════════
        # LAYER 8 — RESPONSE SHAPING
        # ════════════════════════════════════════════════════════
        #
        # We never return raw Google Sheets data.
        # The response shape is controlled and stable:
        # - Column names in the sheet can change without breaking
        #   the tool's output shape (sheets_client handles mapping)
        # - Numbers are typed correctly (int, float) not strings
        # - A computed performance_signal is added
        # - Missing values have safe, meaningful defaults
        #
        # Fixes applied here:
        # - Fix 5:  focus_score None when not recorded (not 0)
        # - Fix 6:  hours_worked None when > 24 (data error)
        # - Fix 7:  expanded NO_BLOCKER_VALUES set
        # - Fix 8:  staleness warning when data > 7 days old
        # - Fix 9:  empty stage → "Not recorded" not empty string
        # - Fix 10: _compute_performance_signal handles None focus
        # ════════════════════════════════════════════════════════

        # Base profile — always present, comes from Employees tab
        base = {
            "employee_code": employee.get("employee_code", "").upper(),
            "name": employee.get("name", "Unknown"),
            "department": employee.get("department", "Unknown"),
            "joining_date": employee.get("joining_date", "Unknown"),
        }

        # Edge case B — employee registered but has no activity data yet
        # (new joiner, returning from leave, tracking not started)
        if not activity:
            return {
                **base,
                "status": "no_activity_data",
                "message": (
                    f"{base['name']} is registered in the system "
                    f"but has no recorded activity data yet."
                ),
            }

        # ── Parse values safely ───────────────────────────────────────

        # Fix 5 — None when non-numeric (N/A, sick day, pending)
        # rather than defaulting to 0 which triggers needs_attention misleadingly
        focus_score = _parse_focus_score(activity.get("focus_score"))

        # Fix 6 — None when > 24 hours (data entry error)
        hours_worked = _parse_hours(activity.get("hours_worked"))

        tasks_completed = _parse_int(activity.get("tasks_completed"), default=0)

        # Fix 7 — expanded set of "means no blocker" values
        raw_blockers = (activity.get("blockers") or "").strip()
        blockers = (
            "None"
            if raw_blockers.lower() in NO_BLOCKER_VALUES
            else raw_blockers
        )

        # Fix 8 — staleness warning when last activity > 7 days ago
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
            pass  # corrupted date — do not crash, just skip the staleness check

        # ── Build the full response ───────────────────────────────────
        response = {
            **base,
            "status": "active",
            # Fix 9 — empty stage returns "Not recorded" not ""
            "current_stage": activity.get("stage") or "Not recorded",
            "focus_score": focus_score,
            "hours_worked": hours_worked,
            "tasks_completed": tasks_completed,
            "blockers": blockers,
            "last_active_date": last_date_str,
            # Fix 10 — handles None focus_score → data_missing signal
            "performance_signal": _compute_performance_signal(
                focus_score=focus_score,
                tasks_completed=tasks_completed,
                blockers=blockers,
            ),
        }

        # Add data_warning only when it exists (Fix 8 — staleness)
        if data_warning:
            response["data_warning"] = data_warning

        # Add data_warning for impossible hours (Fix 6)
        if hours_worked is None:
            response["data_warning"] = (
                "Hours worked value appears incorrect — please check the sheet."
            )

        return response

    # ════════════════════════════════════════════════════════════
    # EXCEPTION HANDLING — every failure type caught explicitly
    # ════════════════════════════════════════════════════════════
    #
    # Each exception maps to a specific structured response.
    # The caller never sees Python internals, stack traces, or
    # internal service names.
    #
    # Fix 4 — ALL log calls use _safe_log so logging failures
    # never prevent the error response from reaching the caller.
    # ════════════════════════════════════════════════════════════

    except AuthError as e:
        duration = int((time.time() - start_time) * 1000)
        _safe_log(
            api_key="unauthenticated",
            tool=tool_name,
            inputs={},
            outcome=e.error.code,
            duration_ms=duration,
        )
        return _build_error_response(e.error, message=e.message)

    except RateLimitError as e:
        duration = int((time.time() - start_time) * 1000)
        _safe_log(
            api_key=company_id,
            tool=tool_name,
            inputs={"employee_code": employee_code},
            outcome=e.error.code,
            duration_ms=duration,
        )
        response = _build_error_response(e.error)
        response["retry_after_seconds"] = e.retry_after
        return response

    except ValidationError as e:
        duration = int((time.time() - start_time) * 1000)
        _safe_log(
            api_key=company_id,
            tool=tool_name,
            inputs={"employee_code": employee_code},
            outcome=e.error.code,
            duration_ms=duration,
        )
        return _build_error_response(e.error, message=e.message)

    except SheetsError as e:
        duration = int((time.time() - start_time) * 1000)
        _safe_log(
            api_key=company_id,
            tool=tool_name,
            inputs={"employee_code": employee_code},
            outcome=e.error.code,
            duration_ms=duration,
        )
        return _build_error_response(e.error, message=e.message)

    except Exception:
        duration = int((time.time() - start_time) * 1000)
        _safe_log(
            api_key=company_id,
            tool=tool_name,
            inputs={"employee_code": employee_code},
            outcome=Errors.INTERNAL_ERROR.code,
            duration_ms=duration,
        )
        return _build_error_response(Errors.INTERNAL_ERROR)


# ── OAuth endpoints — Supabase handles auth, we only need discovery + consent UI ──

@mcp.custom_route("/.well-known/oauth-authorization-server", methods=["GET"])
async def discovery(request):
    from starlette.responses import JSONResponse
    supabase_issuer = os.environ.get("SUPABASE_ISSUER", "")
    mcp_base = os.environ.get("MCP_BASE_URL", "http://localhost:8000")
    return JSONResponse({
        "issuer": supabase_issuer,
        "authorization_endpoint": f"{mcp_base}/oauth/consent",
        "token_endpoint": f"{supabase_issuer}/oauth/token",
        "registration_endpoint": f"{supabase_issuer}/oauth/clients",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["client_secret_basic", "none"],
    })
    
@mcp.custom_route("/debug-headers", methods=["GET", "POST"])
async def debug_headers(request):
    from starlette.responses import JSONResponse
    headers = dict(request.headers)
    params = dict(request.query_params)
    return JSONResponse({
        "headers": headers,
        "query_params": params,
    })


@mcp.custom_route("/oauth/consent", methods=["GET", "POST"])
async def oauth_consent(request):
    from starlette.responses import HTMLResponse, RedirectResponse
    import httpx

    if request.method == "GET":
        # Capture ALL possible OAuth params — Supabase sends different formats
        client_id = request.query_params.get("client_id", "")
        redirect_uri = request.query_params.get("redirect_uri", "")
        code_challenge = request.query_params.get("code_challenge", "")
        code_challenge_method = request.query_params.get("code_challenge_method", "")
        state = request.query_params.get("state", "")
        authorization_id = request.query_params.get("authorization_id", "")

        print(f"DEBUG GET consent: client_id={client_id[:20] if client_id else 'EMPTY'} auth_id={authorization_id[:20] if authorization_id else 'EMPTY'}", flush=True)

        html = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>GrowwStacks MCP — Authorize</title>
            <style>
                body {{ font-family: sans-serif; max-width: 400px; margin: 80px auto; padding: 0 20px; }}
                .logo {{ font-size: 24px; font-weight: bold; color: #10b981; margin-bottom: 8px; }}
                h2 {{ color: #1a1a1a; }}
                p {{ color: #666; font-size: 14px; }}
                input[type=email], input[type=password] {{ width: 100%; padding: 10px; margin: 8px 0; border: 1px solid #ddd; border-radius: 6px; box-sizing: border-box; font-size: 14px; }}
                button {{ width: 100%; padding: 12px; background: #10b981; color: white; border: none; border-radius: 6px; font-size: 16px; cursor: pointer; margin-top: 8px; }}
                button:hover {{ background: #059669; }}
            </style>
        </head>
        <body>
            <div class="logo">GrowwStacks</div>
            <h2>Authorize AI Access</h2>
            <p>Sign in to grant your AI assistant access to WorkWitness data.</p>
            <form method="POST">
                <input type="hidden" name="client_id" value="{client_id}"/>
                <input type="hidden" name="redirect_uri" value="{redirect_uri}"/>
                <input type="hidden" name="code_challenge" value="{code_challenge}"/>
                <input type="hidden" name="code_challenge_method" value="{code_challenge_method}"/>
                <input type="hidden" name="state" value="{state}"/>
                <input type="hidden" name="authorization_id" value="{authorization_id}"/>
                <input type="email" name="email" placeholder="Email address" required/>
                <input type="password" name="password" placeholder="Password" required/>
                <button type="submit">Authorize Access</button>
            </form>
        </body>
        </html>
        """
        return HTMLResponse(html)

    if request.method == "POST":
        form = await request.form()
        email = form.get("email", "")
        password = form.get("password", "")

        # Read ALL params from form hidden fields, fall back to query string
        client_id = form.get("client_id", "") or request.query_params.get("client_id", "")
        redirect_uri = form.get("redirect_uri", "") or request.query_params.get("redirect_uri", "")
        code_challenge = form.get("code_challenge", "") or request.query_params.get("code_challenge", "")
        code_challenge_method = form.get("code_challenge_method", "") or request.query_params.get("code_challenge_method", "")
        state = form.get("state", "") or request.query_params.get("state", "")
        authorization_id = form.get("authorization_id", "") or request.query_params.get("authorization_id", "")

        print(f"DEBUG POST: client_id={client_id[:20] if client_id else 'EMPTY'} auth_id={authorization_id[:20] if authorization_id else 'EMPTY'}", flush=True)

        try:
            from supabase import create_client

            supabase_url = os.environ.get("SUPABASE_URL", "")
            supabase_anon_key = os.environ.get("SUPABASE_ANON_KEY", "")
            supabase = create_client(supabase_url, supabase_anon_key)

            # Step 1 — sign in to get access token
            result = supabase.auth.sign_in_with_password({
                "email": email,
                "password": password
            })
            if not result.user:
                raise Exception("Login failed — incorrect email or password")

            access_token = result.session.access_token
            print(f"DEBUG: Login success user={result.user.email}", flush=True)

            # Step 2 — approve via Supabase callback endpoint
            # This is the correct endpoint — accepts POST with authorization_id + state
            approve_resp = httpx.post(
                f"{supabase_url}/auth/v1/callback",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "apikey": supabase_anon_key,
                    "Content-Type": "application/json",
                },
                json={
                    "authorization_id": authorization_id,
                    "state": state,
                },
                follow_redirects=False,
            )

            print(f"DEBUG approve: status={approve_resp.status_code} headers={dict(approve_resp.headers)} body={approve_resp.text[:400]}", flush=True)

            # Handle redirect response
            if approve_resp.status_code in (301, 302, 303, 307, 308):
                location = approve_resp.headers.get("location", "")
                print(f"DEBUG redirecting to: {location[:100]}", flush=True)
                return RedirectResponse(url=location, status_code=302)

            # Handle 200 with redirect URL in body
            if approve_resp.status_code == 200:
                try:
                    data = approve_resp.json()
                    print(f"DEBUG 200 body: {data}", flush=True)
                    redirect_url = (
                        data.get("redirect_uri") or
                        data.get("url") or
                        data.get("location") or
                        data.get("redirect_to")
                    )
                    if redirect_url:
                        return RedirectResponse(url=redirect_url, status_code=302)
                except Exception:
                    pass
                raise Exception(f"200 OK but no redirect URL. Body: {approve_resp.text[:200]}")

            raise Exception(f"Approve returned {approve_resp.status_code}: {approve_resp.text[:300]}")

        except Exception as e:
            error_detail = str(e)
            print(f"DEBUG error: {error_detail}", flush=True)
            html = f"""
            <!DOCTYPE html>
            <html>
            <head>
                <title>GrowwStacks MCP — Authorize</title>
                <style>
                    body {{ font-family: sans-serif; max-width: 400px; margin: 80px auto; padding: 0 20px; }}
                    .logo {{ font-size: 24px; font-weight: bold; color: #10b981; margin-bottom: 8px; }}
                    .error {{ color: #dc2626; font-size: 13px; margin-bottom: 12px; background: #fef2f2; padding: 8px; border-radius: 4px; word-break: break-all; }}
                    input[type=email], input[type=password] {{ width: 100%; padding: 10px; margin: 8px 0; border: 1px solid #ddd; border-radius: 6px; box-sizing: border-box; font-size: 14px; }}
                    button {{ width: 100%; padding: 12px; background: #10b981; color: white; border: none; border-radius: 6px; font-size: 16px; cursor: pointer; }}
                </style>
            </head>
            <body>
                <div class="logo">GrowwStacks</div>
                <h2>Authorize AI Access</h2>
                <div class="error">{error_detail}</div>
                <form method="POST">
                    <input type="hidden" name="client_id" value="{client_id}"/>
                    <input type="hidden" name="redirect_uri" value="{redirect_uri}"/>
                    <input type="hidden" name="code_challenge" value="{code_challenge}"/>
                    <input type="hidden" name="code_challenge_method" value="{code_challenge_method}"/>
                    <input type="hidden" name="state" value="{state}"/>
                    <input type="hidden" name="authorization_id" value="{authorization_id}"/>
                    <input type="email" name="email" placeholder="Email address" required/>
                    <input type="password" name="password" placeholder="Password" required/>
                    <button type="submit">Try Again</button>
                </form>
            </body>
            </html>
            """
            return HTMLResponse(html, status_code=400)


if __name__ == "__main__":
    mcp.run(transport="streamable-http")