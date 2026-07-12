from __future__ import annotations

import os
from functools import lru_cache

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


DEFAULT_ESIM_ACCESS_BASE_URL = "https://api.esimaccess.com"
DEFAULT_ESIM_ACCESS_TIMEOUT_SECONDS = 30.0
DEFAULT_ESIM_ACCESS_RATE_LIMIT_PER_SECOND = 8.0
DEFAULT_AUTH_SECRET_KEY = "change-me-before-production"
# Session lifetime. The old 24h default silently signed users out roughly daily
# ("signed out after time"). Sessions are meant to be durable: the client rolls
# the token forward on every app open via POST /auth/refresh, so an active user
# effectively never has to re-authenticate. 60 days is the hard ceiling for a
# user who does not open the app at all in that window. Override with
# AUTH_TOKEN_TTL_SECONDS. NOTE: these bearer tokens are not individually
# revocable — rotating AUTH_SECRET_KEY invalidates all sessions at once.
DEFAULT_AUTH_TOKEN_TTL_SECONDS = 60 * 24 * 60 * 60
DEFAULT_FIB_PAYMENT_BASE_URL = "https://fib.prod.fib.iq"
DEFAULT_FIB_PAYMENT_REDIRECT_URI = "tulip://payment/result"
DEFAULT_VERIFYWAY_BASE_URL = "https://api.verifyway.com/api/v1/"


# ---------------------------------------------------------------------------
# Environment readers — single source of truth. Domain modules import these
# instead of each re-declaring byte-identical helpers (see app.py, auth.py,
# esim_access_api.py, supabase_store.py).
# ---------------------------------------------------------------------------
def read_bool_env(name: str, default: bool) -> bool:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    normalized = raw_value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def read_float_env(name: str, default: float, *, minimum: float = 0.0) -> float:
    raw_value = os.getenv(name)
    if raw_value is None or raw_value.strip() == "":
        return default
    try:
        parsed = float(raw_value)
    except ValueError:
        return default
    return max(parsed, minimum)


def read_int_env(name: str, default: int, *, minimum: int = 0) -> int:
    raw_value = os.getenv(name)
    if raw_value is None or raw_value.strip() == "":
        return default
    try:
        parsed = int(raw_value)
    except ValueError:
        return default
    return max(parsed, minimum)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Serve the interactive API docs (/docs, /redoc, /openapi.json). Default True
    # so local development is unchanged; set API_DOCS_ENABLED=false in production
    # because the docs enumerate the full admin/payment surface to anonymous
    # visitors (audit finding #14).
    api_docs_enabled: bool = Field(default=True, alias="API_DOCS_ENABLED")
    esim_access_access_code: str = Field(alias="ESIM_ACCESS_ACCESS_CODE")
    esim_access_secret_key: str = Field(alias="ESIM_ACCESS_SECRET_KEY")
    esim_access_webhook_secret: str | None = Field(default=None, alias="ESIM_ACCESS_WEBHOOK_SECRET")
    fib_payment_client_id: str | None = Field(default=None, alias="FIB_PAYMENT_CLIENT_ID")
    fib_payment_client_secret: str | None = Field(default=None, alias="FIB_PAYMENT_CLIENT_SECRET")
    fib_payment_webhook_secret: str | None = Field(default=None, alias="FIB_PAYMENT_WEBHOOK_SECRET")
    # Accept the raw secret echoed in X-FIB-WEBHOOK-SECRET as webhook auth.
    # Default False → the webhook accepts ONLY the HMAC-over-body signature (which
    # binds auth to the payload) and rejects the static, replayable plaintext
    # bearer. Set FIB_WEBHOOK_ALLOW_PLAINTEXT_SECRET=true to re-enable the
    # plaintext path if FIB still calls the webhook with the raw secret.
    # NOTE: this ONLY governs webhook-callback auth. FIB payment create + confirm
    # run via server-side polling and are UNAFFECTED by this flag.
    fib_webhook_allow_plaintext_secret: bool = Field(
        default=False, alias="FIB_WEBHOOK_ALLOW_PLAINTEXT_SECRET"
    )
    fib_payment_base_url: str = Field(default=DEFAULT_FIB_PAYMENT_BASE_URL, alias="FIB_PAYMENT_BASE_URL")
    # Deployment-specific webhook URL FIB calls back with payment status. No
    # hardcoded default (audit #6) — set FIB_PAYMENT_STATUS_CALLBACK_URL per
    # deployment. When unset, payment creation omits statusCallbackUrl and
    # confirmation relies on server-side status polling (startup logs an ERROR).
    fib_payment_status_callback_url: str | None = Field(
        default=None, alias="FIB_PAYMENT_STATUS_CALLBACK_URL"
    )
    fib_payment_redirect_uri: str = Field(default=DEFAULT_FIB_PAYMENT_REDIRECT_URI, alias="FIB_PAYMENT_REDIRECT_URI")
    firebase_service_account_file: str | None = Field(default=None, alias="FIREBASE_SERVICE_ACCOUNT_FILE")
    firebase_service_account_json: str | None = Field(default=None, alias="FIREBASE_SERVICE_ACCOUNT_JSON")
    push_notification_default_channel_id: str = Field(default="general", alias="PUSH_NOTIFICATION_DEFAULT_CHANNEL_ID")
    # Push hardening. Validate Firebase creds at boot (fail-fast log) instead of
    # only on the first admin send; cap how often a single admin can fire sends;
    # retention window used by scripts/cleanup_anonymous_push_devices.py.
    firebase_validate_on_startup: bool = Field(default=True, alias="FIREBASE_VALIDATE_ON_STARTUP")
    push_send_rate_limit_max: int = Field(default=30, alias="PUSH_SEND_RATE_LIMIT_MAX")
    push_send_rate_limit_window_seconds: int = Field(default=3600, alias="PUSH_SEND_RATE_LIMIT_WINDOW_SECONDS")
    push_anonymous_device_retention_days: int = Field(default=90, alias="PUSH_ANONYMOUS_DEVICE_RETENTION_DAYS")
    database_url: str = Field(default="sqlite:///./esim_access.db", alias="DATABASE_URL")
    auth_secret_key: str = Field(default=DEFAULT_AUTH_SECRET_KEY, alias="AUTH_SECRET_KEY")
    auth_token_ttl_seconds: int = Field(default=DEFAULT_AUTH_TOKEN_TTL_SECONDS, alias="AUTH_TOKEN_TTL_SECONDS")
    # VerifyWay OTP delivery (WhatsApp/SMS). Endpoints return 503 until the API
    # key is configured. OTP tunables are env-overridable but ship with sane
    # defaults: 4-digit code, 5-minute validity, 60s resend cooldown, 5 attempts.
    verifyway_api_key: str | None = Field(default=None, alias="VERIFYWAY_API_KEY")
    verifyway_base_url: str = Field(default=DEFAULT_VERIFYWAY_BASE_URL, alias="VERIFYWAY_BASE_URL")
    verifyway_channel: str = Field(default="whatsapp", alias="VERIFYWAY_CHANNEL")
    verifyway_timeout_seconds: float = Field(default=30.0, alias="VERIFYWAY_TIMEOUT_SECONDS")
    verifyway_otp_length: int = Field(default=4, ge=4, le=8, alias="VERIFYWAY_OTP_LENGTH")
    verifyway_otp_ttl_seconds: int = Field(default=300, ge=60, alias="VERIFYWAY_OTP_TTL_SECONDS")
    verifyway_otp_resend_cooldown_seconds: int = Field(
        default=60, ge=0, alias="VERIFYWAY_OTP_RESEND_COOLDOWN_SECONDS"
    )
    verifyway_otp_max_attempts: int = Field(default=5, ge=1, alias="VERIFYWAY_OTP_MAX_ATTEMPTS")
    wings_auth_token: str | None = Field(default=None, alias="WINGS_AUTH_TOKEN")
    wings_base_url: str = Field(default="https://wings.laveen-air.com/RIAM_main/rest/api", alias="WINGS_BASE_URL")
    wings_search_url: str | None = Field(default=None, alias="WINGS_SEARCH_URL")
    wings_request_timeout_seconds: float = Field(default=60.0, alias="WINGS_REQUEST_TIMEOUT_SECONDS")

    @model_validator(mode="after")
    def validate_production_secrets(self) -> "Settings":
        if not self.auth_secret_key or self.auth_secret_key == DEFAULT_AUTH_SECRET_KEY:
            raise ValueError("AUTH_SECRET_KEY must be set to a strong deployment-specific value.")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
