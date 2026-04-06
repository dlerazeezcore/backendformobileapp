from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from admin import register_admin_routes
from auth import register_auth_routes
from config import (
    DEFAULT_ESIM_ACCESS_BASE_URL,
    DEFAULT_ESIM_ACCESS_RATE_LIMIT_PER_SECOND,
    DEFAULT_ESIM_ACCESS_TIMEOUT_SECONDS,
    Settings,
    get_settings,
)
from dependencies import get_db, get_provider
from esim_access_api import (
    ESimAccessAPI,
    ESimAccessAPIError,
    ESimAccessHTTPError,
    register_esim_access_routes,
)
from supabase_store import create_database
from users import register_user_routes


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
        yield
        await app.state.esim_access_api.close()

    app = FastAPI(title="backendformobileapp", version="0.1.0", lifespan=lifespan)

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

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    register_user_routes(app, get_db)
    register_auth_routes(app, get_db)
    register_esim_access_routes(app, get_db, get_provider)
    register_admin_routes(app, get_db)

    return app


app = create_app()
