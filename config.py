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
    telegram_support_bot_token: str | None = Field(default=None, alias="TELEGRAM_SUPPORT_BOT_TOKEN")
    telegram_support_webhook_secret: str | None = Field(default=None, alias="TELEGRAM_SUPPORT_WEBHOOK_SECRET")
    telegram_support_webhook_base_url: str = Field(
        default="https://mean-lettie-corevia-0bd7cc91.koyeb.app",
        alias="TELEGRAM_SUPPORT_WEBHOOK_BASE_URL",
    )
    telegram_support_auto_sync_on_list: bool = Field(default=True, alias="TELEGRAM_SUPPORT_AUTO_SYNC_ON_LIST")
    twilio_account_sid: str | None = Field(default=None, alias="TWILIO_ACCOUNT_SID")
    twilio_auth_token: str | None = Field(default=None, alias="TWILIO_AUTH_TOKEN")
    twilio_verify_service_sid: str | None = Field(default=None, alias="TWILIO_VERIFY_SERVICE_SID")
    twilio_verify_base_url: str = Field(default="https://verify.twilio.com", alias="TWILIO_VERIFY_BASE_URL")
    twilio_verify_timeout_seconds: float = Field(default=20.0, alias="TWILIO_VERIFY_TIMEOUT_SECONDS")
    twilio_verify_rate_limit_per_second: float = Field(default=5.0, alias="TWILIO_VERIFY_RATE_LIMIT_PER_SECOND")
    support_uploads_s3_endpoint: str | None = Field(
        default="https://splzxivzahitxmjcqstn.storage.supabase.co/storage/v1/s3",
        alias="SUPPORT_UPLOADS_S3_ENDPOINT",
    )
    support_uploads_s3_region: str = Field(default="ap-southeast-2", alias="SUPPORT_UPLOADS_S3_REGION")
    support_uploads_access_key_id: str | None = Field(
        default="4a685847f3ada521c85262193ff55e03",
        alias="SUPPORT_UPLOADS_ACCESS_KEY_ID",
    )
    support_uploads_secret_access_key: str | None = Field(
        default="83bc874f3441c4f16a801e930c8de54aef29a20070d0fa5e7865950530e0ee75",
        alias="SUPPORT_UPLOADS_SECRET_ACCESS_KEY",
    )
    support_uploads_bucket: str = Field(default="Tulip Mobile APP", alias="SUPPORT_UPLOADS_BUCKET")
    support_uploads_object_prefix: str = Field(default="support", alias="SUPPORT_UPLOADS_OBJECT_PREFIX")
    support_uploads_url_ttl_seconds: int = Field(default=600, alias="SUPPORT_UPLOADS_URL_TTL_SECONDS")
    support_uploads_max_file_bytes: int = Field(default=10 * 1024 * 1024, alias="SUPPORT_UPLOADS_MAX_FILE_BYTES")
    support_uploads_public_base_url: str | None = Field(
        default="https://splzxivzahitxmjcqstn.storage.supabase.co/storage/v1/object/public",
        alias="SUPPORT_UPLOADS_PUBLIC_BASE_URL",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
