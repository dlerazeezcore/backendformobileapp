from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    esim_access_access_code: str = Field(alias="ESIM_ACCESS_ACCESS_CODE")
    esim_access_secret_key: str = Field(alias="ESIM_ACCESS_SECRET_KEY")
    esim_access_base_url: str = Field(default="https://api.esimaccess.com", alias="ESIM_ACCESS_BASE_URL")
    esim_access_timeout_seconds: float = Field(default=30.0, alias="ESIM_ACCESS_TIMEOUT_SECONDS")
    esim_access_rate_limit_per_second: float = Field(default=8.0, alias="ESIM_ACCESS_RATE_LIMIT_PER_SECOND")
    database_url: str = Field(default="sqlite:///./esim_access.db", alias="DATABASE_URL")


@lru_cache
def get_settings() -> Settings:
    return Settings()
