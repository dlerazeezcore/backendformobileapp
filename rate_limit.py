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
# "login:ip:<ip>") so independent limits do not collide.

_LOCK = threading.Lock()
_EVENTS: dict[str, deque[float]] = defaultdict(deque)

# BE-3: opportunistic sweep to stop ``_EVENTS`` growing one dead key per distinct
# phone/IP forever. Every distinct caller creates a bucket; once its events age
# out the (now empty) key would otherwise linger indefinitely. We sweep drained
# buckets periodically. ``_SWEEP_HORIZON_SECONDS`` MUST be >= the largest window
# any caller passes to ``enforce_rate_limit`` (currently 3600s) so a live bucket
# is never dropped mid-window.
_SWEEP_EVERY_CALLS = 1024
_SWEEP_HORIZON_SECONDS = 3600
_calls_since_sweep = 0


def _enabled() -> bool:
    # Default ON in prod; can be turned off via RATE_LIMIT_ENABLED=false.
    return read_bool_env("RATE_LIMIT_ENABLED", True)


def _test_bypass_active() -> bool:
    """SEC-7: the limiter no-ops under pytest so the suite can hammer endpoints
    without tripping 429s. Gate that bypass behind an explicit, overridable flag
    (default on, but only under pytest) so a production process can never be
    silently un-limited just because ``PYTEST_CURRENT_TEST`` leaked into its env.
    """
    if not os.environ.get("PYTEST_CURRENT_TEST"):
        return False
    return read_bool_env("RATE_LIMIT_BYPASS_IN_TESTS", True)


def reset() -> None:
    """Clear all recorded events. Intended for tests."""
    global _calls_since_sweep
    with _LOCK:
        _EVENTS.clear()
        _calls_since_sweep = 0


def _sweep_stale_buckets_locked(now: float) -> None:
    """Drop buckets that can no longer affect a decision: empty ones, or ones
    whose most recent event is older than the sweep horizon. Caller holds _LOCK."""
    horizon = now - _SWEEP_HORIZON_SECONDS
    stale_keys = [key for key, bucket in _EVENTS.items() if not bucket or bucket[-1] <= horizon]
    for key in stale_keys:
        del _EVENTS[key]


def enforce_rate_limit(key: str, max_events: int, window_seconds: int) -> None:
    """Record one event for ``key`` and raise HTTP 429 when the sliding window
    has more than ``max_events`` events within ``window_seconds``.

    No-op under the pytest test-bypass flag (so the existing suite stays green)
    and when the RATE_LIMIT_ENABLED flag is disabled.
    """
    global _calls_since_sweep
    if _test_bypass_active():
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

        _calls_since_sweep += 1
        if _calls_since_sweep >= _SWEEP_EVERY_CALLS:
            _calls_since_sweep = 0
            _sweep_stale_buckets_locked(now)
