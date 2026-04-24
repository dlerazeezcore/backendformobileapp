from __future__ import annotations

import os
import unittest
import uuid
from typing import Generator

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from auth import create_access_token
from config import get_settings
from esim_access_api import ESimAccessAPIError, register_esim_access_routes
from supabase_store import AdminUser, Base, normalize_database_url


class _FailingTopUpProvider:
    async def top_up(self, payload):  # pragma: no cover - exercised by integration request path
        _ = payload
        raise ESimAccessAPIError(
            error_code="ESIM_INVALID_TRAN_NO",
            error_message="Invalid esimTranNo for selected package",
            status_code=200,
            request_id="trace-invalid-topup-1",
        )


def _get_provider() -> _FailingTopUpProvider:
    return _FailingTopUpProvider()


class ManagedTopUpErrorHandlingTest(unittest.TestCase):
    def setUp(self) -> None:
        os.environ["AUTH_SECRET_KEY"] = "test-auth-secret"
        get_settings.cache_clear()
        self.engine = create_engine(
            normalize_database_url("sqlite://"),
            future=True,
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.session_factory = sessionmaker(bind=self.engine, autoflush=False, autocommit=False, future=True)
        self.admin_id = str(uuid.uuid4())
        with self.session_factory() as session:
            session.add(
                AdminUser(
                    id=self.admin_id,
                    phone="+9647700000888",
                    name="Topup Admin",
                    status="active",
                    role="admin",
                )
            )
            session.commit()

        def _get_db() -> Generator[Session, None, None]:
            session = self.session_factory()
            try:
                yield session
            finally:
                session.close()

        app = FastAPI()
        register_esim_access_routes(app, _get_db, _get_provider)
        self.client = TestClient(app)
        self.admin_headers = {
            "Authorization": "Bearer "
            + create_access_token(
                subject_id=self.admin_id,
                phone="+9647700000888",
                subject_type="admin",
                secret_key="test-auth-secret",
                ttl_seconds=3600,
            )
        }

    def tearDown(self) -> None:
        self.engine.dispose()
        get_settings.cache_clear()

    def test_invalid_esim_tran_no_returns_json_4xx_envelope(self) -> None:
        response = self.client.post(
            "/api/v1/esim-access/topup/managed",
            json={
                "providerRequest": {
                    "packageCode": "PKG-001",
                    "transactionId": "TOPUP-TRX-001",
                    "esimTranNo": "FAKE-ESIM-TRAN-NO",
                    "iccid": "8986000000000000000",
                },
                "platformCode": "tulip_mobile_app",
                "platformName": "Tulip Mobile App",
                "actorPhone": "+9647500000000",
                "syncAfterTopup": False,
            },
            headers=self.admin_headers,
        )

        self.assertGreaterEqual(response.status_code, 400)
        self.assertLess(response.status_code, 500)
        self.assertNotEqual(response.status_code, 502)
        self.assertEqual(response.headers["content-type"].split(";")[0], "application/json")

        body = response.json()
        self.assertEqual(body.get("success"), False)
        self.assertEqual(body.get("errorCode"), "ESIM_INVALID_TRAN_NO")
        self.assertTrue(body.get("message"))
        self.assertIn("invalid", (body.get("providerMessage") or "").lower())
        self.assertEqual(body.get("requestId"), "trace-invalid-topup-1")
        self.assertEqual(body.get("traceId"), "trace-invalid-topup-1")


if __name__ == "__main__":
    unittest.main()
