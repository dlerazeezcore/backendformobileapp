from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import secrets
import time
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request, status
from pydantic import BaseModel, Field

from config import get_settings
from phone_utils import normalize_phone
from rate_limit import enforce_rate_limit

LOGGER = logging.getLogger("uvicorn.error")

# ---------------------------------------------------------------------------
# VerifyWay OTP domain module — fully STATELESS (no DB, no process memory).
#
# Send: generate a random numeric code, deliver it via VerifyWay (WhatsApp by
# default), and return a signed "challenge" token to the client. The code is
# NOT inside the token — it is mixed into the token's HMAC signature, so the
# server can re-derive validity later without storing anything.
#
# Verify: client returns {phone, code, challenge}. We check the challenge's
# expiry + phone binding, then recompute the HMAC with the submitted code; a
# match proves this exact code was the one sent for this phone. On success we
# mint a short-lived "phone verification" token (same HS256 shape as auth.py's
# session JWTs) that login/signup/forgot-password flows can require as proof
# of phone ownership via validate_verification_token().
#
# Statelessness works on any number of uvicorn workers. Known trade-off: a
# (code, challenge) pair stays re-verifiable until the challenge expires
# (single-use bookkeeping needs storage); TTL + per-phone rate limits keep
# that window small, and an attacker still needs BOTH the WhatsApp code and
# the challenge held by the requesting device.
# ---------------------------------------------------------------------------

_CHALLENGE_TYP = "otp-challenge"
_VERIFICATION_TYP = "phone-verification"
_VERIFICATION_TTL_SECONDS = 600


class VerifyWayError(Exception):
    pass


# ---------------------------------------------------------------------------
# Token plumbing (same stdlib-HMAC style as auth.py; local copies keep the
# domain self-contained per the one-file-per-domain rule).
# ---------------------------------------------------------------------------
def _urlsafe_b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _urlsafe_b64decode(raw: str) -> bytes:
    padding = "=" * (-len(raw) % 4)
    return base64.urlsafe_b64decode((raw + padding).encode("ascii"))


def _json_dumps(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _generate_code(length: int) -> str:
    return f"{secrets.randbelow(10 ** length):0{length}d}"


def _to_recipient(normalized_phone: str) -> str:
    # VerifyWay expects digits without the leading "+" (e.g. "9647507343635").
    return normalized_phone.lstrip("+")


def _mask_phone(value: str | None) -> str:
    """Mask a phone number for logging — keep only the last 3 digits (SEC-6).

    Local replica of auth.py's ``_mask_phone`` (same rule); kept in-module per
    the one-file-per-domain convention."""
    digits = "".join(ch for ch in (value or "") if ch.isdigit())
    if len(digits) <= 3:
        return "***"
    return f"***{digits[-3:]}"


_PHONE_DIGIT_RUN = re.compile(r"\d{7,}")


def _scrub_phone_digits(value: object) -> str:
    """Redact long digit runs (7+) before logging provider payloads — VerifyWay
    error bodies can echo the recipient MSISDN, which would defeat the SEC-6
    masking applied to the ``recipient`` field."""
    return _PHONE_DIGIT_RUN.sub("***", str(value))


def _challenge_signature(payload_segment: str, code: str) -> bytes:
    # The submitted code is part of the MAC input, NOT the token payload:
    # possession of the challenge alone reveals nothing about the code.
    key = get_settings().auth_secret_key.encode("utf-8")
    return hmac.new(key, f"{payload_segment}.{code}".encode("utf-8"), hashlib.sha256).digest()


def _build_challenge(phone: str, code: str, ttl_seconds: int) -> str:
    now = int(time.time())
    payload_segment = _urlsafe_b64encode(
        _json_dumps(
            {
                "typ": _CHALLENGE_TYP,
                "phone": phone,
                "iat": now,
                "exp": now + ttl_seconds,
                "nonce": secrets.token_hex(8),
            }
        )
    )
    return f"{payload_segment}.{_urlsafe_b64encode(_challenge_signature(payload_segment, code))}"


def _check_challenge(challenge: str, phone: str, code: str) -> bool:
    parts = challenge.split(".")
    if len(parts) != 2:
        return False
    payload_segment, signature_segment = parts
    try:
        payload = json.loads(_urlsafe_b64decode(payload_segment).decode("utf-8"))
        provided_signature = _urlsafe_b64decode(signature_segment)
    except Exception:
        return False
    if not isinstance(payload, dict) or payload.get("typ") != _CHALLENGE_TYP:
        return False
    if payload.get("phone") != phone:
        return False
    exp = payload.get("exp")
    if not isinstance(exp, int) or exp <= int(time.time()):
        return False
    return hmac.compare_digest(provided_signature, _challenge_signature(payload_segment, code))


def _mint_verification_token(phone: str) -> str:
    now = int(time.time())
    payload_segment = _urlsafe_b64encode(
        _json_dumps(
            {
                "typ": _VERIFICATION_TYP,
                "phone": phone,
                "iat": now,
                "exp": now + _VERIFICATION_TTL_SECONDS,
            }
        )
    )
    key = get_settings().auth_secret_key.encode("utf-8")
    signature = hmac.new(key, payload_segment.encode("ascii"), hashlib.sha256).digest()
    return f"{payload_segment}.{_urlsafe_b64encode(signature)}"


def validate_verification_token(token: str, phone: str) -> bool:
    """True when ``token`` is an unexpired proof that ``phone`` passed OTP
    verification. For auth flows (login/signup/password reset) to call once
    they are wired to require phone proof."""
    normalized = normalize_phone(phone)
    parts = (token or "").split(".")
    if len(parts) != 2:
        return False
    payload_segment, signature_segment = parts
    key = get_settings().auth_secret_key.encode("utf-8")
    expected = hmac.new(key, payload_segment.encode("ascii"), hashlib.sha256).digest()
    try:
        provided = _urlsafe_b64decode(signature_segment)
        payload = json.loads(_urlsafe_b64decode(payload_segment).decode("utf-8"))
    except Exception:
        return False
    if not hmac.compare_digest(provided, expected):
        return False
    if not isinstance(payload, dict) or payload.get("typ") != _VERIFICATION_TYP:
        return False
    if payload.get("phone") != normalized:
        return False
    exp = payload.get("exp")
    return isinstance(exp, int) and exp > int(time.time())


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
        # Log status + body for operators; never log the code itself, and mask
        # the recipient phone (SEC-6: no full phone numbers in logs). The body
        # is scrubbed too — provider error payloads can echo the MSISDN.
        LOGGER.error(
            "VerifyWay OTP send failed: status=%s recipient=%s body=%s",
            resp.status_code,
            _mask_phone(recipient),
            _scrub_phone_digits(body),
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
    challenge: str = Field(..., min_length=16, description="Opaque token returned by /otp/send")


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
def register_verifyway_routes(app: FastAPI) -> None:
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
            "stateless": True,
        }

    @app.post("/api/v1/otp/send")
    async def otp_send(payload: OtpSendRequest, request: Request) -> dict[str, Any]:
        settings = get_settings()
        normalized = _normalized_or_400(payload.phone)

        # Abuse brakes (process-local sliding windows, same trade-off as the
        # login limiter): hourly caps per IP and per phone, plus a 1-per-cooldown
        # window per phone standing in for a resend cooldown.
        enforce_rate_limit(f"otp:send:ip:{_client_ip(request)}", max_events=10, window_seconds=3600)
        enforce_rate_limit(f"otp:send:phone:{normalized}", max_events=5, window_seconds=3600)
        cooldown = settings.verifyway_otp_resend_cooldown_seconds
        if cooldown > 0:
            enforce_rate_limit(f"otp:send:cooldown:{normalized}", max_events=1, window_seconds=cooldown)

        code = _generate_code(settings.verifyway_otp_length)
        await _post_otp(_to_recipient(normalized), code)

        return {
            "ok": True,
            "channel": settings.verifyway_channel,
            "expiresInSeconds": settings.verifyway_otp_ttl_seconds,
            "resendInSeconds": cooldown,
            "challenge": _build_challenge(normalized, code, settings.verifyway_otp_ttl_seconds),
        }

    @app.post("/api/v1/otp/verify")
    async def otp_verify(payload: OtpVerifyRequest, request: Request) -> dict[str, Any]:
        settings = get_settings()
        normalized = _normalized_or_400(payload.phone)

        # Brute-force brakes: the per-phone window doubles as the attempt cap
        # (max_attempts tries per code lifetime).
        enforce_rate_limit(f"otp:verify:ip:{_client_ip(request)}", max_events=30, window_seconds=3600)
        enforce_rate_limit(
            f"otp:verify:phone:{normalized}",
            max_events=settings.verifyway_otp_max_attempts,
            window_seconds=settings.verifyway_otp_ttl_seconds,
        )

        if not _check_challenge(payload.challenge, normalized, payload.code.strip()):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid or expired code. Please request a new one.",
            )

        return {
            "ok": True,
            "verified": True,
            "phone": normalized,
            "verificationToken": _mint_verification_token(normalized),
            "verificationExpiresInSeconds": _VERIFICATION_TTL_SECONDS,
        }
