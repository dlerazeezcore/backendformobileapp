from __future__ import annotations

import os
import tempfile
import unittest
import uuid
from typing import Generator

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from auth import create_access_token
from config import get_settings
from esim_access_api import register_esim_access_routes
from supabase_store import AppUser, Base, CustomerOrder, normalize_database_url


class _ManagedOrderProvider:
    async def order_profiles(self, payload):
        _ = payload
        return type(
            "ProviderResponse",
            (),
            {
                "model_dump": staticmethod(
                    lambda **_: {
                        "success": True,
                        "errorCode": "0",
                        "obj": {
                            "orderNo": "ORD-PROVIDER-1",
                            "transactionId": "TRX-PROVIDER-1",
                        },
                    }
                )
            },
        )()


class ManagedOrderAuthBindingTest(unittest.TestCase):
    def setUp(self) -> None:
        temp_db = tempfile.NamedTemporaryFile(prefix="managed_order_", suffix=".db", delete=False)
        temp_db.close()
        self.db_path = temp_db.name
        self.user_id = str(uuid.uuid4())
        self.user_phone = "+9647701230001"
        os.environ["AUTH_SECRET_KEY"] = "test-auth-secret"
        get_settings.cache_clear()

        engine = create_engine(
            normalize_database_url(f"sqlite:///{self.db_path}"),
            connect_args={"check_same_thread": False},
            future=True,
        )
        Base.metadata.create_all(engine)
        self.session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
        with self.session_factory() as session:
            session.add(
                AppUser(
                    id=self.user_id,
                    phone=self.user_phone,
                    name="Token User",
                    email="token-user@example.com",
                    status="active",
                )
            )
            session.commit()

        app = FastAPI()

        def _get_db() -> Generator[Session, None, None]:
            session = self.session_factory()
            try:
                yield session
            finally:
                session.close()

        def _get_provider() -> _ManagedOrderProvider:
            return _ManagedOrderProvider()

        register_esim_access_routes(app, _get_db, _get_provider)
        self.client = TestClient(app)
        self.engine = engine

    def tearDown(self) -> None:
        self.engine.dispose()
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
        get_settings.cache_clear()

    def _user_auth_header(self) -> dict[str, str]:
        token = create_access_token(
            subject_id=self.user_id,
            phone=self.user_phone,
            subject_type="user",
            secret_key="test-auth-secret",
            ttl_seconds=3600,
        )
        return {"Authorization": f"Bearer {token}"}

    def test_managed_order_requires_authenticated_user(self) -> None:
        response = self.client.post(
            "/api/v1/esim-access/orders/managed",
            json={
                "providerRequest": {
                    "transactionId": "APP-ORDER-10001",
                    "packageInfoList": [{"packageCode": "PKG-001", "count": 1, "price": 1000}],
                },
                "user": {"phone": "+9647700000000", "name": "Payload User"},
                "platformCode": "mobile_app",
            },
        )
        self.assertEqual(response.status_code, 401)

    def test_managed_order_uses_token_owner_not_payload_user(self) -> None:
        response = self.client.post(
            "/api/v1/esim-access/orders/managed",
            headers=self._user_auth_header(),
            json={
                "providerRequest": {
                    "transactionId": "APP-ORDER-20001",
                    "packageInfoList": [{"packageCode": "PKG-001", "count": 1, "price": 1000}],
                },
                "user": {
                    "phone": "+9647711111111",
                    "name": "Spoofed Payload User",
                    "email": "spoof@example.com",
                },
                "platformCode": "mobile_app",
                "currencyCode": "IQD",
                "providerCurrencyCode": "IQD",
            },
        )
        self.assertEqual(response.status_code, 200)
        order_id = response.json()["database"]["customerOrderId"]
        with self.session_factory() as session:
            order = session.scalar(select(CustomerOrder).where(CustomerOrder.id == order_id))
            self.assertIsNotNone(order)
            assert order is not None
            self.assertEqual(order.user_id, self.user_id)


if __name__ == "__main__":
    unittest.main()
