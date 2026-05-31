"""Tests for provider waiting and install-gated eSIM activation.

Covers the three call sites where the provider can tell us the eSIM has
started its first connection:

1. sync_profiles  (provider query response)
2. record_webhook (provider push)
3. /packages/query filtering for 1-day unlimited

Plus the recover endpoint's expanded response shape.
"""
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
from esim_access_api import (
    _augment_package_list_response,
    _is_daily_unlimited_package,
    register_esim_access_routes,
)
from supabase_store import (
    AppUser,
    Base,
    CustomerOrder,
    ESimProfile,
    OrderItem,
    SupabaseStore,
    normalize_database_url,
    normalize_esim_status,
    utcnow,
)


class _OnboardingProvider:
    """Test double for ESimAccessAPI. Returns ONBOARDING for query_profiles
    and a canned package list for get_packages.
    """

    def __init__(self) -> None:
        self.last_query = None

    async def query_profiles(self, payload):
        self.last_query = payload
        return type(
            "Resp",
            (),
            {
                "model_dump": staticmethod(
                    lambda **_: {
                        "success": True,
                        "errorCode": "0",
                        "obj": {
                            "esimList": [
                                {
                                    "orderNo": "ORD-ONBOARD-1",
                                    "esimTranNo": "ESIM-ONBOARD-1",
                                    "iccid": "ICCID-ONBOARD-1",
                                    "ac": "LPA:1$smdp.example$AC-CODE",
                                    "qrCodeUrl": "https://example.com/q.png",
                                    "shortUrl": "https://example.com/i",
                                    "smdpStatus": "RELEASED",
                                    "esimStatus": "ONBOARDING",
                                    "totalDuration": 7,
                                    "totalVolume": 1024 * 1024 * 1024,  # 1 GB in bytes
                                }
                            ]
                        },
                    }
                )
            },
        )()

    async def get_packages(self, payload):
        return type(
            "Resp",
            (),
            {
                "model_dump": staticmethod(
                    lambda **_: {
                        "success": True,
                        "errorCode": "0",
                        "obj": {
                            "packageList": [
                                # 1-day unlimited — must be dropped from catalog.
                                {
                                    "packageCode": "TEST_1D_UL",
                                    "name": "Daily Unlimited",
                                    "validityDays": 1,
                                    "totalDataMb": None,
                                },
                                # 7-day unlimited — must stay.
                                {
                                    "packageCode": "TEST_7D_UL",
                                    "name": "Weekly Unlimited",
                                    "validityDays": 7,
                                    "totalDataMb": None,
                                },
                                # 1-day capped — must stay.
                                {
                                    "packageCode": "TEST_1D_500MB",
                                    "name": "Daily 500MB",
                                    "validityDays": 1,
                                    "totalDataMb": 500,
                                },
                                # 30-day capped — must stay.
                                {
                                    "packageCode": "TEST_30D_5GB",
                                    "name": "Monthly 5 GB",
                                    "validityDays": 30,
                                    "totalDataMb": 5 * 1024,
                                },
                            ]
                        },
                    }
                )
            },
        )()


class EsimOnboardingAutoActivateTest(unittest.TestCase):
    def setUp(self) -> None:
        temp_db = tempfile.NamedTemporaryFile(prefix="esim_onboard_", suffix=".db", delete=False)
        temp_db.close()
        self.db_path = temp_db.name
        self.user_id = str(uuid.uuid4())
        self.user_phone = "+9647707788991"
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
                    name="Onboard User",
                    email="onboard@example.com",
                    status="active",
                )
            )
            session.commit()

        app = FastAPI()
        self.provider = _OnboardingProvider()

        def _get_db() -> Generator[Session, None, None]:
            session = self.session_factory()
            try:
                yield session
            finally:
                session.close()

        def _get_provider() -> _OnboardingProvider:
            return self.provider

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

    def _seed_profile(self, *, installed: bool = False) -> int:
        """Insert a fresh profile placeholder and return its id."""
        now = utcnow()
        with self.session_factory() as session:
            order = CustomerOrder(
                user_id=self.user_id,
                order_number="ORD-ONBOARD-1",
                order_status="BOOKED",
                booked_at=now,
            )
            session.add(order)
            session.flush()
            item = OrderItem(
                customer_order_id=order.id,
                service_type="esim",
                provider_order_no="ORD-ONBOARD-1",
                item_status="BOOKED",
                country_code="IQ",
                country_name="Iraq",
                booked_at=now,
            )
            session.add(item)
            session.flush()
            profile = ESimProfile(
                order_item_id=item.id,
                user_id=self.user_id,
                app_status="INACTIVE",
                installed=installed,
                installed_at=now if installed else None,
                validity_days=7,
            )
            session.add(profile)
            session.commit()
            return int(profile.id)

    # --- normalization -----------------------------------------------------

    def test_normalize_onboarding_to_active(self) -> None:
        self.assertEqual(normalize_esim_status("ONBOARDING"), "ACTIVE")
        self.assertEqual(normalize_esim_status("onboarding"), "ACTIVE")
        self.assertEqual(normalize_esim_status("IN_USE"), "ACTIVE")

    # --- sync_profiles + install-gated side-effects -------------------------

    def test_sync_profiles_with_onboarding_waits_until_install(self) -> None:
        profile_id = self._seed_profile()
        with self.session_factory() as session:
            store = SupabaseStore(session)
            store.sync_profiles(
                {
                    "obj": {
                        "esimList": [
                            {
                                "orderNo": "ORD-ONBOARD-1",
                                "esimTranNo": "ESIM-ONBOARD-1",
                                "iccid": "ICCID-ONBOARD-1",
                                "ac": "LPA:1$smdp.example$AC-CODE",
                                "smdpStatus": "RELEASED",
                                "esimStatus": "ONBOARDING",
                                "totalDuration": 7,
                            }
                        ]
                    }
                }
            )
            session.commit()
            row = session.get(ESimProfile, profile_id)
            self.assertEqual(row.app_status, "PROVIDER_WAITING")
            self.assertFalse(row.installed)
            self.assertIsNone(row.installed_at)
            self.assertIsNone(row.activated_at)
            self.assertIsNone(row.expires_at)
            self.assertEqual(row.order_item.item_status, "PROVIDER_WAITING")
            self.assertEqual(row.order_item.customer_order.order_status, "PROVIDER_WAITING")

    def test_installed_profile_with_onboarding_becomes_active(self) -> None:
        profile_id = self._seed_profile(installed=True)
        with self.session_factory() as session:
            store = SupabaseStore(session)
            store.sync_profiles(
                {
                    "obj": {
                        "esimList": [
                            {
                                "orderNo": "ORD-ONBOARD-1",
                                "esimTranNo": "ESIM-ONBOARD-1",
                                "iccid": "ICCID-ONBOARD-1",
                                "ac": "LPA:1$smdp.example$AC-CODE",
                                "smdpStatus": "RELEASED",
                                "esimStatus": "ONBOARDING",
                                "totalDuration": 7,
                            }
                        ]
                    }
                }
            )
            session.commit()
            row = session.get(ESimProfile, profile_id)
            self.assertEqual(row.app_status, "ACTIVE")
            self.assertTrue(row.installed)
            self.assertIsNotNone(row.installed_at)
            self.assertIsNotNone(row.activated_at)
            self.assertIsNotNone(row.expires_at)
            self.assertEqual(row.order_item.item_status, "ACTIVE")
            self.assertEqual(row.order_item.customer_order.order_status, "ACTIVE")

    # --- recover endpoint response -----------------------------------------

    def test_recover_endpoint_returns_provider_waiting_until_install(self) -> None:
        profile_id = self._seed_profile()
        response = self.client.post(
            f"/api/v1/esim-access/profiles/{profile_id}/recover",
            headers=self._user_headers(),
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["appStatus"], "PROVIDER_WAITING")
        self.assertFalse(body["installed"])
        self.assertIsNone(body["activatedAt"])
        self.assertTrue(body["hasActivationCode"])
        self.assertTrue(body["hasIccid"])

    # --- webhook auto-activation -------------------------------------------

    def test_webhook_with_onboarding_waits_until_install(self) -> None:
        profile_id = self._seed_profile()
        # The profile needs an iccid so the webhook can locate it without
        # going through provider lookup.
        with self.session_factory() as session:
            row = session.get(ESimProfile, profile_id)
            row.iccid = "ICCID-ONBOARD-1"
            row.esim_tran_no = "ESIM-ONBOARD-1"
            session.commit()

            store = SupabaseStore(session)
            store.record_webhook(
                {
                    "notifyType": "profileStatusChange",
                    "notifyId": "evt-onboard-1",
                    "content": {
                        "orderNo": "ORD-ONBOARD-1",
                        "esimTranNo": "ESIM-ONBOARD-1",
                        "iccid": "ICCID-ONBOARD-1",
                        "smdpStatus": "RELEASED",
                        "esimStatus": "ONBOARDING",
                    },
                }
            )
            session.commit()
            row = session.get(ESimProfile, profile_id)
            self.assertEqual(row.app_status, "PROVIDER_WAITING")
            self.assertFalse(row.installed)
            self.assertIsNone(row.activated_at)

    # --- package filter ----------------------------------------------------

    def test_is_daily_unlimited_detection(self) -> None:
        self.assertTrue(_is_daily_unlimited_package({"validityDays": 1, "totalDataMb": None}))
        self.assertTrue(_is_daily_unlimited_package({"validityDays": 1}))
        self.assertTrue(_is_daily_unlimited_package({"duration": 1, "totalVolume": 0}))
        self.assertFalse(_is_daily_unlimited_package({"validityDays": 7, "totalDataMb": None}))
        self.assertFalse(_is_daily_unlimited_package({"validityDays": 1, "totalDataMb": 500}))
        self.assertFalse(_is_daily_unlimited_package({"validityDays": 30, "totalDataMb": 5120}))

    def test_packages_query_drops_daily_unlimited(self) -> None:
        response = self.client.post(
            "/api/v1/esim-access/packages/query",
            json={"locationCode": "US"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        codes = {p["packageCode"] for p in response.json()["obj"]["packageList"]}
        self.assertNotIn("TEST_1D_UL", codes)
        self.assertIn("TEST_7D_UL", codes)
        self.assertIn("TEST_1D_500MB", codes)
        self.assertIn("TEST_30D_5GB", codes)

    def test_packages_query_does_not_drop_for_topup(self) -> None:
        # Top-up queries (type=TOPUP + iccid) must keep every package the
        # provider returns — even if some happen to look like daily-unlimited.
        response = self.client.post(
            "/api/v1/esim-access/packages/query",
            json={"type": "TOPUP", "iccid": "ICCID-ONBOARD-1"},
        )
        self.assertEqual(response.status_code, 200)
        codes = {p["packageCode"] for p in response.json()["obj"]["packageList"]}
        self.assertIn("TEST_1D_UL", codes)
        self.assertIn("TEST_7D_UL", codes)

    # --- regression: explicit catalog augment helper -----------------------

    def test_augment_helper_filter_off_by_default(self) -> None:
        payload = {
            "obj": {
                "packageList": [
                    {"packageCode": "K_1D_UL", "validityDays": 1, "totalDataMb": None},
                    {"packageCode": "K_7D_UL", "validityDays": 7, "totalDataMb": None},
                ]
            }
        }
        out = _augment_package_list_response(payload)
        codes = {p["packageCode"] for p in out["obj"]["packageList"]}
        self.assertIn("K_1D_UL", codes)  # filter disabled by default
        self.assertIn("K_7D_UL", codes)

        out = _augment_package_list_response(payload, drop_daily_unlimited=True)
        codes = {p["packageCode"] for p in out["obj"]["packageList"]}
        self.assertNotIn("K_1D_UL", codes)
        self.assertIn("K_7D_UL", codes)


if __name__ == "__main__":
    unittest.main()
