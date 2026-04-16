from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import time
from typing import Any, Callable

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from config import get_settings
from phone_utils import normalize_phone, phone_lookup_candidates
from supabase_store import AdminUser, AppUser, utcnow
from twilio_whatsapp import TwilioWhatsAppVerifyAPI


class LoginPayload(BaseModel):
    phone: str
    password: str | None = Field(default=None, min_length=8)
    otp_code: str | None = Field(default=None, alias="otpCode")

    @model_validator(mode="after")
    def validate_auth_factor(self) -> "LoginPayload":
        if self.password or self.otp_code:
            return self
        raise ValueError("password or otpCode is required")


class SignupPayload(BaseModel):
    phone: str
    name: str = Field(min_length=2, max_length=255)
    password: str | None = Field(default=None, min_length=8)
    otp_code: str | None = Field(default=None, alias="otpCode")

    @model_validator(mode="after")
    def validate_signup_factor(self) -> "SignupPayload":
        if self.password or self.otp_code:
            return self
        raise ValueError("password or otpCode is required")


class OTPRequestPayload(BaseModel):
    phone: str
    channel: str = Field(default="whatsapp")


class OTPVerifyPayload(BaseModel):
    phone: str
    code: str = Field(min_length=4, max_length=10)
    name: str | None = Field(default=None, max_length=255)


class LogoutPayload(BaseModel):
    refresh_token: str | None = Field(default=None, alias="refreshToken")


class AuthMeUpdatePayload(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    email: str | None = Field(default=None, max_length=255)


class TokenResponse(BaseModel):
    access_token: str = Field(alias="accessToken")
    token_type: str = Field(default="bearer", alias="tokenType")
    expires_in: int | None = Field(default=None, alias="expiresIn")
    refresh_token: str | None = Field(default=None, alias="refreshToken")


EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


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


async def _verify_twilio_user_otp(
    *,
    provider: TwilioWhatsAppVerifyAPI,
    phone: str,
    code: str,
) -> None:
    result = await provider.check_verification(phone=phone, code=code)
    status_value = str(result.get("status") or "").strip().lower()
    if status_value != "approved":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired OTP code.",
        )


def _issue_user_session(user_row: AppUser) -> dict[str, Any]:
    return _issue_subject_session(user_row, subject_type="user")


def _issue_subject_session(row: AppUser | AdminUser, *, subject_type: str) -> dict[str, Any]:
    token_payload = _issue_token(row, subject_type=subject_type)
    if subject_type == "admin":
        return {
            **token_payload,
            "adminUserId": row.id,
            "id": row.id,
            "phone": row.phone,
            "name": row.name,
            "subjectType": "admin",
            "isAdmin": True,
        }
    return {
        **token_payload,
        "userId": row.id,
        "id": row.id,
        "phone": row.phone,
        "name": row.name,
        "subjectType": "user",
        "isAdmin": False,
    }


def register_auth_routes(
    app: FastAPI,
    get_db: Callable[..., Any],
    get_twilio_provider: Callable[..., Any],
) -> None:
    def _get_optional_twilio_provider(request: Request) -> TwilioWhatsAppVerifyAPI | None:
        return getattr(request.app.state, "twilio_whatsapp_api", None)

    def _require_twilio_provider(provider: TwilioWhatsAppVerifyAPI | None) -> TwilioWhatsAppVerifyAPI:
        if provider is not None:
            return provider
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Twilio WhatsApp OTP service is not configured on this deployment.",
        )

    @app.post("/api/v1/auth/user/otp/request")
    async def request_user_otp(
        payload: OTPRequestPayload,
        provider: TwilioWhatsAppVerifyAPI = Depends(get_twilio_provider),
    ) -> dict[str, Any]:
        normalized_phone = _normalize_and_validate_signup_phone(payload.phone)
        channel = str(payload.channel or "whatsapp").strip().lower() or "whatsapp"
        if channel not in {"whatsapp", "sms"}:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Unsupported OTP channel. Allowed channels: whatsapp, sms.",
            )
        result = await provider.start_verification(phone=normalized_phone, channel=channel)
        return {
            "success": True,
            "data": {
                "to": normalized_phone,
                "channel": channel,
                "status": str(result.get("status") or "pending"),
                "sid": result.get("sid"),
            },
        }

    @app.post("/api/v1/auth/user/otp/verify")
    async def verify_user_otp(
        payload: OTPVerifyPayload,
        db: Session = Depends(get_db),
        provider: TwilioWhatsAppVerifyAPI = Depends(get_twilio_provider),
    ) -> dict[str, Any]:
        normalized_phone = _normalize_and_validate_signup_phone(payload.phone)
        await _verify_twilio_user_otp(provider=provider, phone=normalized_phone, code=payload.code)

        user_row = _lookup_user_by_phone(db, normalized_phone)
        if user_row is None:
            existing_admin = _lookup_admin_by_phone(db, normalized_phone)
            if existing_admin is not None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="This phone is already used by an admin account.",
                )
            normalized_name = str(payload.name or "").strip()
            if len(normalized_name) < 2:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="Name must be at least 2 characters for first-time signup.",
                )
            user_row = AppUser(
                phone=normalized_phone,
                name=normalized_name,
                status="active",
                password_hash=None,
                last_login_at=utcnow(),
            )
            db.add(user_row)
            try:
                db.commit()
            except IntegrityError as exc:
                db.rollback()
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="User account already exists for this phone. Please try again.",
                ) from exc
            db.refresh(user_row)
            return _issue_user_session(user_row)

        if not _is_row_active(user_row):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="User account exists but is not active. Please contact support.",
            )
        user_row.last_login_at = utcnow()
        db.commit()
        return _issue_user_session(user_row)

    @app.post("/api/v1/auth/admin/login")
    async def admin_login(payload: LoginPayload, db: Session = Depends(get_db)) -> dict[str, Any]:
        row = _lookup_admin_by_phone(db, payload.phone)
        if payload.password is None:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="password is required")
        if row is None or not _is_row_active(row) or not verify_password(payload.password, row.password_hash):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid phone or password")
        row.last_login_at = utcnow()
        db.commit()
        return _issue_subject_session(row, subject_type="admin")

    @app.post("/api/v1/auth/user/login")
    async def user_login(
        payload: LoginPayload,
        db: Session = Depends(get_db),
        provider: TwilioWhatsAppVerifyAPI | None = Depends(_get_optional_twilio_provider),
    ) -> dict[str, Any]:
        normalized_phone = _normalize_and_validate_signup_phone(payload.phone)
        if payload.otp_code:
            required_provider = _require_twilio_provider(provider)
            await _verify_twilio_user_otp(provider=required_provider, phone=normalized_phone, code=payload.otp_code)
            user_row = _lookup_user_by_phone(db, normalized_phone)
            if user_row is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="User account not found. Please sign up first.",
                )
            if not _is_row_active(user_row):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="User account exists but is not active. Please contact support.",
                )
            user_row.last_login_at = utcnow()
            db.commit()
            return _issue_subject_session(user_row, subject_type="user")

        if payload.password is None:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="password or otpCode is required")

        user_row = _lookup_user_by_phone(db, normalized_phone)
        if user_row is not None and _is_row_active(user_row) and verify_password(payload.password, user_row.password_hash):
            user_row.last_login_at = utcnow()
            db.commit()
            return _issue_subject_session(user_row, subject_type="user")

        # Compatibility path for frontends using a single login endpoint.
        admin_row = _lookup_admin_by_phone(db, normalized_phone)
        if admin_row is not None and _is_row_active(admin_row) and verify_password(payload.password, admin_row.password_hash):
            admin_row.last_login_at = utcnow()
            db.commit()
            return _issue_subject_session(admin_row, subject_type="admin")

        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid phone or password")

    @app.post("/api/v1/auth/user/register")
    @app.post("/api/v1/auth/user/signup")
    async def user_signup(
        payload: SignupPayload,
        db: Session = Depends(get_db),
        provider: TwilioWhatsAppVerifyAPI | None = Depends(_get_optional_twilio_provider),
    ) -> dict[str, Any]:
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

        if payload.otp_code:
            required_provider = _require_twilio_provider(provider)
            await _verify_twilio_user_otp(provider=required_provider, phone=normalized_phone, code=payload.otp_code)
        elif payload.password is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="password or otpCode is required",
            )

        user_row = AppUser(
            phone=normalized_phone,
            name=normalized_name,
            status="active",
            password_hash=hash_password(payload.password) if payload.password else None,
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

        return _issue_user_session(user_row)

    @app.get("/api/v1/auth/me")
    @app.get("/auth/me")
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
                "email": row.email,
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
            "email": row.email,
            "status": row.status,
            "isLoyalty": row.is_loyalty,
        }

    @app.patch("/api/v1/auth/me")
    @app.patch("/auth/me")
    async def update_auth_me(
        payload: AuthMeUpdatePayload,
        claims: dict[str, Any] = Depends(get_token_claims),
        db: Session = Depends(get_db),
    ) -> dict[str, Any]:
        row = require_active_subject(db, claims=claims)
        provided_fields = set(payload.model_fields_set)
        if not provided_fields:
            raise _api_error(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "AUTH_INVALID_PROFILE_PATCH",
                "At least one profile field is required",
            )

        if "name" in provided_fields:
            normalized_name = str(payload.name or "").strip()
            if len(normalized_name) < 2:
                raise _api_error(
                    status.HTTP_422_UNPROCESSABLE_CONTENT,
                    "AUTH_INVALID_NAME",
                    "Name must be at least 2 characters",
                )
            row.name = normalized_name

        if "email" in provided_fields:
            normalized_email = str(payload.email or "").strip().lower() if payload.email is not None else None
            if normalized_email == "":
                normalized_email = None
            if normalized_email is not None and not EMAIL_PATTERN.fullmatch(normalized_email):
                raise _api_error(
                    status.HTTP_422_UNPROCESSABLE_CONTENT,
                    "AUTH_INVALID_EMAIL",
                    "Invalid email format",
                )

            if normalized_email is not None:
                existing_user = db.scalar(select(AppUser).where(func.lower(AppUser.email) == normalized_email))
                if existing_user is not None and (not isinstance(row, AppUser) or existing_user.id != row.id):
                    raise _api_error(
                        status.HTTP_409_CONFLICT,
                        "AUTH_EMAIL_CONFLICT",
                        "Email already in use",
                    )
                existing_admin = db.scalar(select(AdminUser).where(func.lower(AdminUser.email) == normalized_email))
                if existing_admin is not None and (not isinstance(row, AdminUser) or existing_admin.id != row.id):
                    raise _api_error(
                        status.HTTP_409_CONFLICT,
                        "AUTH_EMAIL_CONFLICT",
                        "Email already in use",
                    )
            row.email = normalized_email

        row.updated_at = utcnow()
        db.commit()
        db.refresh(row)

        if isinstance(row, AdminUser):
            return {
                "subjectType": "admin",
                "id": row.id,
                "adminUserId": row.id,
                "phone": row.phone,
                "name": row.name,
                "email": row.email,
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
        return {
            "subjectType": "user",
            "id": row.id,
            "userId": row.id,
            "phone": row.phone,
            "name": row.name,
            "email": row.email,
            "status": row.status,
            "isLoyalty": row.is_loyalty,
        }

    @app.delete("/api/v1/auth/me")
    @app.delete("/auth/me")
    @app.post("/api/v1/auth/user/delete")
    @app.post("/auth/user/delete")
    async def delete_authenticated_user(
        claims: dict[str, Any] = Depends(get_token_claims),
        db: Session = Depends(get_db),
    ) -> dict[str, Any]:
        token_subject_type = claims.get("typ")
        subject_id = claims.get("sub")
        if not isinstance(subject_id, str):
            raise _api_error(status.HTTP_401_UNAUTHORIZED, "AUTH_INVALID_SUBJECT", "Invalid auth subject")
        if token_subject_type == "user":
            user_row = db.scalar(select(AppUser).where(AppUser.id == subject_id))
            if user_row is None:
                raise _api_error(status.HTTP_401_UNAUTHORIZED, "AUTH_SUBJECT_NOT_FOUND", "Auth subject not found")
            if user_row.blocked_at is not None:
                raise _api_error(status.HTTP_403_FORBIDDEN, "AUTH_SUBJECT_INACTIVE", "Inactive account")
            if user_row.deleted_at is not None or user_row.status == "deleted":
                return {
                    "deleted": True,
                    "id": user_row.id,
                    "userId": user_row.id,
                    "status": "deleted",
                    "deletedAt": user_row.deleted_at,
                    "subjectType": "user",
                }
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
                "subjectType": "user",
            }

        if token_subject_type == "admin":
            admin_row = db.scalar(select(AdminUser).where(AdminUser.id == subject_id))
            if admin_row is None:
                raise _api_error(status.HTTP_401_UNAUTHORIZED, "AUTH_SUBJECT_NOT_FOUND", "Auth subject not found")
            if admin_row.blocked_at is not None:
                raise _api_error(status.HTTP_403_FORBIDDEN, "AUTH_SUBJECT_INACTIVE", "Inactive account")
            if admin_row.deleted_at is not None or admin_row.status == "deleted":
                return {
                    "deleted": True,
                    "id": admin_row.id,
                    "adminUserId": admin_row.id,
                    "status": "deleted",
                    "deletedAt": admin_row.deleted_at,
                    "subjectType": "admin",
                }
            admin_row.status = "deleted"
            admin_row.deleted_at = utcnow()
            admin_row.updated_at = utcnow()
            db.commit()
            return {
                "deleted": True,
                "id": admin_row.id,
                "adminUserId": admin_row.id,
                "status": admin_row.status,
                "deletedAt": admin_row.deleted_at,
                "subjectType": "admin",
            }

        raise _api_error(
            status.HTTP_403_FORBIDDEN,
            "AUTH_SCOPE_FORBIDDEN",
            "Token subject is not allowed for this endpoint",
        )
