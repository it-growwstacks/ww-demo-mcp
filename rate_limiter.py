# rate_limiter.py
# Layer 4 — Rate Limiting
# Tracks how many requests each API key has made in the last 60 seconds.
# Rejects requests that exceed the configured limit with a 429 error.
# Uses a sliding window — not a fixed reset — to prevent burst abuse.

import os
import time
from collections import defaultdict
from dotenv import load_dotenv
from error_codes import Errors
from audit_logger import log_rate_limit

load_dotenv()

_RATE_LIMIT    = int(os.environ.get("RATE_LIMIT_PER_MINUTE", "60"))
_WINDOW_SECONDS = 60

_call_timestamps: dict[str, list[float]] = defaultdict(list)


class RateLimitError(Exception):
    """
    Raised when a caller exceeds their request limit.
    Caught by server.py and converted into a 429 response.
    """
    def __init__(self, retry_after: int = 60):
        self.error = Errors.RATE_LIMITED
        self.retry_after = retry_after
        super().__init__(self.error.message)


def check_rate_limit(api_key: str) -> None:
    """
    Checks whether this api_key has exceeded the rate limit.

    Raises RateLimitError if the limit is exceeded.
    Returns None if the call is allowed.
    """
    now = time.time()
    window_start = now - _WINDOW_SECONDS

    _call_timestamps[api_key] = [
        ts for ts in _call_timestamps[api_key]
        if ts > window_start
    ]

    call_count = len(_call_timestamps[api_key])

    if call_count >= _RATE_LIMIT:
        log_rate_limit(api_key=api_key)
        oldest = min(_call_timestamps[api_key])
        retry_after = int(_WINDOW_SECONDS - (now - oldest)) + 1
        raise RateLimitError(retry_after=retry_after)

    _call_timestamps[api_key].append(now)


def get_current_usage(api_key: str) -> dict:
    """
    Returns the current rate limit status for an api_key.
    Useful for debugging — not called in the main request path.
    """
    now = time.time()
    window_start = now - _WINDOW_SECONDS
    recent_calls = [ts for ts in _call_timestamps[api_key] if ts > window_start]
    return {
        "calls_in_window": len(recent_calls),
        "limit": _RATE_LIMIT,
        "remaining": max(0, _RATE_LIMIT - len(recent_calls)),
    }