from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

import httpx
from fastapi import FastAPI, HTTPException, Request, status
from pydantic import BaseModel, Field

from config import get_settings
from phone_utils import normalize_phone
from rate_limit import enforce_rate_limit

LOGGER = logging.getLogger("uvicorn.error")

# ---------------------------------------------------------------------------
# VerifyWay OTP domain module.
#
# Flow: POST /api/v1/otp/send generates a random numeric code server-side,
# delivers it to the caller-supplied phone via VerifyWay (WhatsApp by default),
# and stores only an HMAC digest of it. POST /api/v1/otp/verify checks the code
# with attempt caps and TTL. Codes are single-use.
#
# The pending-code store is process-local and in-memory (same trade-off as
# rate_limit.py): it is an auth-code cache with a 5-minute lifetime, not
# durable state, so it needs no migration. If the deployment ever runs
# multiple worker processes behind one load balancer, move this store to the
# database or Redis so send/verify can land on different workers.
# ---------------------------------------------------------------------------

_VERIFIED_TTL_SECONDS = 600  # window in which a successful verify can be consumed


class VerifyWayError(Exception):
    pass


@dataclass
class _OtpState:
    code_digest: str
    expires_at: float  # time.monotonic() basis
    attempts_left: int
    last_sent_at: float


_LOCK = threading.Lock()
_PENDING: dict[str, _OtpState] = {}
# phone -> monotonic timestamp of last successful verification. Lets a future
# auth flow (signup/login/phone-change in auth.py) confirm "this phone was just
# proven" via consume_recent_verification() without re-plumbing OTP state.
_VERIFIED_AT: dict[str, float] = {}


def reset() -> None:
    """Clear all OTP state. Intended for tests."""
    with _LOCK:
        _PENDING.clear()
        _VERIFIED_AT.clear()


# ---------------------------------------------------------------------------
# Code generation & hashing. Only the HMAC digest is stored, keyed with the
# deployment auth secret, so a memory dump or stray log never exposes a live
# code. The plaintext code exists only in the outbound VerifyWay request.
# ---------------------------------------------------------------------------
def _generate_code(length: int) -> str:
    return f"{secrets.randbelow(10 ** length):0{length}d}"


def _code_digest(phone: str, code: str) -> str:
    key = get_settings().auth_secret_key.encode("utf-8")
    return hmac.new(key, f"{phone}:{code}".encode("utf-8"), hashlib.sha256).hexdigest()


def _to_recipient(normalized_phone: str) -> str:
    # VerifyWay expects digits without the leading "+" (e.g. "9647507343635").
    return normalized_phone.lstrip("+")


def consume_recent_verification(phone: str) -> bool:
    """Return True (once) if ``phone`` completed OTP verification within the
    last _VERIFIED_TTL_SECONDS. Consuming clears the flag so a single verify
    cannot authorize two separate sensitive actions."""
    normalized = normalize_phone(phone)
    now = time.monotonic()
    with _LOCK:
        verified_at = _VERIFIED_AT.pop(normalized, None)
    return verified_at is not None and (now - verified_at) <= _VERIFIED_TTL_SECONDS


# ---------------------------------------------------------------------------
# Upstream call.
# ---------------------------------------------------------------------------
async def _post_otp(recipient: str, code: str) -> dict[str, Any]:
    settings = get_settings()
    api_key = settings.verifyway_api_key
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"provider": "verifyway", "error": "VERIFYWAY_API_KEY is not configured"},
        )
    payload = {
        "recipient": recipient,
        "type": "otp",
        "code": code,
        "channel": settings.verifyway_channel,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=settings.verifyway_timeout_seconds) as client:
            resp = await client.post(settings.verifyway_base_url, headers=headers, json=payload)
    except httpx.HTTPError as exc:
        LOGGER.error("VerifyWay OTP request failed to connect: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"provider": "verifyway", "error": "OTP delivery failed"},
        ) from exc

    try:
        body = resp.json()
    except Exception:
        body = {"raw_text": resp.text}
    if resp.status_code >= 400:
        # Log status + body for operators; never log the code itself.
        LOGGER.error(
            "VerifyWay OTP send failed: status=%s recipient=%s body=%s",
            resp.status_code,
            recipient,
            body,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"provider": "verifyway", "error": "OTP delivery failed"},
        )
    return body


# ---------------------------------------------------------------------------
# Request models.
# ---------------------------------------------------------------------------
class OtpSendRequest(BaseModel):
    phone: str = Field(..., description="Recipient phone in any common format; normalized server-side")


class OtpVerifyRequest(BaseModel):
    phone: str
    code: str = Field(..., min_length=4, max_length=8)


def _normalized_or_400(phone: str) -> str:
    normalized = normalize_phone(phone)
    digits = normalized.lstrip("+")
    if len(digits) < 9 or not digits.isdigit():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A valid phone number is required.",
        )
    return normalized


# ---------------------------------------------------------------------------
# Routes.
# ---------------------------------------------------------------------------
def register_verifyway_routes(app: FastAPI, get_db: Callable[..., Any] | None = None) -> None:
    _ = get_db  # OTP state is not DB-backed (see module header); kept for wiring symmetry.

    def _client_ip(request: Request) -> str:
        client = request.client
        return client.host if client and client.host else "unknown"

    @app.get("/api/v1/otp/health")
    async def otp_health() -> dict[str, Any]:
        settings = get_settings()
        return {
            "ok": True,
            "service": "verifyway-otp",
            "base_url": settings.verifyway_base_url,
            "channel": settings.verifyway_channel,
            "api_key_configured": bool(settings.verifyway_api_key),
            "code_length": settings.verifyway_otp_length,
            "ttl_seconds": settings.verifyway_otp_ttl_seconds,
        }

    @app.post("/api/v1/otp/send")
    async def otp_send(payload: OtpSendRequest, request: Request) -> dict[str, Any]:
        settings = get_settings()
        normalized = _normalized_or_400(payload.phone)

        # Abuse brakes: per-IP and per-phone sliding windows, plus a hard
        # resend cooldown so one phone cannot be flooded with WhatsApp messages.
        enforce_rate_limit(f"otp:send:ip:{_client_ip(request)}", max_events=10, window_seconds=3600)
        enforce_rate_limit(f"otp:send:phone:{normalized}", max_events=5, window_seconds=3600)

        now = time.monotonic()
        cooldown = settings.verifyway_otp_resend_cooldown_seconds
        with _LOCK:
            existing = _PENDING.get(normalized)
            if existing is not None and (now - existing.last_sent_at) < cooldown:
                retry_after = max(1, int(cooldown - (now - existing.last_sent_at)) + 1)
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="A code was just sent. Please wait before requesting another.",
                    headers={"Retry-After": str(retry_after)},
                )

        code = _generate_code(settings.verifyway_otp_length)
        await _post_otp(_to_recipient(normalized), code)

        # Store only after a successful upstream send so a delivery failure
        # never burns the resend cooldown.
        with _LOCK:
            _PENDING[normalized] = _OtpState(
                code_digest=_code_digest(normalized, code),
                expires_at=now + settings.verifyway_otp_ttl_seconds,
                attempts_left=settings.verifyway_otp_max_attempts,
                last_sent_at=now,
            )

        return {
            "ok": True,
            "channel": settings.verifyway_channel,
            "expiresInSeconds": settings.verifyway_otp_ttl_seconds,
            "resendInSeconds": cooldown,
        }

    @app.post("/api/v1/otp/verify")
    async def otp_verify(payload: OtpVerifyRequest, request: Request) -> dict[str, Any]:
        normalized = _normalized_or_400(payload.phone)
        enforce_rate_limit(f"otp:verify:ip:{_client_ip(request)}", max_events=30, window_seconds=3600)

        submitted = payload.code.strip()
        now = time.monotonic()
        invalid = HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired code. Please request a new one.",
        )

        with _LOCK:
            state = _PENDING.get(normalized)
            if state is None or now >= state.expires_at:
                _PENDING.pop(normalized, None)
                raise invalid
            if not hmac.compare_digest(state.code_digest, _code_digest(normalized, submitted)):
                state.attempts_left -= 1
                if state.attempts_left <= 0:
                    del _PENDING[normalized]
                raise invalid
            # Success: single-use — drop the pending code, record the proof.
            del _PENDING[normalized]
            _VERIFIED_AT[normalized] = now

        return {"ok": True, "verified": True, "phone": normalized}
