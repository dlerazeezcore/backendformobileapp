from __future__ import annotations

import os
import time
from logging.config import fileConfig
import logging

from alembic import context
from dotenv import load_dotenv
from sqlalchemy import engine_from_config, pool
from sqlalchemy.exc import OperationalError

from supabase_store import Base, normalize_database_url

load_dotenv()

config = context.config
LOGGER = logging.getLogger("alembic.env")

database_url = normalize_database_url(
    os.getenv("DATABASE_URL", "sqlite:///./esim_access.db")
)
config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _as_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _is_pool_saturation_error(error: OperationalError) -> bool:
    lowered = str(error).lower()
    return "maxclientsinsessionmode" in lowered or "max clients reached" in lowered


def _is_supabase_pooler_database_url(url: str) -> bool:
    lowered = (url or "").lower()
    return "pooler.supabase.com" in lowered


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    max_retries = max(int(os.getenv("ALEMBIC_DB_CONNECT_RETRIES", "8")), 1)
    retry_delay_seconds = max(float(os.getenv("ALEMBIC_DB_CONNECT_RETRY_DELAY_SECONDS", "1.5")), 0.1)
    allow_skip_on_pool_saturation = _as_bool(os.getenv("ALEMBIC_ALLOW_SKIP_ON_POOL_SATURATION"), default=True)

    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        connect_args={"prepare_threshold": None} if _is_supabase_pooler_database_url(database_url) else {},
    )

    for attempt in range(1, max_retries + 1):
        try:
            with connectable.connect() as connection:
                context.configure(
                    connection=connection,
                    target_metadata=target_metadata,
                    compare_type=True,
                    compare_server_default=True,
                )

                with context.begin_transaction():
                    context.run_migrations()
            return
        except OperationalError as error:
            if attempt < max_retries:
                LOGGER.warning(
                    "Alembic DB connect failed (attempt %s/%s). Retrying in %.1fs.",
                    attempt,
                    max_retries,
                    retry_delay_seconds,
                )
                time.sleep(retry_delay_seconds)
                continue
            if allow_skip_on_pool_saturation and _is_pool_saturation_error(error):
                LOGGER.warning(
                    "Skipping alembic migration for this startup after %s failed connection attempts "
                    "due to DB pool saturation.",
                    max_retries,
                )
                return
            raise


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
