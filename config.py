from __future__ import annotations

import os
from functools import lru_cache

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


DEFAULT_ESIM_ACCESS_BASE_URL = "https://api.esimaccess.com"
DEFAULT_ESIM_ACCESS_TIMEOUT_SECONDS = 30.0
DEFAULT_ESIM_ACCESS_RATE_LIMIT_PER_SECOND = 8.0
DEFAULT_AUTH_SECRET_KEY = "change-me-before-production"
DEFAULT_AUTH_TOKEN_TTL_SECONDS = 24 * 60 * 60
DEFAULT_FIB_PAYMENT_BASE_URL = "https://fib.prod.fib.iq"
DEFAULT_FIB_PAYMENT_STATUS_CALLBACK_URL = (
    "https://mean-lettie-corevia-0bd7cc91.koyeb.app/api/v1/payments/fib/webhook"
)
DEFAULT_FIB_PAYMENT_REDIRECT_URI = "tulip://payment/result"
DEFAULT_TWILIO_VERIFY_BASE_URL = "https://verify.twilio.com"


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

    esim_access_access_code: str = Field(alias="ESIM_ACCESS_ACCESS_CODE")
    esim_access_secret_key: str = Field(alias="ESIM_ACCESS_SECRET_KEY")
    esim_access_webhook_secret: str | None = Field(default=None, alias="ESIM_ACCESS_WEBHOOK_SECRET")
    fib_payment_client_id: str | None = Field(default=None, alias="FIB_PAYMENT_CLIENT_ID")
    fib_payment_client_secret: str | None = Field(default=None, alias="FIB_PAYMENT_CLIENT_SECRET")
    fib_payment_webhook_secret: str | None = Field(default=None, alias="FIB_PAYMENT_WEBHOOK_SECRET")
    fib_payment_base_url: str = Field(default=DEFAULT_FIB_PAYMENT_BASE_URL, alias="FIB_PAYMENT_BASE_URL")
    fib_payment_status_callback_url: str = Field(
        default=DEFAULT_FIB_PAYMENT_STATUS_CALLBACK_URL, alias="FIB_PAYMENT_STATUS_CALLBACK_URL"
    )
    fib_payment_redirect_uri: str = Field(default=DEFAULT_FIB_PAYMENT_REDIRECT_URI, alias="FIB_PAYMENT_REDIRECT_URI")
    firebase_service_account_file: str | None = Field(default=None, alias="FIREBASE_SERVICE_ACCOUNT_FILE")
    firebase_service_account_json: str | None = Field(default=None, alias="FIREBASE_SERVICE_ACCOUNT_JSON")
    push_notification_default_channel_id: str = Field(default="general", alias="PUSH_NOTIFICATION_DEFAULT_CHANNEL_ID")
    database_url: str = Field(default="sqlite:///./esim_access.db", alias="DATABASE_URL")
    auth_secret_key: str = Field(default=DEFAULT_AUTH_SECRET_KEY, alias="AUTH_SECRET_KEY")
    auth_token_ttl_seconds: int = Field(default=DEFAULT_AUTH_TOKEN_TTL_SECONDS, alias="AUTH_TOKEN_TTL_SECONDS")
    twilio_account_sid: str | None = Field(default=None, alias="TWILIO_ACCOUNT_SID")
    twilio_auth_token: str | None = Field(default=None, alias="TWILIO_AUTH_TOKEN")
    twilio_verify_service_sid: str | None = Field(default=None, alias="TWILIO_VERIFY_SERVICE_SID")
    twilio_verify_base_url: str = Field(default=DEFAULT_TWILIO_VERIFY_BASE_URL, alias="TWILIO_VERIFY_BASE_URL")
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
