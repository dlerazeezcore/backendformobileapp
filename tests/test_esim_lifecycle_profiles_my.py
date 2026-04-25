from __future__ import annotations

import os
import tempfile
import unittest
import uuid
from datetime import timedelta
from typing import Generator

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from auth import create_access_token
from config import get_settings
from esim_access_api import register_esim_access_routes
from supabase_store import AppUser, Base, CustomerOrder, ESimProfile, OrderItem, normalize_database_url, utcnow


class _LifecycleProvider:
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
                            "orderNo": "ORD-LIFECYCLE-1",
                            "transactionId": "TRX-LIFECYCLE-1",
                        },
                    }
                )
            },
        )()

    async def query_profiles(self, payload):
        _ = payload
        # Simulate delayed provider profile materialization.
        return type(
            "ProviderResponse",
            (),
            {
                "model_dump": staticmethod(
                    lambda **_: {
                        "success": True,
                        "errorCode": "0",
                        "obj": {"esimList": []},
                    }
                )
            },
        )()


class EsimLifecycleProfilesMyTest(unittest.TestCase):
    def setUp(self) -> None:
        temp_db = tempfile.NamedTemporaryFile(prefix="esim_lifecycle_", suffix=".db", delete=False)
        temp_db.close()
        self.db_path = temp_db.name
        self.user_id = str(uuid.uuid4())
        self.user_phone = "+9647707788990"
        os.environ["AUTH_SECRET_KEY"] = "test-auth-secret"
        os.environ["ESIM_ACCESS_ACCESS_CODE"] = "test-code"
        os.environ["ESIM_ACCESS_SECRET_KEY"] = "test-secret"
        get_settings.cache_clear()

        self.engine = create_engine(
            normalize_database_url(f"sqlite:///{self.db_path}"),
            connect_args={"check_same_thread": False},
            future=True,
        )
        Base.metadata.create_all(self.engine)
        self.session_factory = sessionmaker(bind=self.engine, autoflush=False, autocommit=False, future=True)

        with self.session_factory() as session:
            session.add(
                AppUser(
                    id=self.user_id,
                    phone=self.user_phone,
                    name="Lifecycle User",
                    email="lifecycle@example.com",
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

        def _get_provider() -> _LifecycleProvider:
            return _LifecycleProvider()

        register_esim_access_routes(app, _get_db, _get_provider)
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.engine.dispose()
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
        get_settings.cache_clear()

    def _user_headers(self) -> dict[str, str]:
        token = create_access_token(
            subject_id=self.user_id,
            phone=self.user_phone,
            subject_type="user",
            secret_key="test-auth-secret",
            ttl_seconds=3600,
        )
        return {"Authorization": f"Bearer {token}"}

    def test_managed_order_appears_immediately_in_inactive_with_fallback_row(self) -> None:
        order_response = self.client.post(
            "/api/v1/esim-access/orders/managed",
            headers=self._user_headers(),
            json={
                "providerRequest": {
                    "transactionId": "APP-LIFECYCLE-ORDER-1",
                    "packageInfoList": [{"packageCode": "PKG-7D", "count": 1, "price": 1000, "periodNum": 7}],
                },
                "user": {
                    "phone": self.user_phone,
                    "name": "Lifecycle User",
                    "email": "lifecycle@example.com",
                },
                "platformCode": "mobile_app",
                "currencyCode": "IQD",
                "providerCurrencyCode": "IQD",
                "customFields": {
                    "supportTopUpType": 2,
                    "countryCode": "US",
                    "countryName": "United States",
                },
            },
        )
        self.assertEqual(order_response.status_code, 200)
        order_body = order_response.json()
        self.assertTrue(order_body.get("success"))
        self.assertEqual(order_body.get("providerOrderNo"), "ORD-LIFECYCLE-1")

        profiles_response = self.client.get(
            "/api/v1/esim-access/profiles/my",
            headers=self._user_headers(),
        )
        self.assertEqual(profiles_response.status_code, 200)
        body = profiles_response.json()
        self.assertTrue(body.get("success"))
        self.assertEqual(body["data"]["total"], 1)
        profile = body["data"]["profiles"][0]
        self.assertEqual(profile["providerOrderNo"], "ORD-LIFECYCLE-1")
        self.assertEqual(profile["status"], "inactive")
        self.assertFalse(profile["installed"])
        self.assertEqual(profile["supportTopUpType"], 2)

    def test_active_but_not_installed_is_never_returned_as_active(self) -> None:
        now = utcnow()
        with self.session_factory() as session:
            order = CustomerOrder(
                user_id=self.user_id,
                order_number="ORD-LIFECYCLE-2",
                order_status="BOOKED",
                booked_at=now,
            )
            session.add(order)
            session.flush()
            item = OrderItem(
                customer_order_id=order.id,
                service_type="esim",
                provider_order_no="ORD-LIFECYCLE-2",
                item_status="ACTIVE",
                country_code="IQ",
                country_name="Iraq",
                booked_at=now,
            )
            session.add(item)
            session.flush()
            session.add(
                ESimProfile(
                    order_item_id=item.id,
                    user_id=self.user_id,
                    esim_tran_no="ESIM-LIFECYCLE-2",
                    iccid="ICCID-LIFECYCLE-2",
                    app_status="ACTIVE",
                    installed=False,
                    activated_at=now,
                    validity_days=7,
                    custom_fields={"supportTopUpType": 3},
                )
            )
            session.commit()

        response = self.client.get("/api/v1/esim-access/profiles/my", headers=self._user_headers())
        self.assertEqual(response.status_code, 200)
        profile = response.json()["data"]["profiles"][0]
        self.assertEqual(profile["status"], "inactive")
        self.assertEqual(profile["supportTopUpType"], 3)

    def test_activate_by_provider_order_no_works_for_recent_purchase_placeholder(self) -> None:
        order_response = self.client.post(
            "/api/v1/esim-access/orders/managed",
            headers=self._user_headers(),
            json={
                "providerRequest": {
                    "transactionId": "APP-LIFECYCLE-ORDER-2",
                    "packageInfoList": [{"packageCode": "PKG-30D", "count": 1, "price": 1200, "periodNum": 30}],
                },
                "user": {
                    "phone": self.user_phone,
                    "name": "Lifecycle User",
                    "email": "lifecycle@example.com",
                },
                "platformCode": "mobile_app",
                "currencyCode": "IQD",
                "providerCurrencyCode": "IQD",
            },
        )
        self.assertEqual(order_response.status_code, 200)
        provider_order_no = order_response.json().get("providerOrderNo")
        self.assertEqual(provider_order_no, "ORD-LIFECYCLE-1")

        activate_response = self.client.post(
            "/api/v1/esim-access/profiles/activate/my",
            headers=self._user_headers(),
            json={"providerOrderNo": provider_order_no},
        )
        self.assertEqual(activate_response.status_code, 200)
        activated_profile = activate_response.json()["data"]["profile"]
        self.assertTrue(activated_profile["installed"])
        self.assertEqual(activated_profile["status"], "active")
        self.assertEqual(activated_profile["providerOrderNo"], provider_order_no)

    def test_bundle_expiry_moves_profile_to_expired(self) -> None:
        now = utcnow()
        with self.session_factory() as session:
            order = CustomerOrder(
                user_id=self.user_id,
                order_number="ORD-LIFECYCLE-3",
                order_status="ACTIVE",
                booked_at=now - timedelta(days=8),
            )
            session.add(order)
            session.flush()
            item = OrderItem(
                customer_order_id=order.id,
                service_type="esim",
                provider_order_no="ORD-LIFECYCLE-3",
                item_status="ACTIVE",
                country_code="US",
                country_name="United States",
                booked_at=now - timedelta(days=8),
            )
            session.add(item)
            session.flush()
            session.add(
                ESimProfile(
                    order_item_id=item.id,
                    user_id=self.user_id,
                    esim_tran_no="ESIM-LIFECYCLE-3",
                    iccid="ICCID-LIFECYCLE-3",
                    app_status="ACTIVE",
                    installed=True,
                    installed_at=now - timedelta(days=8),
                    activated_at=now - timedelta(days=8),
                    validity_days=7,
                    # Long retention expiry; should not control app lifecycle countdown.
                    expires_at=now + timedelta(days=180),
                )
            )
            session.commit()

        response = self.client.get("/api/v1/esim-access/profiles/my", headers=self._user_headers())
        self.assertEqual(response.status_code, 200)
        profile = response.json()["data"]["profiles"][0]
        self.assertEqual(profile["status"], "expired")
        self.assertEqual(profile["daysLeft"], 0)
        self.assertIsNotNone(profile["bundleExpiresAt"])
        self.assertIsNotNone(profile["expiresAt"])

    def test_deduplicates_profile_and_fallback_rows_while_keeping_other_orders(self) -> None:
        now = utcnow()
        with self.session_factory() as session:
            order_a = CustomerOrder(
                user_id=self.user_id,
                order_number="ORD-LIFECYCLE-4A",
                order_status="ACTIVE",
                booked_at=now,
            )
            order_b = CustomerOrder(
                user_id=self.user_id,
                order_number="ORD-LIFECYCLE-4B",
                order_status="BOOKED",
                booked_at=now - timedelta(minutes=1),
            )
            session.add_all([order_a, order_b])
            session.flush()

            item_a = OrderItem(
                customer_order_id=order_a.id,
                service_type="esim",
                provider_order_no="ORD-LIFECYCLE-4A",
                item_status="ACTIVE",
                booked_at=now,
            )
            item_b = OrderItem(
                customer_order_id=order_b.id,
                service_type="esim",
                provider_order_no="ORD-LIFECYCLE-4B",
                item_status="BOOKED",
                booked_at=now - timedelta(minutes=1),
            )
            session.add_all([item_a, item_b])
            session.flush()

            session.add(
                ESimProfile(
                    order_item_id=item_a.id,
                    user_id=self.user_id,
                    esim_tran_no="ESIM-LIFECYCLE-4A",
                    iccid="ICCID-LIFECYCLE-4A",
                    app_status="ACTIVE",
                    installed=True,
                    activated_at=now,
                    validity_days=30,
                )
            )
            session.commit()

        response = self.client.get("/api/v1/esim-access/profiles/my", headers=self._user_headers())
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["data"]["total"], 2)
        provider_order_nos = {row.get("providerOrderNo") for row in payload["data"]["profiles"]}
        self.assertEqual(provider_order_nos, {"ORD-LIFECYCLE-4A", "ORD-LIFECYCLE-4B"})


if __name__ == "__main__":
    unittest.main()
