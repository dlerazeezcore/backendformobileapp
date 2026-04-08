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
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from config import get_settings
from phone_utils import normalize_phone, phone_lookup_candidates
from supabase_store import AdminUser, AppUser, utcnow


class LoginPayload(BaseModel):
    phone: str
    password: str = Field(min_length=8)
    otp_code: str | None = Field(default=None, alias="otpCode")


class SignupPayload(BaseModel):
    phone: str
    name: str = Field(min_length=2, max_length=255)
    password: str = Field(min_length=8)


class LogoutPayload(BaseModel):
    refresh_token: str | None = Field(default=None, alias="refreshToken")


class TokenResponse(BaseModel):
    access_token: str = Field(alias="accessToken")
    token_type: str = Field(default="bearer", alias="tokenType")
    expires_in: int | None = Field(default=None, alias="expiresIn")
    refresh_token: str | None = Field(default=None, alias="refreshToken")


def _api_error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={
            "code": code,
            "message": message,
        },
    )


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
        raise _api_error(status.HTTP_401_UNAUTHORIZED, "AUTH_INVALID_TOKEN", "Invalid token")
    header_segment, payload_segment, signature_segment = parts
    signing_input = f"{header_segment}.{payload_segment}".encode("ascii")
    expected_signature = hmac.new(secret_key.encode("utf-8"), signing_input, hashlib.sha256).digest()
    try:
        provided_signature = _urlsafe_b64decode(signature_segment)
    except Exception as exc:
        raise _api_error(status.HTTP_401_UNAUTHORIZED, "AUTH_INVALID_TOKEN_SIGNATURE", "Invalid token signature") from exc
    if not hmac.compare_digest(provided_signature, expected_signature):
        raise _api_error(status.HTTP_401_UNAUTHORIZED, "AUTH_INVALID_TOKEN_SIGNATURE", "Invalid token signature")
    try:
        payload = json.loads(_urlsafe_b64decode(payload_segment).decode("utf-8"))
    except Exception as exc:
        raise _api_error(status.HTTP_401_UNAUTHORIZED, "AUTH_INVALID_TOKEN_PAYLOAD", "Invalid token payload") from exc
    if not isinstance(payload, dict):
        raise _api_error(status.HTTP_401_UNAUTHORIZED, "AUTH_INVALID_TOKEN_PAYLOAD", "Invalid token payload")
    exp = payload.get("exp")
    if not isinstance(exp, int) or exp <= int(time.time()):
        raise _api_error(status.HTTP_401_UNAUTHORIZED, "AUTH_TOKEN_EXPIRED", "Token expired")
    if payload.get("typ") not in {"admin", "user"}:
        raise _api_error(status.HTTP_401_UNAUTHORIZED, "AUTH_INVALID_TOKEN_TYPE", "Invalid token type")
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
        raise _api_error(
            status.HTTP_401_UNAUTHORIZED,
            "AUTH_MISSING_BEARER_TOKEN",
            "Missing or invalid bearer token",
        )
    return token


def get_token_claims(token: str = Depends(require_bearer_token)) -> dict[str, Any]:
    settings = get_settings()
    return decode_access_token(token, secret_key=settings.auth_secret_key)


def _is_row_active(row: AppUser | AdminUser) -> bool:
    return row.status == "active" and row.deleted_at is None and row.blocked_at is None


def require_active_subject(
    db: Session,
    *,
    claims: dict[str, Any],
    subject_type: str | None = None,
) -> AppUser | AdminUser:
    token_subject_type = claims.get("typ")
    subject_id = claims.get("sub")
    if subject_type is not None and token_subject_type != subject_type:
        raise _api_error(
            status.HTTP_403_FORBIDDEN,
            "AUTH_SCOPE_FORBIDDEN",
            "Token subject is not allowed for this endpoint",
        )
    if not isinstance(subject_id, str) or not isinstance(token_subject_type, str):
        raise _api_error(status.HTTP_401_UNAUTHORIZED, "AUTH_INVALID_SUBJECT", "Invalid auth subject")
    if token_subject_type == "admin":
        row = db.scalar(select(AdminUser).where(AdminUser.id == subject_id))
    elif token_subject_type == "user":
        row = db.scalar(select(AppUser).where(AppUser.id == subject_id))
    else:
        raise _api_error(status.HTTP_401_UNAUTHORIZED, "AUTH_INVALID_TOKEN_TYPE", "Invalid auth token type")
    if row is None:
        raise _api_error(status.HTTP_401_UNAUTHORIZED, "AUTH_SUBJECT_NOT_FOUND", "Auth subject not found")
    if not _is_row_active(row):
        raise _api_error(status.HTTP_403_FORBIDDEN, "AUTH_SUBJECT_INACTIVE", "Inactive account")
    return row


def _lookup_admin_by_phone(db: Session, phone: str) -> AdminUser | None:
    candidates = phone_lookup_candidates(phone)
    if not candidates:
        return None
    return db.scalar(select(AdminUser).where(AdminUser.phone.in_(candidates)))


def _lookup_user_by_phone(db: Session, phone: str) -> AppUser | None:
    candidates = phone_lookup_candidates(phone)
    if not candidates:
        return None
    return db.scalar(select(AppUser).where(AppUser.phone.in_(candidates)))


def _issue_token(row: AppUser | AdminUser, *, subject_type: str) -> dict[str, Any]:
    settings = get_settings()
    token = create_access_token(
        subject_id=row.id,
        phone=row.phone,
        subject_type=subject_type,
        secret_key=settings.auth_secret_key,
        ttl_seconds=settings.auth_token_ttl_seconds,
    )
    response = TokenResponse(accessToken=token, expiresIn=settings.auth_token_ttl_seconds)
    return response.model_dump(by_alias=True, exclude_none=True)


def _normalize_and_validate_signup_phone(phone: str) -> str:
    normalized = normalize_phone(phone)
    if not normalized.startswith("+"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Phone must be in international format (for example +9647xxxxxxxxx).",
        )
    digits = normalized[1:]
    if not digits.isdigit() or len(digits) < 8:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Phone number format is invalid.",
        )
    return normalized


def register_auth_routes(app: FastAPI, get_db: Callable[..., Any]) -> None:
    @app.post("/api/v1/auth/admin/login")
    async def admin_login(payload: LoginPayload, db: Session = Depends(get_db)) -> dict[str, Any]:
        row = _lookup_admin_by_phone(db, payload.phone)
        if row is None or not _is_row_active(row) or not verify_password(payload.password, row.password_hash):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid phone or password")
        row.last_login_at = utcnow()
        db.commit()
        return _issue_token(row, subject_type="admin")

    @app.post("/api/v1/auth/user/login")
    async def user_login(payload: LoginPayload, db: Session = Depends(get_db)) -> dict[str, Any]:
        user_row = _lookup_user_by_phone(db, payload.phone)
        if user_row is not None and _is_row_active(user_row) and verify_password(payload.password, user_row.password_hash):
            user_row.last_login_at = utcnow()
            db.commit()
            return _issue_token(user_row, subject_type="user")

        # Compatibility path for frontends using a single login endpoint.
        admin_row = _lookup_admin_by_phone(db, payload.phone)
        if admin_row is not None and _is_row_active(admin_row) and verify_password(payload.password, admin_row.password_hash):
            admin_row.last_login_at = utcnow()
            db.commit()
            return _issue_token(admin_row, subject_type="admin")

        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid phone or password")

    @app.post("/api/v1/auth/user/register")
    @app.post("/api/v1/auth/user/signup")
    async def user_signup(payload: SignupPayload, db: Session = Depends(get_db)) -> dict[str, Any]:
        normalized_phone = _normalize_and_validate_signup_phone(payload.phone)
        normalized_name = str(payload.name or "").strip()
        if len(normalized_name) < 2:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Name must be at least 2 characters.",
            )

        existing_user = _lookup_user_by_phone(db, normalized_phone)
        if existing_user is not None:
            if _is_row_active(existing_user):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="User account already exists for this phone. Please log in.",
                )
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="User account exists but is not active. Please contact support.",
            )

        existing_admin = _lookup_admin_by_phone(db, normalized_phone)
        if existing_admin is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This phone is already used by an admin account.",
            )

        user_row = AppUser(
            phone=normalized_phone,
            name=normalized_name,
            status="active",
            password_hash=hash_password(payload.password),
            last_login_at=utcnow(),
        )
        db.add(user_row)
        try:
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="User account already exists for this phone. Please log in.",
            ) from exc
        db.refresh(user_row)

        token_payload = _issue_token(user_row, subject_type="user")
        return {
            **token_payload,
            "userId": user_row.id,
            "id": user_row.id,
            "phone": user_row.phone,
            "name": user_row.name,
            "subjectType": "user",
        }

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
                raise _api_error(status.HTTP_401_UNAUTHORIZED, "AUTH_SUBJECT_NOT_FOUND", "Auth subject not found")
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
            raise _api_error(status.HTTP_401_UNAUTHORIZED, "AUTH_SUBJECT_NOT_FOUND", "Auth subject not found")
        return {
            "subjectType": "user",
            "id": row.id,
            "phone": row.phone,
            "name": row.name,
            "status": row.status,
            "isLoyalty": row.is_loyalty,
        }

    @app.delete("/api/v1/auth/me")
    @app.post("/api/v1/auth/user/delete")
    async def delete_authenticated_user(
        claims: dict[str, Any] = Depends(get_token_claims),
        db: Session = Depends(get_db),
    ) -> dict[str, Any]:
        user_row = require_active_subject(db, claims=claims, subject_type="user")
        assert isinstance(user_row, AppUser)
        user_row.status = "deleted"
        user_row.deleted_at = utcnow()
        user_row.updated_at = utcnow()
        db.commit()
        return {
            "deleted": True,
            "id": user_row.id,
            "userId": user_row.id,
            "status": user_row.status,
            "deletedAt": user_row.deleted_at,
        }
