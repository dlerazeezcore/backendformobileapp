from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from config import get_settings
from phone_utils import normalize_phone
from rate_limit import enforce_rate_limit
from supabase_store import OtpCode

LOGGER = logging.getLogger("uvicorn.error")

# ---------------------------------------------------------------------------
# VerifyWay OTP domain module.
#
# Flow: POST /api/v1/otp/send generates a random numeric code server-side,
# delivers it to the caller-supplied phone via VerifyWay (WhatsApp by default),
# and stores only an HMAC digest of it. POST /api/v1/otp/verify checks the code
# with attempt caps and TTL. Codes are single-use.
#
# State lives in the ``otp_codes`` table (one row per normalized phone,
# migration 0049) — NOT in process memory. Production runs uvicorn with
# multiple workers, so send and verify routinely land on different processes;
# only shared storage makes the flow reliable.
# ---------------------------------------------------------------------------

_VERIFIED_TTL_SECONDS = 600  # window in which a successful verify can be consumed


class VerifyWayError(Exception):
    pass


# ---------------------------------------------------------------------------
# Code generation & hashing. Only the HMAC digest is stored, keyed with the
# deployment auth secret, so a DB leak or stray log never exposes a live
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


def _utcnow() -> datetime:
    # This module writes/compares in plain UTC (NOT supabase_store.utcnow's
    # GMT+3): SQLite drops the offset on write, so any non-UTC zone would skew
    # every reread timestamp by the offset. Postgres timestamptz is unaffected.
    return datetime.now(timezone.utc)


def _as_aware(value: datetime | None) -> datetime | None:
    # SQLite returns naive datetimes even for DateTime(timezone=True); Postgres
    # returns aware ones. Normalize so comparisons never raise.
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def consume_recent_verification(db: Session, phone: str) -> bool:
    """Return True (once) if ``phone`` completed OTP verification within the
    last _VERIFIED_TTL_SECONDS. Consuming clears the flag so a single verify
    cannot authorize two separate sensitive actions."""
    normalized = normalize_phone(phone)
    row = db.get(OtpCode, normalized)
    verified_at = _as_aware(row.verified_at) if row is not None else None
    if verified_at is None:
        return False
    row.verified_at = None
    db.commit()
    return (_utcnow() - verified_at) <= timedelta(seconds=_VERIFIED_TTL_SECONDS)


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
def register_verifyway_routes(app: FastAPI, get_db: Callable[..., Any]) -> None:
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
    async def otp_send(
        payload: OtpSendRequest,
        request: Request,
        db: Session = Depends(get_db),
    ) -> dict[str, Any]:
        settings = get_settings()
        normalized = _normalized_or_400(payload.phone)

        # Abuse brakes: per-IP and per-phone sliding windows, plus a hard
        # resend cooldown so one phone cannot be flooded with WhatsApp messages.
        enforce_rate_limit(f"otp:send:ip:{_client_ip(request)}", max_events=10, window_seconds=3600)
        enforce_rate_limit(f"otp:send:phone:{normalized}", max_events=5, window_seconds=3600)

        now = _utcnow()
        cooldown = settings.verifyway_otp_resend_cooldown_seconds
        row = db.get(OtpCode, normalized)
        last_sent_at = _as_aware(row.last_sent_at) if row is not None else None
        if last_sent_at is not None:
            elapsed = (now - last_sent_at).total_seconds()
            if elapsed < cooldown:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="A code was just sent. Please wait before requesting another.",
                    headers={"Retry-After": str(max(1, int(cooldown - elapsed) + 1))},
                )

        code = _generate_code(settings.verifyway_otp_length)
        await _post_otp(_to_recipient(normalized), code)

        # Persist only after a successful upstream send so a delivery failure
        # never burns the resend cooldown.
        if row is None:
            row = OtpCode(phone=normalized)
            db.add(row)
        row.code_digest = _code_digest(normalized, code)
        row.expires_at = now + timedelta(seconds=settings.verifyway_otp_ttl_seconds)
        row.attempts_left = settings.verifyway_otp_max_attempts
        row.last_sent_at = now
        row.verified_at = None
        try:
            db.commit()
        except IntegrityError:
            # Two concurrent first-sends raced on the same phone; the other
            # request's code is live, so surface the cooldown response.
            db.rollback()
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="A code was just sent. Please wait before requesting another.",
                headers={"Retry-After": str(cooldown)},
            )

        return {
            "ok": True,
            "channel": settings.verifyway_channel,
            "expiresInSeconds": settings.verifyway_otp_ttl_seconds,
            "resendInSeconds": cooldown,
        }

    @app.post("/api/v1/otp/verify")
    async def otp_verify(
        payload: OtpVerifyRequest,
        request: Request,
        db: Session = Depends(get_db),
    ) -> dict[str, Any]:
        normalized = _normalized_or_400(payload.phone)
        enforce_rate_limit(f"otp:verify:ip:{_client_ip(request)}", max_events=30, window_seconds=3600)

        submitted = payload.code.strip()
        now = _utcnow()
        invalid = HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired code. Please request a new one.",
        )

        row = db.get(OtpCode, normalized)
        expires_at = _as_aware(row.expires_at) if row is not None else None
        if row is None or not row.code_digest or expires_at is None or now >= expires_at:
            raise invalid
        if not hmac.compare_digest(row.code_digest, _code_digest(normalized, submitted)):
            row.attempts_left -= 1
            if row.attempts_left <= 0:
                row.code_digest = None
                row.expires_at = None
            db.commit()
            raise invalid
        # Success: single-use — clear the pending code, record the proof.
        row.code_digest = None
        row.expires_at = None
        row.verified_at = now
        db.commit()

        return {"ok": True, "verified": True, "phone": normalized}
