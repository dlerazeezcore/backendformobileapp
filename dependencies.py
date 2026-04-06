from __future__ import annotations

from typing import Any

from fastapi import Request

from esim_access_api import ESimAccessAPI


def get_provider(request: Request) -> ESimAccessAPI:
    return request.app.state.esim_access_api


def get_db(request: Request) -> Any:
    session_factory = request.app.state.db_session_factory
    session = session_factory()
    try:
        yield session
    finally:
        session.close()
