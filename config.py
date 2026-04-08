from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


DEFAULT_ESIM_ACCESS_BASE_URL = "https://api.esimaccess.com"
DEFAULT_ESIM_ACCESS_TIMEOUT_SECONDS = 30.0
DEFAULT_ESIM_ACCESS_RATE_LIMIT_PER_SECOND = 8.0
DEFAULT_AUTH_SECRET_KEY = "change-me-before-production"
DEFAULT_AUTH_TOKEN_TTL_SECONDS = 24 * 60 * 60


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    esim_access_access_code: str = Field(alias="ESIM_ACCESS_ACCESS_CODE")
    esim_access_secret_key: str = Field(alias="ESIM_ACCESS_SECRET_KEY")
    fib_payment_client_id: str | None = Field(default=None, alias="FIB_PAYMENT_CLIENT_ID")
    fib_payment_client_secret: str | None = Field(default=None, alias="FIB_PAYMENT_CLIENT_SECRET")
    fib_payment_webhook_secret: str | None = Field(default=None, alias="FIB_PAYMENT_WEBHOOK_SECRET")
    firebase_service_account_file: str | None = Field(default=None, alias="FIREBASE_SERVICE_ACCOUNT_FILE")
    firebase_service_account_json: str | None = Field(default=None, alias="FIREBASE_SERVICE_ACCOUNT_JSON")
    push_notification_default_channel_id: str = Field(default="general", alias="PUSH_NOTIFICATION_DEFAULT_CHANNEL_ID")
    database_url: str = Field(default="sqlite:///./esim_access.db", alias="DATABASE_URL")
    auth_secret_key: str = Field(default=DEFAULT_AUTH_SECRET_KEY, alias="AUTH_SECRET_KEY")
    auth_token_ttl_seconds: int = Field(default=DEFAULT_AUTH_TOKEN_TTL_SECONDS, alias="AUTH_TOKEN_TTL_SECONDS")


@lru_cache
def get_settings() -> Settings:
    return Settings()
