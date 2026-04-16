from __future__ import annotations

from contextlib import asynccontextmanager
import inspect
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response

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

CORS_ALLOWED_ORIGINS = [
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
CORS_ALLOWED_HEADERS = ["Authorization", "Content-Type"]
CORS_ALLOW_ORIGIN_REGEX = r"^https://([a-zA-Z0-9-]+\.)?figma\.site$"
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


def create_app(settings: Settings | None = None) -> FastAPI:
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
        if cfg.fib_payment_client_id and cfg.fib_payment_client_secret:
            app.state.fib_payment_api = FIBPaymentAPI(
                client_id=cfg.fib_payment_client_id,
                client_secret=cfg.fib_payment_client_secret,
                base_url=FIB_PAYMENT_BASE_URL,
                timeout=FIB_PAYMENT_TIMEOUT_SECONDS,
                rate_limit_per_second=FIB_PAYMENT_RATE_LIMIT_PER_SECOND,
                default_status_callback_url=FIB_PAYMENT_STATUS_CALLBACK_URL,
                default_redirect_uri=FIB_PAYMENT_REDIRECT_URI,
                webhook_secret=cfg.fib_payment_webhook_secret or FIB_PAYMENT_WEBHOOK_SECRET,
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

    app = FastAPI(title="backendformobileapp", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=CORS_ALLOWED_ORIGINS,
        allow_origin_regex=CORS_ALLOW_ORIGIN_REGEX,
        allow_credentials=True,
        allow_methods=CORS_ALLOWED_METHODS,
        allow_headers=CORS_ALLOWED_HEADERS,
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
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    @app.exception_handler(ESimAccessHTTPError)
    async def handle_http_error(request: Request, exc: ESimAccessHTTPError) -> JSONResponse:
        _ = request
        return JSONResponse(status_code=502, content={"detail": str(exc)})

    @app.exception_handler(ESimAccessAPIError)
    async def handle_api_error(request: Request, exc: ESimAccessAPIError) -> JSONResponse:
        _ = request
        return JSONResponse(
            status_code=502,
            content={"detail": str(exc), "errorCode": exc.error_code, "errorMessage": exc.error_message},
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

    return app


app = create_app()
