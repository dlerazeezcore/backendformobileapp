from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import logging
import os
from pathlib import Path
from time import monotonic
import uuid
from typing import Any, AsyncIterator

from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from sqlalchemy import text
from sqlalchemy.exc import InternalError as SQLAlchemyInternalError
from sqlalchemy.exc import OperationalError as SQLAlchemyOperationalError
from sqlalchemy.exc import ProgrammingError as SQLAlchemyProgrammingError
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError

from admin import register_admin_routes
from app_meta import register_app_meta_routes
from auth import register_auth_routes
from config import (
    DEFAULT_ESIM_ACCESS_BASE_URL,
    DEFAULT_ESIM_ACCESS_RATE_LIMIT_PER_SECOND,
    DEFAULT_ESIM_ACCESS_TIMEOUT_SECONDS,
    Settings,
    get_settings,
    read_bool_env as _read_bool_env,
    read_float_env as _read_float_env,
)
from dependencies import get_db, get_fib_provider, get_provider, get_push_provider
from esim_access_api import (
    ESimAccessAPI,
    ESimAccessAPIError,
    ESimAccessHTTPError,
    register_esim_access_routes,
    stop_periodic_usage_sync_worker,
)
from fib_payment_api import (
    FIBPaymentAPI,
    FIBPaymentAPIError,
    FIBPaymentHTTPError,
    register_fib_payment_routes,
)
from push_notification import PushNotificationService, register_push_notification_routes
from supabase_store import create_database
from users import register_user_routes
from verifyway import register_verifyway_routes
from wings_api import register_wings_routes

DEFAULT_CORS_ALLOWED_ORIGINS = [
    # Production web origins.
    "https://tulipbookings.com",
    "https://www.tulipbookings.com",
    "https://dlerazeezcore.github.io",
    # Native shell / dev origins. NOTE: no bare "http://localhost" (L12) —
    # "https://localhost" stays because it is the Capacitor Android WebView origin.
    "capacitor://localhost",
    "ionic://localhost",
    "https://localhost",
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    # Expo web dev server (npm run web / npm run dev → expo start --web --port 8081).
    "http://localhost:8081",
    "http://127.0.0.1:8081",
    "http://localhost:19006",
    "http://127.0.0.1:19006",
]
CORS_ALLOWED_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
CORS_ALLOWED_HEADERS = ["*"]
# No arbitrary-origin reflection by default; only explicit allow_origins are
# echoed back. An operator can still opt into a regex via CORS_ALLOW_ORIGIN_REGEX.
DEFAULT_CORS_ALLOW_ORIGIN_REGEX: str | None = None
# Connection-tuning constants stay here; URLs/endpoints live in config.py (env-overridable).
FIB_PAYMENT_TIMEOUT_SECONDS = 30.0
FIB_PAYMENT_RATE_LIMIT_PER_SECOND = 8.0
PUSH_NOTIFICATION_DEFAULT_CHANNEL_ID = "general"
# Optional hardcoded fallback when env var is not set.
FIB_PAYMENT_WEBHOOK_SECRET: str | None = None
LOGGER = logging.getLogger("uvicorn.error")


def _split_env_csv(value: str | None) -> list[str]:
    if value is None:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _get_cors_allowed_origins() -> list[str]:
    configured = _split_env_csv(os.getenv("CORS_ALLOWED_ORIGINS"))
    if configured:
        return configured
    return DEFAULT_CORS_ALLOWED_ORIGINS


def _get_cors_allow_origin_regex() -> str | None:
    configured = os.getenv("CORS_ALLOW_ORIGIN_REGEX")
    if configured is not None:
        stripped = configured.strip()
        # With allow_credentials=True a loose regex enables credentialed
        # cross-origin reads. An unanchored pattern is the classic foot-gun
        # ("https://app.example.com" also matches "https://app.example.com.evil.io"),
        # so demand explicit ^...$ anchoring and warn loudly when it's missing.
        if stripped and (not stripped.startswith("^") or not stripped.endswith("$")):
            logging.getLogger("uvicorn.error").warning(
                "CORS_ALLOW_ORIGIN_REGEX is not anchored (^...$): %r. "
                "Unanchored patterns can match attacker-controlled origins under "
                "allow_credentials=True — anchor the pattern explicitly.",
                stripped,
            )
        return stripped or None
    return DEFAULT_CORS_ALLOW_ORIGIN_REGEX


def _get_expected_alembic_heads() -> list[str]:
    try:
        alembic_ini = Path(__file__).with_name("alembic.ini")
        config = AlembicConfig(str(alembic_ini))
        config.set_main_option("script_location", str(Path(__file__).with_name("alembic")))
        script = ScriptDirectory.from_config(config)
        return sorted(script.get_heads())
    except Exception as exc:  # pragma: no cover - diagnostic endpoint should not break app startup
        LOGGER.warning("alembic.head_lookup_failed detail=%s", exc)
        return []


def create_app(settings: Settings | None = None) -> FastAPI:
    def _is_pool_saturation_detail(raw_detail: str) -> bool:
        lowered = raw_detail.lower()
        return (
            "maxclientsinsessionmode" in lowered
            or "max clients reached" in lowered
            or "unable to check out connection from the pool due to timeout" in lowered
            or "check out connection from the pool due to timeout" in lowered
            or "dbhandler exited" in lowered
        )

    def _error_envelope(
        *,
        status_code: int,
        detail: object,
        error_code: str | None = None,
        request_id: str | None = None,
        extra: dict[str, object] | None = None,
    ) -> dict[str, object]:
        trace_id = request_id or str(uuid.uuid4())
        if isinstance(detail, dict):
            message = str(detail.get("message") or detail.get("detail") or detail.get("error") or "Request failed.")
            inferred_code = str(detail.get("code") or detail.get("errorCode") or f"HTTP_{status_code}")
        else:
            message = str(detail)
            inferred_code = f"HTTP_{status_code}"
        payload: dict[str, object] = {
            "success": False,
            "data": None,
            "errorCode": error_code or inferred_code,
            "message": message,
            "detail": detail,
            "requestId": trace_id,
            "traceId": trace_id,
        }
        if extra:
            payload.update(extra)
        return payload

    def _warm_db_pool(session_factory) -> None:
        """Open one connection and run SELECT 1 to prime the pool before traffic arrives."""
        db = session_factory()
        try:
            db.scalar(text("select 1"))
        finally:
            db.close()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        cfg = settings or get_settings()
        app.state.db_session_factory = create_database(cfg.database_url)
        # Warm the DB connection pool so the first /health/db (and first user request)
        # doesn't pay the Supabase pooler cold-start cost. Falling back to lazy init
        # if warm-up fails is intentional: a transient pooler hiccup must not block
        # the entire process from booting.
        warmup_timeout_seconds = _read_float_env("DATABASE_WARMUP_TIMEOUT_SECONDS", 15.0, minimum=1.0)
        warmup_started_at = monotonic()
        try:
            await asyncio.wait_for(
                asyncio.to_thread(_warm_db_pool, app.state.db_session_factory),
                timeout=warmup_timeout_seconds,
            )
            LOGGER.info(
                "database.warmup_complete elapsed_ms=%.0f",
                (monotonic() - warmup_started_at) * 1000.0,
            )
        except asyncio.TimeoutError:
            LOGGER.warning(
                "database.warmup_timeout timeout_seconds=%.1f -- continuing with lazy init",
                warmup_timeout_seconds,
            )
        except Exception as exc:  # pragma: no cover - defensive: never block boot on warmup
            LOGGER.warning("database.warmup_failed detail=%s", exc)
        app.state.esim_access_api = ESimAccessAPI(
            access_code=cfg.esim_access_access_code,
            secret_key=cfg.esim_access_secret_key,
            base_url=DEFAULT_ESIM_ACCESS_BASE_URL,
            timeout=DEFAULT_ESIM_ACCESS_TIMEOUT_SECONDS,
            rate_limit_per_second=DEFAULT_ESIM_ACCESS_RATE_LIMIT_PER_SECOND,
        )
        app.state.fib_payment_api = None
        fib_webhook_secret = cfg.fib_payment_webhook_secret or FIB_PAYMENT_WEBHOOK_SECRET
        if cfg.fib_payment_client_id and cfg.fib_payment_client_secret:
            # The webhook secret is OPTIONAL. FIB does not sign its status callbacks, so payment
            # confirmation is done by polling the FIB status endpoint (create/status/confirm). The
            # self-managed webhook secret only guards our own /webhook endpoint; when it is absent the
            # webhook route stays disabled (returns 503) while the rest of the integration is fully active.
            app.state.fib_payment_api = FIBPaymentAPI(
                client_id=cfg.fib_payment_client_id,
                client_secret=cfg.fib_payment_client_secret,
                base_url=cfg.fib_payment_base_url,
                timeout=FIB_PAYMENT_TIMEOUT_SECONDS,
                rate_limit_per_second=FIB_PAYMENT_RATE_LIMIT_PER_SECOND,
                default_status_callback_url=cfg.fib_payment_status_callback_url,
                default_redirect_uri=cfg.fib_payment_redirect_uri,
                webhook_secret=fib_webhook_secret,
                webhook_allow_plaintext_secret=cfg.fib_webhook_allow_plaintext_secret,
            )
            if not fib_webhook_secret:
                LOGGER.info(
                    "FIB payment integration active (create/status/confirm via polling). "
                    "FIB_PAYMENT_WEBHOOK_SECRET is not set, so the /webhook endpoint is disabled; "
                    "payment confirmation relies on polling the FIB status endpoint."
                )
        app.state.push_notification_service = PushNotificationService(
            service_account_file=cfg.firebase_service_account_file,
            service_account_json=cfg.firebase_service_account_json,
            default_channel_id=cfg.push_notification_default_channel_id or PUSH_NOTIFICATION_DEFAULT_CHANNEL_ID,
        )
        # Fail-fast: surface a misconfigured Firebase credential in the boot logs
        # instead of silently deferring the failure to the first admin send. This
        # never crashes the app — push is optional (unconfigured => 503).
        if getattr(cfg, "firebase_validate_on_startup", True):
            app.state.push_notification_service.validate_configuration()
        # BE-1: start the background eSIM usage-sync worker here (replaces the
        # deprecated @app.on_event("startup") handler in esim_access_api).
        start_usage_sync = getattr(app.state, "start_esim_usage_sync_worker", None)
        if start_usage_sync is not None:
            await start_usage_sync()
        yield
        # Stop the background eSIM usage-sync worker before tearing down the
        # provider/DB it depends on (replaces esim_access_api's deprecated
        # @app.on_event("shutdown") handler).
        await stop_periodic_usage_sync_worker(app)
        await app.state.esim_access_api.close()
        if app.state.fib_payment_api is not None:
            await app.state.fib_payment_api.close()
        db_session_factory = getattr(app.state, "db_session_factory", None)
        db_engine = getattr(db_session_factory, "kw", {}).get("bind") if db_session_factory is not None else None
        if db_engine is not None:
            db_engine.dispose()

    app = FastAPI(title="backendformobileapp", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_get_cors_allowed_origins(),
        allow_origin_regex=_get_cors_allow_origin_regex(),
        allow_credentials=_read_bool_env("CORS_ALLOW_CREDENTIALS", default=True),
        allow_methods=CORS_ALLOWED_METHODS,
        allow_headers=CORS_ALLOWED_HEADERS,
        max_age=86400,
    )

    @app.middleware("http")
    async def reject_duplicate_api_prefix(request: Request, call_next):
        path = request.url.path
        if path == "/api/v1/api/v1" or path.startswith("/api/v1/api/v1/"):
            return JSONResponse(
                status_code=404,
                content={"detail": "Invalid API path: duplicate '/api/v1' prefix."},
            )
        return await call_next(request)

    @app.exception_handler(HTTPException)
    async def handle_http_exception(request: Request, exc: HTTPException) -> JSONResponse:
        _ = request
        # Preserve exc.headers (Retry-After on 429s, WWW-Authenticate on 401s):
        # re-wrapping into the error envelope must not strip them.
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_envelope(status_code=exc.status_code, detail=exc.detail),
            headers=exc.headers,
        )

    @app.exception_handler(SQLAlchemyTimeoutError)
    async def handle_db_pool_timeout(request: Request, exc: SQLAlchemyTimeoutError) -> JSONResponse:
        LOGGER.warning("database.pool_timeout path=%s detail=%s", request.url.path, str(exc))
        return JSONResponse(
            status_code=503,
            content=_error_envelope(
                status_code=503,
                detail="Database is busy. Please retry in a few seconds.",
                error_code="DB_POOL_TIMEOUT",
            ),
        )

    @app.exception_handler(SQLAlchemyOperationalError)
    async def handle_db_operational_error(request: Request, exc: SQLAlchemyOperationalError) -> JSONResponse:
        raw_detail = str(exc)
        if _is_pool_saturation_detail(raw_detail):
            error_code = "DB_POOL_SATURATED"
            message = "Database connection limit reached. Please retry in a few seconds."
        else:
            error_code = "DB_OPERATIONAL_ERROR"
            message = "Database is temporarily unavailable. Please retry shortly."
        LOGGER.warning("database.operational_error path=%s code=%s detail=%s", request.url.path, error_code, raw_detail)
        return JSONResponse(
            status_code=503,
            content=_error_envelope(
                status_code=503,
                detail=message,
                error_code=error_code,
            ),
        )

    @app.exception_handler(SQLAlchemyInternalError)
    async def handle_db_internal_error(request: Request, exc: SQLAlchemyInternalError) -> JSONResponse:
        raw_detail = str(exc)
        if _is_pool_saturation_detail(raw_detail):
            error_code = "DB_POOL_SATURATED"
            message = "Database connection limit reached. Please retry in a few seconds."
            status_code = 503
        else:
            error_code = "DB_INTERNAL_ERROR"
            message = "Database internal error."
            status_code = 500
        LOGGER.warning("database.internal_error path=%s code=%s detail=%s", request.url.path, error_code, raw_detail)
        return JSONResponse(
            status_code=status_code,
            content=_error_envelope(
                status_code=status_code,
                detail=message,
                error_code=error_code,
            ),
        )

    @app.exception_handler(SQLAlchemyProgrammingError)
    async def handle_db_programming_error(request: Request, exc: SQLAlchemyProgrammingError) -> JSONResponse:
        raw_detail = str(exc)
        lowered = raw_detail.lower()
        if "duplicatepreparedstatement" in lowered or (
            "prepared statement" in lowered and "already exists" in lowered
        ):
            error_code = "DB_PREPARED_STATEMENT_CONFLICT"
            message = "Database pool session conflict detected. Please retry in a few seconds."
            status_code = 503
        else:
            error_code = "DB_PROGRAMMING_ERROR"
            message = "Database query failed."
            status_code = 500
        LOGGER.warning("database.programming_error path=%s code=%s detail=%s", request.url.path, error_code, raw_detail)
        return JSONResponse(
            status_code=status_code,
            content=_error_envelope(
                status_code=status_code,
                detail=message,
                error_code=error_code,
            ),
        )

    @app.exception_handler(ESimAccessHTTPError)
    async def handle_http_error(request: Request, exc: ESimAccessHTTPError) -> JSONResponse:
        _ = request
        return JSONResponse(
            status_code=502,
            content=_error_envelope(
                status_code=502,
                detail=str(exc),
                error_code="ESIM_PROVIDER_UNREACHABLE",
                request_id=exc.request_id,
            ),
        )

    @app.exception_handler(ESimAccessAPIError)
    async def handle_api_error(request: Request, exc: ESimAccessAPIError) -> JSONResponse:
        _ = request
        return JSONResponse(
            status_code=502,
            content=_error_envelope(
                status_code=502,
                detail=str(exc),
                error_code=exc.error_code or "ESIM_PROVIDER_ERROR",
                request_id=exc.request_id,
                extra={"errorMessage": exc.error_message},
            ),
        )

    @app.exception_handler(FIBPaymentHTTPError)
    async def handle_fib_http_error(request: Request, exc: FIBPaymentHTTPError) -> JSONResponse:
        _ = request
        return JSONResponse(
            status_code=502,
            content={
                "detail": str(exc),
                "errorCode": "FIB_UPSTREAM_UNAVAILABLE",
                "errorMessage": "Unable to reach FIB payment provider.",
            },
        )

    @app.exception_handler(FIBPaymentAPIError)
    async def handle_fib_api_error(request: Request, exc: FIBPaymentAPIError) -> JSONResponse:
        _ = request
        mapped_status = exc.status_code if 400 <= exc.status_code < 600 else 502
        # BE-2: the raw provider payload can carry internal identifiers / config
        # hints — log it server-side only, never return it to the client.
        LOGGER.warning(
            "FIB API error (status=%s, code=%s): %s payload=%r",
            exc.status_code, exc.error_code, exc.error_message, exc.payload,
        )
        return JSONResponse(
            status_code=mapped_status,
            content={
                "detail": str(exc),
                "errorCode": exc.error_code or "FIB_API_ERROR",
                "errorMessage": exc.error_message or "FIB request failed.",
            },
        )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    def _run_health_db_check() -> dict[str, Any]:
        session_factory = app.state.db_session_factory
        db = session_factory()
        try:
            db.scalar(text("select 1"))
            current_revisions = list(
                db.execute(text("select version_num from alembic_version order by version_num")).scalars().all()
            )
        finally:
            db.close()
        expected_heads = _get_expected_alembic_heads()
        return {
            "status": "ok",
            "database": "ok",
            "alembic": {
                "currentRevisions": current_revisions,
                "expectedHeads": expected_heads,
                "isCurrent": bool(current_revisions) and set(current_revisions) == set(expected_heads),
            },
        }

    @app.get("/health/db")
    async def health_db() -> Any:
        timeout_seconds = _read_float_env("DATABASE_HEALTH_TIMEOUT_SECONDS", 4.0, minimum=0.5)
        try:
            return await asyncio.wait_for(asyncio.to_thread(_run_health_db_check), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            LOGGER.warning("database.health_timeout path=/health/db timeout_seconds=%.1f", timeout_seconds)
            return JSONResponse(
                status_code=503,
                content=_error_envelope(
                    status_code=503,
                    detail="Database health check timed out. Please retry shortly.",
                    error_code="DB_HEALTH_TIMEOUT",
                ),
            )

    @app.options("/api/v1/{path:path}", include_in_schema=False)
    async def options_fallback(path: str) -> Response:
        _ = path
        return Response(status_code=204)

    register_user_routes(app, get_db)
    register_auth_routes(app, get_db)
    register_esim_access_routes(app, get_db, get_provider)
    register_fib_payment_routes(app, get_fib_provider, get_db)
    register_push_notification_routes(app, get_push_provider, get_db)
    register_admin_routes(app, get_db)
    register_app_meta_routes(app, get_db)
    register_wings_routes(app, get_db)
    register_verifyway_routes(app)

    return app


app = create_app()
