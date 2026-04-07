from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


DEFAULT_ESIM_ACCESS_BASE_URL = "https://api.esimaccess.com"
DEFAULT_ESIM_ACCESS_TIMEOUT_SECONDS = 30.0
DEFAULT_ESIM_ACCESS_RATE_LIMIT_PER_SECOND = 8.0
DEFAULT_FIB_PAYMENT_BASE_URL = "https://fib.stage.fib.iq"
DEFAULT_FIB_PAYMENT_TIMEOUT_SECONDS = 30.0
DEFAULT_FIB_PAYMENT_RATE_LIMIT_PER_SECOND = 8.0
DEFAULT_AUTH_SECRET_KEY = "change-me-before-production"
DEFAULT_AUTH_TOKEN_TTL_SECONDS = 24 * 60 * 60


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    esim_access_access_code: str = Field(alias="ESIM_ACCESS_ACCESS_CODE")
    esim_access_secret_key: str = Field(alias="ESIM_ACCESS_SECRET_KEY")
    fib_payment_client_id: str | None = Field(default=None, alias="FIB_PAYMENT_CLIENT_ID")
    fib_payment_client_secret: str | None = Field(default=None, alias="FIB_PAYMENT_CLIENT_SECRET")
    fib_payment_base_url: str = Field(default=DEFAULT_FIB_PAYMENT_BASE_URL, alias="FIB_PAYMENT_BASE_URL")
    fib_payment_timeout_seconds: float = Field(
        default=DEFAULT_FIB_PAYMENT_TIMEOUT_SECONDS,
        alias="FIB_PAYMENT_TIMEOUT_SECONDS",
    )
    fib_payment_rate_limit_per_second: float = Field(
        default=DEFAULT_FIB_PAYMENT_RATE_LIMIT_PER_SECOND,
        alias="FIB_PAYMENT_RATE_LIMIT_PER_SECOND",
    )
    fib_payment_status_callback_url: str | None = Field(default=None, alias="FIB_PAYMENT_STATUS_CALLBACK_URL")
    fib_payment_redirect_uri: str | None = Field(default=None, alias="FIB_PAYMENT_REDIRECT_URI")
    fib_payment_webhook_secret: str | None = Field(default=None, alias="FIB_PAYMENT_WEBHOOK_SECRET")
    database_url: str = Field(default="sqlite:///./esim_access.db", alias="DATABASE_URL")
    auth_secret_key: str = Field(default=DEFAULT_AUTH_SECRET_KEY, alias="AUTH_SECRET_KEY")
    auth_token_ttl_seconds: int = Field(default=DEFAULT_AUTH_TOKEN_TTL_SECONDS, alias="AUTH_TOKEN_TTL_SECONDS")


@lru_cache
def get_settings() -> Settings:
    return Settings()
