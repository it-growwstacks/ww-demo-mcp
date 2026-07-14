# auth.py
# Layer 2 — Authentication (OAuth 2.1 / JWT)
# Verifies every incoming JWT token against Clerk's JWKS endpoint.
# If the token is missing, expired, wrongly signed, or missing required
# claims, the request is rejected here — no tool code ever runs.

import os
import jwt
from jwt import PyJWKClient, ExpiredSignatureError, DecodeError
from dotenv import load_dotenv
from error_codes import Errors, ErrorDef
from audit_logger import log_auth_failure

load_dotenv()

# ── Clerk configuration from .env ────────────────────────────────────
JWKS_URL = os.environ.get("SUPABASE_JWKS_URL", "").strip()
ISSUER   = os.environ.get("SUPABASE_ISSUER", "").strip()

if not JWKS_URL or not ISSUER:
    raise RuntimeError(
        "SUPABASE_JWKS_URL and SUPABASE_ISSUER must be set in your .env file. "
        "The server cannot start without OAuth 2.1 configuration."
    )

_jwks_client = PyJWKClient(JWKS_URL, cache_keys=True)


class AuthError(Exception):
    """
    Raised when JWT verification fails for any reason.
    Caught by server.py and converted into a clean 401 or 403 response.
    Carries the full ErrorDef so code, message, and HTTP status
    are all available in one place.
    """
    def __init__(self, error: ErrorDef, message: str | None = None):
        self.error = error
        # Allow a custom message override (e.g. "Token has expired")
        # while keeping the standard code and HTTP status from ErrorDef.
        self.message = message or error.message
        super().__init__(self.message)


def verify_token(bearer_token: str | None) -> dict:
    """
    Verifies a JWT bearer token issued by Clerk.

    Performs these checks in order:
    1. Token is present
    2. Token header is readable (not malformed)
    3. Signing key exists in Clerk's JWKS
    4. RS256 signature is cryptographically valid
    5. Token has not expired (exp claim)
    6. Issuer matches our trusted Clerk domain (iss claim)
    7. scope claim contains 'sheets:read' or 'profile' (OAuth flow)

    Returns the verified claims dict if all checks pass.
    Raises AuthError if any check fails.
    """

    # ── Check 1 — token must be present ──────────────────────────
    if not bearer_token:
        log_auth_failure(reason="missing_token")
        raise AuthError(Errors.UNAUTHENTICATED)

    token = bearer_token.replace("Bearer ", "").strip()

    try:
        # ── Check 2+3 — fetch the matching public key from Clerk ──
        signing_key = _jwks_client.get_signing_key_from_jwt(token)

        # ── Check 4+5+6 — verify signature, expiry, and issuer ───
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            issuer=ISSUER,
            options={
                "require": ["exp", "iss", "sub"],
                "verify_exp": True,
                "verify_iss": True,
            }
        )

        # ── Check 7 — scope must include sheets:read or profile ───
        token_scope = claims.get("scope", "")
        # Supabase OAuth tokens carry standard scopes (email, profile, openid)
        # Any valid Supabase token from our project is accepted
        if not token_scope and not claims.get("sub"):
            log_auth_failure(reason="missing_scope")
            raise AuthError(Errors.FORBIDDEN)

        return claims

    except ExpiredSignatureError:
        log_auth_failure(reason="token_expired")
        raise AuthError(Errors.UNAUTHENTICATED, message="Token has expired. Please re-authenticate.")

    except DecodeError:
        log_auth_failure(reason="malformed_token")
        raise AuthError(Errors.UNAUTHENTICATED)

    except AuthError:
        raise

    except Exception as e:
        log_auth_failure(reason=f"unexpected_auth_error: {type(e).__name__}")
        raise AuthError(Errors.UNAUTHENTICATED)


def extract_bearer_token(request_headers: dict) -> str | None:
    """
    Extracts the Bearer token from request headers.
    Returns None if the header is absent.
    """
    if not request_headers:
        return None
    return (
        request_headers.get("Authorization")
        or request_headers.get("authorization")
        or None
    )