from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response

from admin import register_admin_routes
from auth import register_auth_routes
from config import (
    DEFAULT_ESIM_ACCESS_BASE_URL,
    DEFAULT_ESIM_ACCESS_RATE_LIMIT_PER_SECOND,
    DEFAULT_ESIM_ACCESS_TIMEOUT_SECONDS,
    DEFAULT_FIB_PAYMENT_RATE_LIMIT_PER_SECOND,
    DEFAULT_FIB_PAYMENT_TIMEOUT_SECONDS,
    Settings,
    get_settings,
)
from dependencies import get_db, get_fib_provider, get_provider
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
from supabase_store import create_database
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
CORS_ALLOWED_HEADERS = ["Authorization", "Content-Type", "Accept", "Origin", "X-Requested-With"]
CORS_ALLOW_ORIGIN_REGEX = r"^https://([a-zA-Z0-9-]+\.)?figma\.site$"


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
                base_url=cfg.fib_payment_base_url,
                timeout=cfg.fib_payment_timeout_seconds or DEFAULT_FIB_PAYMENT_TIMEOUT_SECONDS,
                rate_limit_per_second=cfg.fib_payment_rate_limit_per_second or DEFAULT_FIB_PAYMENT_RATE_LIMIT_PER_SECOND,
                default_status_callback_url=cfg.fib_payment_status_callback_url,
                default_redirect_uri=cfg.fib_payment_redirect_uri,
                webhook_secret=cfg.fib_payment_webhook_secret,
            )
        yield
        await app.state.esim_access_api.close()
        if app.state.fib_payment_api is not None:
            await app.state.fib_payment_api.close()

    app = FastAPI(title="backendformobileapp", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=CORS_ALLOWED_ORIGINS,
        allow_origin_regex=CORS_ALLOW_ORIGIN_REGEX,
        allow_credentials=True,
        allow_methods=CORS_ALLOWED_METHODS,
        allow_headers=CORS_ALLOWED_HEADERS,
    )

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

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.options("/api/v1/{path:path}", include_in_schema=False)
    async def options_fallback(path: str) -> Response:
        _ = path
        return Response(status_code=204)

    register_user_routes(app, get_db)
    register_auth_routes(app, get_db)
    register_esim_access_routes(app, get_db, get_provider)
    register_fib_payment_routes(app, get_fib_provider)
    register_admin_routes(app, get_db)

    return app


app = create_app()
