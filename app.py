from __future__ import annotations

from contextlib import asynccontextmanager
import inspect
import logging
import os
from pathlib import Path
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
from auth import register_auth_routes
from config import (
    DEFAULT_ESIM_ACCESS_BASE_URL,
    DEFAULT_ESIM_ACCESS_RATE_LIMIT_PER_SECOND,
    DEFAULT_ESIM_ACCESS_TIMEOUT_SECONDS,
    Settings,
    get_settings,
)
from dependencies import get_db, get_fib_provider, get_provider, get_push_provider, get_twilio_provider
from esim_access_api import (
    ESimAccessAPI,
    ESimAccessAPIError,
    ESimAccessHTTPError,
    register_esim_access_routes,
)
from fib_payment_api import (
    FIBPaymentAPI,
    FIBPaymentAPIError,
    FIBPaymentHTTPError,
    register_fib_payment_routes,
)
from push_notification import PushNotificationService, register_push_notification_routes
from telegram_support import register_telegram_support_routes
from supabase_store import create_database
from twilio_whatsapp import TwilioVerifyAPIError, TwilioVerifyHTTPError, TwilioWhatsAppVerifyAPI
from users import register_user_routes
from wings_api import register_wings_routes

DEFAULT_CORS_ALLOWED_ORIGINS = [
    "capacitor://localhost",
    "ionic://localhost",
    "http://localhost",
    "https://localhost",
    "https://www.figma.com",
    "https://figma.com",
    "https://makeproxy-m.figma.site",
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
]
CORS_ALLOWED_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
CORS_ALLOWED_HEADERS = ["*"]
DEFAULT_CORS_ALLOW_ORIGIN_REGEX = (
    r"^(?:"
    r"https://([a-zA-Z0-9-]+\.)?"
    r"(figma\.site|koyeb\.app|vercel\.app|netlify\.app|pages\.dev|web\.app|firebaseapp\.com)"
    r"|https?://(?:localhost|127\.0\.0\.1|10(?:\.\d{1,3}){3}|192\.168(?:\.\d{1,3}){2}|172\.(?:1[6-9]|2\d|3[0-1])(?:\.\d{1,3}){2})(?::\d+)?"
    r"|capacitor://localhost|ionic://localhost"
    r")$"
)
FIB_PAYMENT_BASE_URL = "https://fib.prod.fib.iq"
FIB_PAYMENT_TIMEOUT_SECONDS = 30.0
FIB_PAYMENT_RATE_LIMIT_PER_SECOND = 8.0
FIB_PAYMENT_STATUS_CALLBACK_URL = "https://mean-lettie-corevia-0bd7cc91.koyeb.app/api/v1/payments/fib/webhook"
FIB_PAYMENT_REDIRECT_URI = "tulip://payment/result"
PUSH_NOTIFICATION_DEFAULT_CHANNEL_ID = "general"
TWILIO_VERIFY_BASE_URL = "https://verify.twilio.com"
TWILIO_VERIFY_TIMEOUT_SECONDS = 20.0
TWILIO_VERIFY_RATE_LIMIT_PER_SECOND = 5.0
# Optional hardcoded fallback when env var is not set.
FIB_PAYMENT_WEBHOOK_SECRET: str | None = None
LOGGER = logging.getLogger("uvicorn.error")


def _split_env_csv(value: str | None) -> list[str]:
    if value is None:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _read_bool_env(name: str, default: bool) -> bool:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    normalized = raw_value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def _get_cors_allowed_origins() -> list[str]:
    configured = _split_env_csv(os.getenv("CORS_ALLOWED_ORIGINS"))
    if configured:
        return configured
    return DEFAULT_CORS_ALLOWED_ORIGINS


def _get_cors_allow_origin_regex() -> str | None:
    configured = os.getenv("CORS_ALLOW_ORIGIN_REGEX")
    if configured is not None:
        stripped = configured.strip()
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

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        cfg = settings or get_settings()
        app.state.db_session_factory = create_database(cfg.database_url)
        app.state.esim_access_api = ESimAccessAPI(
            access_code=cfg.esim_access_access_code,
            secret_key=cfg.esim_access_secret_key,
            base_url=DEFAULT_ESIM_ACCESS_BASE_URL,
            timeout=DEFAULT_ESIM_ACCESS_TIMEOUT_SECONDS,
            rate_limit_per_second=DEFAULT_ESIM_ACCESS_RATE_LIMIT_PER_SECOND,
        )
        app.state.fib_payment_api = None
        fib_webhook_secret = cfg.fib_payment_webhook_secret or FIB_PAYMENT_WEBHOOK_SECRET
        if cfg.fib_payment_client_id and cfg.fib_payment_client_secret and fib_webhook_secret:
            app.state.fib_payment_api = FIBPaymentAPI(
                client_id=cfg.fib_payment_client_id,
                client_secret=cfg.fib_payment_client_secret,
                base_url=FIB_PAYMENT_BASE_URL,
                timeout=FIB_PAYMENT_TIMEOUT_SECONDS,
                rate_limit_per_second=FIB_PAYMENT_RATE_LIMIT_PER_SECOND,
                default_status_callback_url=FIB_PAYMENT_STATUS_CALLBACK_URL,
                default_redirect_uri=FIB_PAYMENT_REDIRECT_URI,
                webhook_secret=fib_webhook_secret,
            )
        elif cfg.fib_payment_client_id and cfg.fib_payment_client_secret:
            LOGGER.warning(
                "FIB payment credentials are set but FIB_PAYMENT_WEBHOOK_SECRET is missing; "
                "FIB integration will remain disabled until a webhook secret is configured."
            )
        app.state.push_notification_service = PushNotificationService(
            service_account_file=cfg.firebase_service_account_file,
            service_account_json=cfg.firebase_service_account_json,
            default_channel_id=cfg.push_notification_default_channel_id or PUSH_NOTIFICATION_DEFAULT_CHANNEL_ID,
        )
        app.state.twilio_whatsapp_api = None
        if cfg.twilio_account_sid and cfg.twilio_auth_token and cfg.twilio_verify_service_sid:
            app.state.twilio_whatsapp_api = TwilioWhatsAppVerifyAPI(
                account_sid=cfg.twilio_account_sid,
                auth_token=cfg.twilio_auth_token,
                verify_service_sid=cfg.twilio_verify_service_sid,
                base_url=TWILIO_VERIFY_BASE_URL,
                timeout=TWILIO_VERIFY_TIMEOUT_SECONDS,
                rate_limit_per_second=TWILIO_VERIFY_RATE_LIMIT_PER_SECOND,
            )
        yield
        await app.state.esim_access_api.close()
        if app.state.fib_payment_api is not None:
            await app.state.fib_payment_api.close()
        twilio_provider = getattr(app.state, "twilio_whatsapp_api", None)
        if twilio_provider is not None:
            close_method = getattr(twilio_provider, "close", None)
            if callable(close_method):
                close_result = close_method()
                if inspect.isawaitable(close_result):
                    await close_result
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
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_envelope(status_code=exc.status_code, detail=exc.detail),
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
        return JSONResponse(
            status_code=mapped_status,
            content={
                "detail": str(exc),
                "errorCode": exc.error_code or "FIB_API_ERROR",
                "errorMessage": exc.error_message or "FIB request failed.",
                "providerPayload": exc.payload or {},
            },
        )

    @app.exception_handler(TwilioVerifyHTTPError)
    async def handle_twilio_http_error(request: Request, exc: TwilioVerifyHTTPError) -> JSONResponse:
        _ = request
        return JSONResponse(
            status_code=502,
            content={
                "detail": str(exc),
                "errorCode": "TWILIO_VERIFY_UPSTREAM_UNAVAILABLE",
                "errorMessage": "Unable to reach Twilio Verify provider.",
            },
        )

    @app.exception_handler(TwilioVerifyAPIError)
    async def handle_twilio_api_error(request: Request, exc: TwilioVerifyAPIError) -> JSONResponse:
        _ = request
        mapped_status = exc.status_code if 400 <= exc.status_code < 600 else 502
        return JSONResponse(
            status_code=mapped_status,
            content={
                "detail": str(exc),
                "errorCode": exc.error_code or "TWILIO_VERIFY_ERROR",
                "errorMessage": exc.error_message or "Twilio Verify request failed.",
                "providerPayload": exc.payload or {},
            },
        )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/db")
    def health_db() -> dict[str, Any]:
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

    @app.options("/api/v1/{path:path}", include_in_schema=False)
    async def options_fallback(path: str) -> Response:
        _ = path
        return Response(status_code=204)

    register_user_routes(app, get_db)
    register_auth_routes(app, get_db, get_twilio_provider)
    register_esim_access_routes(app, get_db, get_provider)
    register_fib_payment_routes(app, get_fib_provider, get_db)
    register_push_notification_routes(app, get_push_provider, get_db)
    register_telegram_support_routes(app, get_db, get_push_provider)
    register_admin_routes(app, get_db)
    register_wings_routes(app)

    return app


app = create_app()
