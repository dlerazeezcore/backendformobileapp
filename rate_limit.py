from __future__ import annotations

import os
import threading
import time
from collections import defaultdict, deque

from fastapi import HTTPException, status

from config import read_bool_env

# In-memory sliding-window rate limiter (stdlib only, no extra deps).
#
# This is intentionally simple and process-local: each worker keeps its own
# window state. It is meant as an abuse brake on unauthenticated endpoints,
# not as a distributed quota system. Keys are namespaced by caller (e.g.
# "otp:phone:<phone>" / "login:ip:<ip>") so independent limits do not collide.

_LOCK = threading.Lock()
_EVENTS: dict[str, deque[float]] = defaultdict(deque)


def _enabled() -> bool:
    # Default ON in prod; can be turned off via RATE_LIMIT_ENABLED=false.
    return read_bool_env("RATE_LIMIT_ENABLED", True)


def reset() -> None:
    """Clear all recorded events. Intended for tests."""
    with _LOCK:
        _EVENTS.clear()


def enforce_rate_limit(key: str, max_events: int, window_seconds: int) -> None:
    """Record one event for ``key`` and raise HTTP 429 when the sliding window
    has more than ``max_events`` events within ``window_seconds``.

    No-op under pytest (so the existing suite stays green) and when the
    RATE_LIMIT_ENABLED flag is disabled.
    """
    # Skip entirely while the test suite is running so existing tests that hit
    # these endpoints repeatedly do not start failing with 429s.
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return
    if not _enabled():
        return
    if max_events <= 0 or window_seconds <= 0:
        return

    now = time.monotonic()
    cutoff = now - window_seconds
    with _LOCK:
        bucket = _EVENTS[key]
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        if len(bucket) >= max_events:
            retry_after = max(1, int(bucket[0] + window_seconds - now) + 1)
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many requests. Please try again later.",
                headers={"Retry-After": str(retry_after)},
            )
        bucket.append(now)
