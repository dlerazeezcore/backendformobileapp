from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy.exc import OperationalError as SQLAlchemyOperationalError
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError

from config import read_float_env, read_int_env
from esim_access_api import ESimAccessAPI
from fib_payment_api import FIBPaymentAPI
from push_notification import PushNotificationService

LOGGER = logging.getLogger(__name__)


def get_provider(request: Request) -> ESimAccessAPI:
    return request.app.state.esim_access_api


def get_db(request: Request) -> Any:
    session_factory = request.app.state.db_session_factory
    # Establish the physical DB connection HERE — before the route handler runs —
    # with a bounded retry. The Supabase transaction pooler intermittently fails to
    # accept a brand-new connection within connect_timeout (psycopg ConnectionTimeout
    # -> SQLAlchemy OperationalError). Forcing the connect at this point, before any
    # handler logic or user SQL has executed, means a transient pooler blip can be
    # retried transparently and side-effect-free instead of becoming a user-facing
    # 503. Only connection/checkout failures surface here; genuine query errors happen
    # later inside the handler and are NOT retried by this loop. All knobs are
    # env-tunable; default is a single retry (2 attempts total).
    max_retries = read_int_env("DATABASE_CONNECT_RETRY_ATTEMPTS", 1, minimum=0)
    backoff_seconds = read_float_env("DATABASE_CONNECT_RETRY_BACKOFF_SECONDS", 0.25, minimum=0.0)
    session = None
    for attempt in range(max_retries + 1):
        session = session_factory()
        try:
            # Force pool checkout + pool_pre_ping + physical connect now so a
            # connect failure is raised here (retryable) rather than lazily in
            # the handler (not retryable without re-running side effects).
            session.connection()
            break
        except (SQLAlchemyOperationalError, SQLAlchemyTimeoutError) as exc:
            try:
                session.close()
            except Exception:
                LOGGER.exception("get_db: closing pre-connect session failed")
            if attempt >= max_retries:
                LOGGER.warning(
                    "get_db: DB connect failed after %s attempt(s): %s", max_retries + 1, exc
                )
                raise
            LOGGER.warning(
                "get_db: DB connect attempt %s/%s failed, retrying in %.2fs: %s",
                attempt + 1,
                max_retries + 1,
                backoff_seconds,
                exc,
            )
            if backoff_seconds > 0:
                time.sleep(backoff_seconds)
    try:
        yield session
    except Exception:
        try:
            session.rollback()
        except Exception:
            # BE-8: surface rollback failures instead of swallowing them — a failed
            # rollback can leave a poisoned connection that the pool hands out next.
            LOGGER.exception("get_db: session rollback failed")
        raise
    finally:
        session.close()


def get_fib_provider(request: Request) -> FIBPaymentAPI:
    provider: FIBPaymentAPI | None = getattr(request.app.state, "fib_payment_api", None)
    if provider is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="FIB payment integration is not configured on this deployment.",
        )
    return provider


def get_push_provider(request: Request) -> PushNotificationService:
    provider: PushNotificationService | None = getattr(request.app.state, "push_notification_service", None)
    if provider is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Push notification service is not configured on this deployment.",
        )
    return provider
