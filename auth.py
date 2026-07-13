from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import time
from datetime import datetime
from typing import Any, Callable

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value

from config import get_settings, read_float_env as _read_float_env
from phone_utils import normalize_phone, phone_lookup_candidates
from rate_limit import enforce_rate_limit
from supabase_store import AdminUser, AppUser, utcnow
from verifyway import validate_verification_token


class LoginPayload(BaseModel):
    phone: str | None = None
    email: str | None = None
    password: str | None = Field(default=None, min_length=8)

    @model_validator(mode="after")
    def validate_auth_factor(self) -> "LoginPayload":
        has_phone = bool(self.phone and self.phone.strip())
        has_email = bool(self.email and self.email.strip())
        if not has_phone and not has_email:
            raise ValueError("phone or email is required")
        # Phone is the default identifier; email is the alternative.
        if self.password:
            return self
        raise ValueError("password is required")


class SignupPayload(BaseModel):
    phone: str
    name: str = Field(min_length=2, max_length=255)
    # max_length bounds the scrypt input so an oversized payload can't be used
    # to burn CPU on password hashing.
    password: str = Field(min_length=8, max_length=128)
    # Proof of WhatsApp OTP ownership of `phone`, minted by POST /otp/verify.
    # Signup requires a verified phone (product rule: sign up with password + OTP).
    verification_token: str = Field(alias="verificationToken")

    model_config = {"populate_by_name": True}


class OtpLoginPayload(BaseModel):
    """Passwordless login for an existing account, proven by a WhatsApp OTP."""
    phone: str
    verification_token: str = Field(alias="verificationToken")

    model_config = {"populate_by_name": True}


class ResetPasswordPayload(BaseModel):
    """Set a new password using a WhatsApp OTP proof (no old password needed)."""
    phone: str
    verification_token: str = Field(alias="verificationToken")
    new_password: str = Field(min_length=8, max_length=128, alias="newPassword")

    model_config = {"populate_by_name": True}


class LogoutPayload(BaseModel):
    refresh_token: str | None = Field(default=None, alias="refreshToken")


class AuthMeUpdatePayload(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    email: str | None = Field(default=None, max_length=255)
    # Audit #11: self-service password change lives here now (it used to ride
    # on the retired non-admin branch of POST /admin/users). AppUser only,
    # same minimum length as signup. SEC: changing the password requires the
    # current one, so a leaked bearer token alone can't re-key the account.
    password: str | None = Field(default=None, min_length=8, max_length=128)
    current_password: str | None = Field(default=None, max_length=128, alias="currentPassword")
    preferred_language: str | None = Field(default=None, max_length=8, alias="preferredLanguage")
    preferred_currency: str | None = Field(default=None, max_length=8, alias="preferredCurrency")
    notifications_enabled: bool | None = Field(default=None, alias="notificationsEnabled")

    model_config = {"populate_by_name": True}


class TokenResponse(BaseModel):
    access_token: str = Field(alias="accessToken")
    token_type: str = Field(default="bearer", alias="tokenType")
    expires_in: int | None = Field(default=None, alias="expiresIn")
    refresh_token: str | None = Field(default=None, alias="refreshToken")


EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
LOGGER = logging.getLogger("uvicorn.error")


def _api_error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={
            "code": code,
            "message": message,
        },
    )


async def _run_login_db_worker(worker: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    timeout_seconds = _read_float_env("AUTH_DB_TIMEOUT_SECONDS", 4.0, minimum=0.5)
    try:
        return await asyncio.wait_for(asyncio.to_thread(worker, *args, **kwargs), timeout=timeout_seconds)
    except asyncio.TimeoutError as exc:
        raise _api_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "AUTH_DB_TIMEOUT",
            "Login database check timed out. Please retry in a few seconds.",
        ) from exc


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
    # SEC-10: pin the JWT header before doing anything else. We only ever mint
    # HS256/JWT tokens, so reject any other alg (defends against alg-confusion /
    # `alg:none` forgeries up front instead of relying solely on the HMAC check).
    try:
        header = json.loads(_urlsafe_b64decode(header_segment).decode("utf-8"))
    except Exception as exc:
        raise _api_error(status.HTTP_401_UNAUTHORIZED, "AUTH_INVALID_TOKEN_HEADER", "Invalid token header") from exc
    if not isinstance(header, dict) or header.get("alg") != "HS256" or header.get("typ") != "JWT":
        raise _api_error(status.HTTP_401_UNAUTHORIZED, "AUTH_INVALID_TOKEN_HEADER", "Unsupported token header")
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


def _stamp_app_version(db: Session, row: AppUser | AdminUser, reported_version: str | None) -> None:
    """Record the app build a user is running (from the ``X-App-Version`` header).

    AppUser only; writes ONLY when the value changed, so the common case stays
    read-only. Called from ``/auth/me`` (fires on every launch) and from
    ``require_active_subject`` (any authenticated user request), so the admin
    panel's per-user version column fills in from normal app usage.

    Audit #10: the stamp runs as an isolated UPDATE in its own short-lived
    session on the same bind. Committing on the request session here would
    also commit any unrelated pending state a handler accumulated mid-request
    (these are otherwise read-only endpoints).
    """
    if not isinstance(row, AppUser):
        return
    reported = (reported_version or "").strip()[:32]
    if not reported or reported == (row.app_version or ""):
        return
    stamped_at = utcnow()
    with Session(bind=db.get_bind()) as stamp_db:
        stamp_db.execute(
            update(AppUser)
            .where(AppUser.id == row.id)
            .values(app_version=reported, app_version_updated_at=stamped_at)
        )
        stamp_db.commit()
    # Mirror the persisted values onto the already-loaded row WITHOUT dirtying
    # the request session (set_committed_value == "as if freshly loaded").
    set_committed_value(row, "app_version", reported)
    set_committed_value(row, "app_version_updated_at", stamped_at)


def require_active_subject(
    db: Session,
    *,
    claims: dict[str, Any],
    subject_type: str | None = None,
    app_version: str | None = None,
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
    if app_version is not None:
        _stamp_app_version(db, row, app_version)
    return row


def _digits_only(value: str) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def _compact_phone_expression(column: Any) -> Any:
    compact = column
    for token in ("+", " ", "-", "(", ")", "\t", "\r", "\n"):
        compact = func.replace(compact, token, "")
    return compact


def _lookup_row_by_compact_phone(db: Session, model: type[AppUser] | type[AdminUser], candidates: list[str]) -> Any | None:
    compact_candidates = sorted({_digits_only(candidate) for candidate in candidates if _digits_only(candidate)})
    if not compact_candidates:
        return None
    return db.scalar(
        select(model)
        .where(_compact_phone_expression(model.phone).in_(compact_candidates))
        .limit(1)
    )


def _lookup_admin_by_phone(db: Session, phone: str) -> AdminUser | None:
    candidates = phone_lookup_candidates(phone)
    if candidates:
        row = db.scalar(select(AdminUser).where(AdminUser.phone.in_(candidates)).limit(1))
        if row is not None:
            return row
    canonical = normalize_phone(phone)
    if not canonical:
        return None
    row = _lookup_row_by_compact_phone(db, AdminUser, candidates)
    if row is not None:
        LOGGER.debug(
            "auth.lookup.admin fallback-compact-match canonical=%s stored=%s",
            _mask_phone(canonical),
            _mask_phone(row.phone),
        )
    return row


def _lookup_user_by_phone(db: Session, phone: str) -> AppUser | None:
    candidates = phone_lookup_candidates(phone)
    if candidates:
        row = db.scalar(select(AppUser).where(AppUser.phone.in_(candidates)).limit(1))
        if row is not None:
            return row
    canonical = normalize_phone(phone)
    if not canonical:
        return None
    row = _lookup_row_by_compact_phone(db, AppUser, candidates)
    if row is not None:
        LOGGER.debug(
            "auth.lookup.user fallback-compact-match canonical=%s stored=%s",
            _mask_phone(canonical),
            _mask_phone(row.phone),
        )
    return row


def _lookup_user_by_email(db: Session, email: str) -> AppUser | None:
    normalized = str(email or "").strip().lower()
    if not normalized:
        return None
    return db.scalar(select(AppUser).where(func.lower(AppUser.email) == normalized).limit(1))


def _lookup_admin_by_email(db: Session, email: str) -> AdminUser | None:
    normalized = str(email or "").strip().lower()
    if not normalized:
        return None
    return db.scalar(select(AdminUser).where(func.lower(AdminUser.email) == normalized).limit(1))


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


def _mask_phone(value: str | None) -> str:
    """Mask a phone number for logging — keep only the last 3 digits (SEC-6)."""
    digits = "".join(ch for ch in (value or "") if ch.isdigit())
    if len(digits) <= 3:
        return "***"
    return f"***{digits[-3:]}"


def _log_phone_lookup(context: str, raw_phone: str, normalized_phone: str) -> None:
    # SEC-6: never log full phone numbers (PII). Mask to the last 3 digits and
    # drop the raw value + candidate list entirely.
    _ = raw_phone
    LOGGER.info("%s phone lookup phone=%s", context, _mask_phone(normalized_phone))


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
            "email": row.email,
            "subjectType": "admin",
            "isAdmin": True,
            "createdAt": row.created_at,
        }
    return {
        **token_payload,
        "userId": row.id,
        "id": row.id,
        "phone": row.phone,
        "name": row.name,
        "email": row.email,
        "subjectType": "user",
        "isAdmin": False,
        "isLoyalty": bool(getattr(row, "is_loyalty", False)),
        "createdAt": row.created_at,
    }


def _last_login_touch_due(previous: datetime | None, now: datetime) -> bool:
    interval_seconds = _read_float_env("AUTH_LAST_LOGIN_TOUCH_INTERVAL_SECONDS", 3600.0, minimum=0.0)
    if previous is None:
        return True
    if previous.tzinfo is None and now.tzinfo is not None:
        previous = previous.replace(tzinfo=now.tzinfo)
    try:
        return (now - previous).total_seconds() >= interval_seconds
    except TypeError:
        return True


def _touch_last_login_if_due(db: Session, row: AppUser | AdminUser) -> None:
    now = utcnow()
    if not _last_login_touch_due(row.last_login_at, now):
        return
    row.last_login_at = now
    db.commit()


def _lookup_subject_by_phone(db: Session, phone: str) -> tuple[AppUser | AdminUser | None, str]:
    """Find an account by phone, preferring an AppUser then an AdminUser.

    This mirrors the dual lookup password login uses (`_login_subject_with_password`)
    so OTP login and password reset work for admin accounts too — admins
    authenticate through the same `/auth/user/*` endpoints. Returns (row, subject_type);
    (None, "") when no account matches."""
    user_row = _lookup_user_by_phone(db, phone)
    if user_row is not None:
        return user_row, "user"
    admin_row = _lookup_admin_by_phone(db, phone)
    if admin_row is not None:
        return admin_row, "admin"
    return None, ""


def _login_subject_with_password(
    session_factory: Callable[[], Session],
    *,
    phone: str | None = None,
    email: str | None = None,
    password: str,
) -> dict[str, Any]:
    db = session_factory()
    try:
        if email:
            user_row = _lookup_user_by_email(db, email)
        else:
            user_row = _lookup_user_by_phone(db, phone)
        if user_row is not None:
            if not _is_row_active(user_row):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="User account exists but is not active. Please contact support.",
                )
            if verify_password(password, user_row.password_hash):
                _touch_last_login_if_due(db, user_row)
                return _issue_subject_session(user_row, subject_type="user")
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

        admin_row = _lookup_admin_by_email(db, email) if email else _lookup_admin_by_phone(db, phone)
        if admin_row is not None:
            if not _is_row_active(admin_row):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Admin account exists but is not active.",
                )
            if verify_password(password, admin_row.password_hash):
                _touch_last_login_if_due(db, admin_row)
                return _issue_subject_session(admin_row, subject_type="admin")
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Account not found. Please sign up first.",
        )
    finally:
        db.close()


def register_auth_routes(
    app: FastAPI,
    get_db: Callable[..., Any],
) -> None:
    def _client_ip(request: Request) -> str:
        client = request.client
        return client.host if client and client.host else "unknown"

    @app.post("/api/v1/auth/admin/login")
    def admin_login(payload: LoginPayload, request: Request, db: Session = Depends(get_db)) -> dict[str, Any]:
        enforce_rate_limit(f"login:ip:{_client_ip(request)}", max_events=15, window_seconds=300)
        if payload.password is None:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="password is required")
        if payload.email and not payload.phone:
            row = _lookup_admin_by_email(db, payload.email)
        else:
            row = _lookup_admin_by_phone(db, payload.phone)
        if row is None or not _is_row_active(row) or not verify_password(payload.password, row.password_hash):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
        row.last_login_at = utcnow()
        db.commit()
        return _issue_subject_session(row, subject_type="admin")

    @app.post("/api/v1/auth/user/login")
    async def user_login(
        payload: LoginPayload,
        request: Request,
    ) -> dict[str, Any]:
        enforce_rate_limit(f"login:ip:{_client_ip(request)}", max_events=15, window_seconds=300)
        session_factory = request.app.state.db_session_factory
        # Email is the alternative identifier (phone stays the default).
        if payload.email and not payload.phone:
            if payload.password is None:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="password is required for email login",
                )
            return await _run_login_db_worker(
                _login_subject_with_password,
                session_factory,
                email=payload.email.strip(),
                password=payload.password,
            )
        normalized_phone = _normalize_and_validate_signup_phone(payload.phone)
        _log_phone_lookup("auth.user.login", payload.phone, normalized_phone)
        if payload.password is None:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="password is required")

        return await _run_login_db_worker(
            _login_subject_with_password,
            session_factory,
            phone=normalized_phone,
            password=payload.password,
        )

    @app.post("/api/v1/auth/user/register")
    @app.post("/api/v1/auth/user/signup")
    async def user_signup(
        payload: SignupPayload,
        db: Session = Depends(get_db),
    ) -> dict[str, Any]:
        normalized_phone = _normalize_and_validate_signup_phone(payload.phone)
        _log_phone_lookup("auth.user.signup", payload.phone, normalized_phone)
        normalized_name = str(payload.name or "").strip()
        if len(normalized_name) < 2:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Name must be at least 2 characters.",
            )
        # Require a valid WhatsApp-OTP proof for this phone (product rule: sign up
        # with password + OTP). validate_verification_token re-normalizes the phone.
        if not validate_verification_token(payload.verification_token, normalized_phone):
            raise _api_error(
                status.HTTP_400_BAD_REQUEST,
                "AUTH_OTP_REQUIRED",
                "Phone verification is required. Please verify the code sent to your phone.",
            )

        def _lookup_work() -> None:
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

        await asyncio.to_thread(_lookup_work)

        def _persist_work() -> dict[str, Any]:
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

            return _issue_user_session(user_row)

        return await asyncio.to_thread(_persist_work)

    @app.post("/api/v1/auth/user/otp-login")
    @app.post("/auth/user/otp-login")
    def user_otp_login(
        payload: OtpLoginPayload,
        request: Request,
        db: Session = Depends(get_db),
    ) -> dict[str, Any]:
        """Passwordless login for an existing account via a verified WhatsApp OTP."""
        enforce_rate_limit(f"login:ip:{_client_ip(request)}", max_events=15, window_seconds=300)
        normalized_phone = _normalize_and_validate_signup_phone(payload.phone)
        _log_phone_lookup("auth.user.otp-login", payload.phone, normalized_phone)
        if not validate_verification_token(payload.verification_token, normalized_phone):
            raise _api_error(
                status.HTTP_400_BAD_REQUEST,
                "AUTH_OTP_INVALID",
                "Phone verification failed. Please request a new code.",
            )
        row, subject_type = _lookup_subject_by_phone(db, normalized_phone)
        if row is None:
            raise _api_error(
                status.HTTP_404_NOT_FOUND,
                "AUTH_NO_ACCOUNT",
                "No account found for this phone. Please sign up first.",
            )
        if not _is_row_active(row):
            raise _api_error(
                status.HTTP_403_FORBIDDEN,
                "AUTH_ACCOUNT_INACTIVE",
                "This account is not active. Please contact support.",
            )
        _touch_last_login_if_due(db, row)
        return _issue_subject_session(row, subject_type=subject_type)

    @app.post("/api/v1/auth/user/reset-password")
    @app.post("/auth/user/reset-password")
    def user_reset_password(
        payload: ResetPasswordPayload,
        request: Request,
        db: Session = Depends(get_db),
    ) -> dict[str, Any]:
        """Set a new password using a verified WhatsApp OTP (forgot-password flow).

        The OTP already proves phone ownership, so we auto-issue a session on
        success — the user lands signed in with their new password."""
        enforce_rate_limit(f"login:ip:{_client_ip(request)}", max_events=15, window_seconds=300)
        normalized_phone = _normalize_and_validate_signup_phone(payload.phone)
        _log_phone_lookup("auth.user.reset-password", payload.phone, normalized_phone)
        if not validate_verification_token(payload.verification_token, normalized_phone):
            raise _api_error(
                status.HTTP_400_BAD_REQUEST,
                "AUTH_OTP_INVALID",
                "Phone verification failed. Please request a new code.",
            )
        row, subject_type = _lookup_subject_by_phone(db, normalized_phone)
        if row is None:
            raise _api_error(
                status.HTTP_404_NOT_FOUND,
                "AUTH_NO_ACCOUNT",
                "No account found for this phone. Please sign up first.",
            )
        if not _is_row_active(row):
            raise _api_error(
                status.HTTP_403_FORBIDDEN,
                "AUTH_ACCOUNT_INACTIVE",
                "This account is not active. Please contact support.",
            )
        row.password_hash = hash_password(payload.new_password)
        row.last_login_at = utcnow()
        db.commit()
        db.refresh(row)
        return _issue_subject_session(row, subject_type=subject_type)

    @app.post("/api/v1/auth/refresh")
    @app.post("/auth/refresh")
    def auth_refresh(
        claims: dict[str, Any] = Depends(get_token_claims),
        db: Session = Depends(get_db),
    ) -> dict[str, Any]:
        """Roll a still-valid session forward so active users never hit the TTL.

        The client calls this on every app open. require_active_subject rejects a
        token whose subject was deleted/deactivated; get_token_claims rejects an
        expired or forged token (401), in which case the client keeps its cached
        session and simply retries next launch."""
        row = require_active_subject(db, claims=claims)
        subject_type = "admin" if isinstance(row, AdminUser) else "user"
        return _issue_subject_session(row, subject_type=subject_type)

    # The unprefixed `/auth/*` paths are intentional back-compat aliases for older
    # app builds that called the API without the `/api/v1` prefix. New clients use
    # the `/api/v1/...` form; both map to the same handler. Keep both until those
    # legacy builds are retired.
    @app.get("/api/v1/auth/me")
    @app.get("/auth/me")
    def auth_me(
        token: str = Depends(require_bearer_token),
        db: Session = Depends(get_db),
        x_app_version: str | None = Header(default=None, alias="X-App-Version"),
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
                "createdAt": row.created_at,
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
        # Stamp the app build this user is running (sent by the client on every
        # request; /auth/me fires on each launch). Write only on change.
        _stamp_app_version(db, row, x_app_version)
        return {
            "subjectType": "user",
            "id": row.id,
            "phone": row.phone,
            "name": row.name,
            "email": row.email,
            "status": row.status,
            "isLoyalty": row.is_loyalty,
            "createdAt": row.created_at,
            "preferredLanguage": row.preferred_language,
            "preferredCurrency": row.preferred_currency,
            "notificationsEnabled": row.notifications_enabled,
        }

    @app.patch("/api/v1/auth/me")
    @app.patch("/auth/me")
    def update_auth_me(
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

        if "preferred_language" in provided_fields and isinstance(row, AppUser):
            value = (payload.preferred_language or "").strip().lower() if payload.preferred_language is not None else None
            row.preferred_language = value or None

        if "preferred_currency" in provided_fields and isinstance(row, AppUser):
            value = (payload.preferred_currency or "").strip().upper() if payload.preferred_currency is not None else None
            row.preferred_currency = value or None

        if "notifications_enabled" in provided_fields and isinstance(row, AppUser):
            row.notifications_enabled = bool(payload.notifications_enabled)

        # Audit #11: password change moved here from the retired non-admin
        # branch of POST /admin/users. Same scope as before: AppUser only
        # (AppUser-only fields are silently skipped for admin subjects, like
        # the preference fields above); a null password is a no-op.
        # SEC hardening: the current password must be presented and verified,
        # so a stolen bearer token can't be parlayed into a permanent takeover.
        if "password" in provided_fields and payload.password and isinstance(row, AppUser):
            if not verify_password(payload.current_password or "", row.password_hash):
                raise _api_error(
                    status.HTTP_401_UNAUTHORIZED,
                    "AUTH_INVALID_CURRENT_PASSWORD",
                    "Current password is incorrect",
                )
            row.password_hash = hash_password(payload.password)

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
            "preferredLanguage": row.preferred_language,
            "preferredCurrency": row.preferred_currency,
            "notificationsEnabled": row.notifications_enabled,
        }

    @app.delete("/api/v1/auth/me")
    @app.delete("/auth/me")
    @app.post("/api/v1/auth/user/delete")
    @app.post("/auth/user/delete")
    def delete_authenticated_user(
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
