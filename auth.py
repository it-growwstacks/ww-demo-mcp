# auth.py
# Layer 2 — Authentication (OAuth 2.1 / JWT)
# Verifies every incoming JWT token against Clerk's JWKS endpoint.
# If the token is missing, expired, wrongly signed, or missing required
# claims, the request is rejected here — no tool code ever runs.
#
# This replaces the simple API key check with full cryptographic
# JWT verification using RS256 asymmetric signing.

import os
import jwt
import requests
from jwt import PyJWKClient, ExpiredSignatureError, DecodeError
from dotenv import load_dotenv
from error_codes import ErrorCode, ErrorMessage
from audit_logger import log_auth_failure

load_dotenv()

# ── Clerk configuration from .env ────────────────────────────────────
JWKS_URL  = os.environ.get("CLERK_JWKS_URL", "").strip()
ISSUER    = os.environ.get("CLERK_ISSUER", "").strip()
AUDIENCE  = os.environ.get("CLERK_AUDIENCE", "mcp-access").strip()

# Fail immediately at startup if critical config is missing
if not JWKS_URL or not ISSUER:
    raise RuntimeError(
        "CLERK_JWKS_URL and CLERK_ISSUER must be set in your .env file. "
        "The server cannot start without OAuth 2.1 configuration."
    )

# ── JWKS client ───────────────────────────────────────────────────────
# PyJWKClient fetches Clerk's public keys from the JWKS endpoint.
# It caches the keys automatically and refreshes when Clerk rotates them.
# This means your server always has the correct public key without
# any manual key management on your part.
_jwks_client = PyJWKClient(JWKS_URL, cache_keys=True)


class AuthError(Exception):
    """
    Raised when JWT verification fails for any reason.
    Caught by server.py and converted into a clean 401 or 403 response.
    Carries both a machine-readable code and a human-readable message.
    """
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


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
    7. scope claim contains 'sheets:read'

    Returns the verified claims dict if all checks pass.
    Raises AuthError if any check fails.

    The returned claims dict contains:
    - sub         : Clerk user ID (this becomes the tenant identity)
    - company_id  : mapped from user.id in our JWT template
    - scope       : "sheets:read"
    - exp         : expiry timestamp
    - iss         : issuer URL
    """

    # ── Check 1 — token must be present ──────────────────────────────
    if not bearer_token:
        log_auth_failure(reason="missing_token")
        raise AuthError(
            code=ErrorCode.UNAUTHENTICATED,
            message=ErrorMessage.UNAUTHENTICATED
        )

    # Strip "Bearer " prefix if present
    # Some clients send "Bearer eyJ..." others send just "eyJ..."
    token = bearer_token.replace("Bearer ", "").strip()

    try:
        # ── Check 2+3 — fetch the matching public key from Clerk ─────
        # PyJWKClient reads the token header to find the key ID (kid),
        # then fetches the matching public key from Clerk's JWKS endpoint.
        # If the key ID does not exist in Clerk's JWKS, this raises an error —
        # meaning forged tokens with fake key IDs are rejected here.
        signing_key = _jwks_client.get_signing_key_from_jwt(token)

        # ── Check 4+5+6 — verify signature, expiry, and issuer ───────
        # jwt.decode performs all cryptographic verification in one call:
        # - Verifies the RS256 signature using the public key
        # - Checks the exp claim — rejects if token has expired
        # - Checks the iss claim — rejects if issuer does not match
        # If any check fails, it raises a specific exception we catch below
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

        # ── Check 7 — scope must include sheets:read ──────────────────
        # The scope claim was added by our JWT template in Clerk.
        # Without this check, any valid Clerk token could access our server —
        # even tokens issued for completely different applications. 
        token_scope = claims.get("scope", "")
        if "sheets:read" not in token_scope and "profile" not in token_scope:
            log_auth_failure(reason="missing_scope")
            raise AuthError(
                code=ErrorCode.FORBIDDEN,
                message=ErrorMessage.FORBIDDEN
            )

        # All checks passed — return the verified claims
        # server.py will extract company_id from claims["company_id"]
        return claims

    except ExpiredSignatureError:
        # Token exists and was valid, but has passed its expiry time
        log_auth_failure(reason="token_expired")
        raise AuthError(
            code=ErrorCode.UNAUTHENTICATED,
            message="Token has expired. Please re-authenticate."
        )

    except DecodeError:
        # Token is malformed — not a valid JWT structure
        log_auth_failure(reason="malformed_token")
        raise AuthError(
            code=ErrorCode.UNAUTHENTICATED,
            message=ErrorMessage.UNAUTHENTICATED
        )

    except AuthError:
        # Re-raise our own AuthError without wrapping it again
        raise

    except Exception as e:
        # Catch-all for unexpected errors — JWKS fetch failure, network issues
        # Log the real error internally but never expose it to the caller
        log_auth_failure(reason=f"unexpected_auth_error: {type(e).__name__}")
        raise AuthError(
            code=ErrorCode.UNAUTHENTICATED,
            message=ErrorMessage.UNAUTHENTICATED
        )


def extract_bearer_token(request_headers: dict) -> str | None:
    """
    Extracts the Bearer token from request headers.

    Looks for the Authorization header in common capitalisation variants.
    Returns the full header value (e.g. "Bearer eyJ...").
    Returns None if the header is absent.

    server.py passes this into verify_token() on every tool call.
    """
    if not request_headers:
        return None

    return (
        request_headers.get("Authorization")
        or request_headers.get("authorization")
        or None
    )