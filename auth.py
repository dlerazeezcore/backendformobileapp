from __future__ import annotations

from typing import Any

from fastapi import Header, HTTPException, status
from pydantic import BaseModel, Field


class LoginPayload(BaseModel):
    phone: str
    password: str | None = None
    otp_code: str | None = Field(default=None, alias="otpCode")


class LogoutPayload(BaseModel):
    refresh_token: str | None = Field(default=None, alias="refreshToken")


class TokenResponse(BaseModel):
    access_token: str = Field(alias="accessToken")
    token_type: str = Field(default="bearer", alias="tokenType")
    expires_in: int | None = Field(default=None, alias="expiresIn")
    refresh_token: str | None = Field(default=None, alias="refreshToken")


def extract_bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    parts = authorization.strip().split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip() or None


def require_bearer_token(authorization: str | None = Header(default=None)) -> str:
    token = extract_bearer_token(authorization)
    if token is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid bearer token",
        )
    return token


def build_login_not_ready_response() -> dict[str, Any]:
    return {
        "detail": (
            "B2C authentication has a dedicated root-level auth.py now, "
            "but token issuance and verification are intentionally not wired yet."
        )
    }
