from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from typing import Any, Callable

from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from config import get_settings
from supabase_store import AdminUser, AppUser, utcnow


class LoginPayload(BaseModel):
    phone: str
    password: str = Field(min_length=8)
    otp_code: str | None = Field(default=None, alias="otpCode")


class LogoutPayload(BaseModel):
    refresh_token: str | None = Field(default=None, alias="refreshToken")


class TokenResponse(BaseModel):
    access_token: str = Field(alias="accessToken")
    token_type: str = Field(default="bearer", alias="tokenType")
    expires_in: int | None = Field(default=None, alias="expiresIn")
    refresh_token: str | None = Field(default=None, alias="refreshToken")


def _urlsafe_b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _urlsafe_b64decode(raw: str) -> bytes:
    padding = "=" * (-len(raw) % 4)
    return base64.urlsafe_b64decode((raw + padding).encode("ascii"))


def _json_dumps(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt$16384$8$1${_urlsafe_b64encode(salt)}${_urlsafe_b64encode(digest)}"


def verify_password(password: str, encoded_hash: str | None) -> bool:
    if not encoded_hash:
        return False
    parts = encoded_hash.split("$")
    if len(parts) != 6 or parts[0] != "scrypt":
        return False
    _, n_str, r_str, p_str, salt_b64, digest_b64 = parts
    try:
        n = int(n_str)
        r = int(r_str)
        p = int(p_str)
        salt = _urlsafe_b64decode(salt_b64)
        expected = _urlsafe_b64decode(digest_b64)
    except Exception:
        return False
    actual = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=len(expected))
    return hmac.compare_digest(actual, expected)


def create_access_token(
    *,
    subject_id: str,
    phone: str,
    subject_type: str,
    secret_key: str,
    ttl_seconds: int,
) -> str:
    now = int(time.time())
    payload = {
        "sub": subject_id,
        "phone": phone,
        "typ": subject_type,
        "iat": now,
        "exp": now + ttl_seconds,
    }
    header_segment = _urlsafe_b64encode(_json_dumps({"alg": "HS256", "typ": "JWT"}))
    payload_segment = _urlsafe_b64encode(_json_dumps(payload))
    signing_input = f"{header_segment}.{payload_segment}".encode("ascii")
    signature = hmac.new(secret_key.encode("utf-8"), signing_input, hashlib.sha256).digest()
    return f"{header_segment}.{payload_segment}.{_urlsafe_b64encode(signature)}"


def decode_access_token(token: str, *, secret_key: str) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) != 3:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    header_segment, payload_segment, signature_segment = parts
    signing_input = f"{header_segment}.{payload_segment}".encode("ascii")
    expected_signature = hmac.new(secret_key.encode("utf-8"), signing_input, hashlib.sha256).digest()
    try:
        provided_signature = _urlsafe_b64decode(signature_segment)
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token signature") from exc
    if not hmac.compare_digest(provided_signature, expected_signature):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token signature")
    try:
        payload = json.loads(_urlsafe_b64decode(payload_segment).decode("utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token payload") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token payload")
    exp = payload.get("exp")
    if not isinstance(exp, int) or exp <= int(time.time()):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token expired")
    if payload.get("typ") not in {"admin", "user"}:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token type")
    return payload


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


def _is_row_active(row: AppUser | AdminUser) -> bool:
    return row.status == "active" and row.deleted_at is None and row.blocked_at is None


def register_auth_routes(app: FastAPI, get_db: Callable[..., Any]) -> None:
    @app.post("/api/v1/auth/admin/login")
    async def admin_login(payload: LoginPayload, db: Session = Depends(get_db)) -> dict[str, Any]:
        settings = get_settings()
        row = db.scalar(select(AdminUser).where(AdminUser.phone == payload.phone))
        if row is None or not _is_row_active(row) or not verify_password(payload.password, row.password_hash):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid phone or password")
        row.last_login_at = utcnow()
        db.commit()
        token = create_access_token(
            subject_id=row.id,
            phone=row.phone,
            subject_type="admin",
            secret_key=settings.auth_secret_key,
            ttl_seconds=settings.auth_token_ttl_seconds,
        )
        response = TokenResponse(accessToken=token, expiresIn=settings.auth_token_ttl_seconds)
        return response.model_dump(by_alias=True, exclude_none=True)

    @app.post("/api/v1/auth/user/login")
    async def user_login(payload: LoginPayload, db: Session = Depends(get_db)) -> dict[str, Any]:
        settings = get_settings()
        row = db.scalar(select(AppUser).where(AppUser.phone == payload.phone))
        if row is None or not _is_row_active(row) or not verify_password(payload.password, row.password_hash):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid phone or password")
        row.last_login_at = utcnow()
        db.commit()
        token = create_access_token(
            subject_id=row.id,
            phone=row.phone,
            subject_type="user",
            secret_key=settings.auth_secret_key,
            ttl_seconds=settings.auth_token_ttl_seconds,
        )
        response = TokenResponse(accessToken=token, expiresIn=settings.auth_token_ttl_seconds)
        return response.model_dump(by_alias=True, exclude_none=True)

    @app.get("/api/v1/auth/me")
    async def auth_me(
        token: str = Depends(require_bearer_token),
        db: Session = Depends(get_db),
    ) -> dict[str, Any]:
        settings = get_settings()
        claims = decode_access_token(token, secret_key=settings.auth_secret_key)
        subject_type = claims["typ"]
        subject_id = claims["sub"]
        if subject_type == "admin":
            row = db.scalar(select(AdminUser).where(AdminUser.id == subject_id))
            if row is None:
                raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Auth subject not found")
            return {
                "subjectType": "admin",
                "id": row.id,
                "phone": row.phone,
                "name": row.name,
                "status": row.status,
                "role": row.role,
                "permissions": {
                    "canManageUsers": row.can_manage_users,
                    "canManageOrders": row.can_manage_orders,
                    "canManagePricing": row.can_manage_pricing,
                    "canManageContent": row.can_manage_content,
                    "canSendPush": row.can_send_push,
                },
            }
        row = db.scalar(select(AppUser).where(AppUser.id == subject_id))
        if row is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Auth subject not found")
        return {
            "subjectType": "user",
            "id": row.id,
            "phone": row.phone,
            "name": row.name,
            "status": row.status,
            "isLoyalty": row.is_loyalty,
        }
