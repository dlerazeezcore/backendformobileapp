from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request, status

from esim_access_api import ESimAccessAPI
from fib_payment_api import FIBPaymentAPI


def get_provider(request: Request) -> ESimAccessAPI:
    return request.app.state.esim_access_api


def get_db(request: Request) -> Any:
    session_factory = request.app.state.db_session_factory
    session = session_factory()
    try:
        yield session
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
