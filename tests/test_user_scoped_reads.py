from __future__ import annotations

from datetime import timedelta
import os
import tempfile
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import sessionmaker

from app import create_app
from auth import create_access_token, hash_password
from config import get_settings
from supabase_store import (
    AdminUser,
    AppUser,
    Base,
    CustomerOrder,
    ESimProfile,
    ExchangeRate,
    OrderItem,
    normalize_database_url,
    utcnow,
)


class UserScopedReadsTest(unittest.TestCase):
    def setUp(self) -> None:
        temp_db = tempfile.NamedTemporaryFile(prefix="user_scoped_reads_", suffix=".db", delete=False)
        temp_db.close()
        self.db_path = temp_db.name
        os.environ["ESIM_ACCESS_ACCESS_CODE"] = "test-code"
        os.environ["ESIM_ACCESS_SECRET_KEY"] = "test-secret"
        os.environ["DATABASE_URL"] = f"sqlite:///{self.db_path}"
        os.environ["AUTH_SECRET_KEY"] = "test-auth-secret"
        get_settings.cache_clear()

        self.engine = create_engine(
            normalize_database_url(os.environ["DATABASE_URL"]),
            connect_args={"check_same_thread": False},
            future=True,
        )
        Base.metadata.create_all(self.engine)
        self.session_factory = sessionmaker(bind=self.engine, autoflush=False, autocommit=False, future=True)

        now = utcnow()
        with self.session_factory() as session:
            session.add(
                AdminUser(
                    id="11111111-1111-1111-1111-111111111111",
                    phone="+9647700000001",
                    name="Admin",
                    status="active",
                    role="admin",
                    can_manage_users=True,
                    can_manage_orders=True,
                    can_manage_pricing=True,
                    can_manage_content=True,
                    can_send_push=True,
                    password_hash=hash_password("StrongPass123"),
                )
            )
            session.add_all(
                [
                    AppUser(
                        id="22222222-2222-2222-2222-222222222222",
                        phone="+9647700000002",
                        name="User One",
                        status="active",
                    ),
                    AppUser(
                        id="33333333-3333-3333-3333-333333333333",
                        phone="+9647700000003",
                        name="User Two",
                        status="active",
                    ),
                ]
            )

            order_1 = CustomerOrder(
                user_id="22222222-2222-2222-2222-222222222222",
                order_number="ORD-USER1-0001",
                order_status="BOOKED",
            )
            order_2 = CustomerOrder(
                user_id="33333333-3333-3333-3333-333333333333",
                order_number="ORD-USER2-0001",
                order_status="BOOKED",
            )
            session.add_all([order_1, order_2])
            session.flush()

            item_1 = OrderItem(
                customer_order_id=order_1.id,
                country_code="IQ",
                country_name="Iraq",
                item_status="ACTIVE",
                service_type="esim",
            )
            item_2 = OrderItem(
                customer_order_id=order_2.id,
                country_code="US",
                country_name="United States",
                item_status="ACTIVE",
                service_type="esim",
            )
            session.add_all([item_1, item_2])
            session.flush()

            session.add_all(
                [
                    ESimProfile(
                        order_item_id=item_1.id,
                        user_id="22222222-2222-2222-2222-222222222222",
                        esim_tran_no="T-USER1",
                        iccid="ICCID-USER1",
                        app_status="ACTIVE",
                        installed=True,
                        total_data_mb=102400,
                        used_data_mb=2048,
                        remaining_data_mb=98304,
                        validity_days=30,
                        installed_at=now,
                        activated_at=now,
                        expires_at=now,
                        activation_code="ACT-USER1",
                        install_url="https://install.example/1",
                        custom_fields={"source": "test", "usageUnit": "KB"},
                    ),
                    ESimProfile(
                        order_item_id=item_2.id,
                        user_id="33333333-3333-3333-3333-333333333333",
                        esim_tran_no="T-USER2",
                        iccid="ICCID-USER2",
                        app_status="ACTIVE",
                        installed=False,
                        total_data_mb=2048,
                        used_data_mb=100,
                        remaining_data_mb=1948,
                    ),
                ]
            )

            session.add(
                ExchangeRate(
                    base_currency="USD",
                    quote_currency="IQD",
                    rate=1500.0,
                    source="tulip-admin",
                    active=True,
                    effective_at=now,
                    custom_fields={"enableIQD": True, "markupPercent": "10"},
                )
            )
            session.commit()

    def tearDown(self) -> None:
        self.engine.dispose()
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
        get_settings.cache_clear()

    def _token(self, *, subject_id: str, phone: str, subject_type: str) -> str:
        return create_access_token(
            subject_id=subject_id,
            phone=phone,
            subject_type=subject_type,
            secret_key="test-auth-secret",
            ttl_seconds=3600,
        )

    def test_profiles_my_user_token_returns_only_own_profiles(self) -> None:
        token = self._token(
            subject_id="22222222-2222-2222-2222-222222222222",
            phone="+9647700000002",
            subject_type="user",
        )
        with TestClient(create_app()) as client:
            response = client.get("/api/v1/esim-access/profiles/my", headers={"Authorization": f"Bearer {token}"})
            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertTrue(payload.get("success"))
            data = payload["data"]
            self.assertEqual(data["total"], 1)
            self.assertEqual(len(data["profiles"]), 1)
            profile = data["profiles"][0]
            self.assertEqual(profile["userId"], "22222222-2222-2222-2222-222222222222")
            self.assertEqual(profile["iccid"], "ICCID-USER1")
            self.assertEqual(profile["countryCode"], "IQ")
            self.assertEqual(profile["countryName"], "Iraq")
            self.assertEqual(profile["totalDataMb"], 100)
            self.assertEqual(profile["packageDataMb"], 100)
            self.assertEqual(profile["usedDataMb"], 2)
            self.assertEqual(profile["remainingDataMb"], 96)
            self.assertEqual(profile["dataUnit"], "MB")
            self.assertEqual(profile["usageUnit"], "MB")
            self.assertEqual(profile["totalDataGb"], round(profile["totalDataMb"] / 1024, 6))
            self.assertEqual(profile["usedDataGb"], round(profile["usedDataMb"] / 1024, 6))
            self.assertEqual(profile["remainingDataGb"], round(profile["remainingDataMb"] / 1024, 6))

    def test_profiles_my_supports_filters(self) -> None:
        token = self._token(
            subject_id="22222222-2222-2222-2222-222222222222",
            phone="+9647700000002",
            subject_type="user",
        )
        with TestClient(create_app()) as client:
            response = client.get(
                "/api/v1/esim-access/profiles/my?status=active&installed=true",
                headers={"Authorization": f"Bearer {token}"},
            )
            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertEqual(payload["data"]["total"], 1)
            self.assertEqual(payload["data"]["profiles"][0]["status"], "active")
            self.assertTrue(payload["data"]["profiles"][0]["installed"])

    def test_profiles_my_days_left_starts_after_activation(self) -> None:
        with self.session_factory() as session:
            profile = session.scalar(select(ESimProfile).where(ESimProfile.iccid == "ICCID-USER2"))
            assert profile is not None
            profile.app_status = "GOT_RESOURCE"
            profile.activated_at = None
            profile.expires_at = utcnow() + timedelta(days=7)
            session.commit()

        admin_token = self._token(
            subject_id="11111111-1111-1111-1111-111111111111",
            phone="+9647700000001",
            subject_type="admin",
        )

        with TestClient(create_app()) as client:
            response = client.get(
                "/api/v1/esim-access/profiles/my?userId=33333333-3333-3333-3333-333333333333",
                headers={"Authorization": f"Bearer {admin_token}"},
            )
            self.assertEqual(response.status_code, 200)
            payload = response.json()
            profile = payload["data"]["profiles"][0]
            self.assertEqual(profile["status"], "inactive")
            self.assertIsNone(profile["activatedAt"])
            self.assertIsNone(profile["daysLeft"])

    def test_profiles_my_days_left_prefers_bundle_validity_over_provider_expiry(self) -> None:
        now = utcnow()
        with self.session_factory() as session:
            profile = session.scalar(select(ESimProfile).where(ESimProfile.iccid == "ICCID-USER2"))
            assert profile is not None
            profile.app_status = "ACTIVE"
            profile.installed = True
            profile.installed_at = now
            profile.activated_at = now
            profile.validity_days = 7
            # Simulate long provider retention/expiry window.
            profile.expires_at = now + timedelta(days=180)
            session.commit()

        admin_token = self._token(
            subject_id="11111111-1111-1111-1111-111111111111",
            phone="+9647700000001",
            subject_type="admin",
        )

        with TestClient(create_app()) as client:
            response = client.get(
                "/api/v1/esim-access/profiles/my?userId=33333333-3333-3333-3333-333333333333",
                headers={"Authorization": f"Bearer {admin_token}"},
            )
            self.assertEqual(response.status_code, 200)
            payload = response.json()
            profile = payload["data"]["profiles"][0]
            self.assertEqual(profile["status"], "active")
            self.assertIsNotNone(profile["activatedAt"])
            self.assertEqual(profile["daysLeft"], 7)
            self.assertIsNotNone(profile["bundleExpiresAt"])
            self.assertIsNotNone(profile["expiresAt"])

    def test_profiles_my_user_token_forbids_other_user_filter(self) -> None:
        token = self._token(
            subject_id="22222222-2222-2222-2222-222222222222",
            phone="+9647700000002",
            subject_type="user",
        )
        with TestClient(create_app()) as client:
            response = client.get(
                "/api/v1/esim-access/profiles/my?userId=33333333-3333-3333-3333-333333333333",
                headers={"Authorization": f"Bearer {token}"},
            )
            self.assertEqual(response.status_code, 403)

    def test_profiles_my_admin_token_can_target_user_id(self) -> None:
        token = self._token(
            subject_id="11111111-1111-1111-1111-111111111111",
            phone="+9647700000001",
            subject_type="admin",
        )
        with TestClient(create_app()) as client:
            response = client.get(
                "/api/v1/esim-access/profiles/my?userId=33333333-3333-3333-3333-333333333333",
                headers={"Authorization": f"Bearer {token}"},
            )
            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertEqual(payload["data"]["total"], 1)
            self.assertEqual(payload["data"]["profiles"][0]["userId"], "33333333-3333-3333-3333-333333333333")

    def test_install_my_updates_owned_profile(self) -> None:
        token = self._token(
            subject_id="22222222-2222-2222-2222-222222222222",
            phone="+9647700000002",
            subject_type="user",
        )
        with self.session_factory() as session:
            profile = session.scalar(select(ESimProfile).where(ESimProfile.iccid == "ICCID-USER1"))
            assert profile is not None
            profile.installed = False
            profile.installed_at = None
            session.commit()

        with TestClient(create_app()) as client:
            response = client.post(
                "/api/v1/esim-access/profiles/install/my",
                headers={"Authorization": f"Bearer {token}"},
                json={"iccid": "ICCID-USER1"},
            )
            self.assertEqual(response.status_code, 200)
            payload = response.json()
            profile = payload["data"]["profile"]
            self.assertTrue(profile["installed"])
            self.assertIsNotNone(profile["installedAt"])
            self.assertEqual(profile["iccid"], "ICCID-USER1")

    def test_activate_my_forbids_non_owner(self) -> None:
        token = self._token(
            subject_id="22222222-2222-2222-2222-222222222222",
            phone="+9647700000002",
            subject_type="user",
        )
        with TestClient(create_app()) as client:
            response = client.post(
                "/api/v1/esim-access/profiles/activate/my",
                headers={"Authorization": f"Bearer {token}"},
                json={"iccid": "ICCID-USER2"},
            )
            self.assertEqual(response.status_code, 403)

    def test_activate_my_admin_can_target_user_id(self) -> None:
        token = self._token(
            subject_id="11111111-1111-1111-1111-111111111111",
            phone="+9647700000001",
            subject_type="admin",
        )
        with TestClient(create_app()) as client:
            response = client.post(
                "/api/v1/esim-access/profiles/activate/my",
                headers={"Authorization": f"Bearer {token}"},
                json={"iccid": "ICCID-USER2", "userId": "33333333-3333-3333-3333-333333333333"},
            )
            self.assertEqual(response.status_code, 200)
            payload = response.json()
            profile = payload["data"]["profile"]
            self.assertEqual(profile["iccid"], "ICCID-USER2")
            self.assertTrue(profile["installed"])
            self.assertIsNotNone(profile["activatedAt"])
            self.assertEqual(profile["status"], "active")

    def test_exchange_rates_current_returns_configured_values(self) -> None:
        user_token = self._token(
            subject_id="22222222-2222-2222-2222-222222222222",
            phone="+9647700000002",
            subject_type="user",
        )
        with TestClient(create_app()) as client:
            response = client.get(
                "/api/v1/esim-access/exchange-rates/current",
                headers={"Authorization": f"Bearer {user_token}"},
            )
            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertTrue(payload.get("success"))
            data = payload["data"]
            self.assertTrue(data["enableIQD"])
            self.assertEqual(data["exchangeRate"], "1500")
            self.assertEqual(data["markupPercent"], "10")
            self.assertEqual(data["source"], "tulip-admin")
            self.assertIsNotNone(data["updatedAt"])

    def test_exchange_rates_current_returns_defaults_when_not_configured(self) -> None:
        admin_token = self._token(
            subject_id="11111111-1111-1111-1111-111111111111",
            phone="+9647700000001",
            subject_type="admin",
        )
        with self.session_factory() as session:
            session.execute(delete(ExchangeRate))
            session.commit()

        with TestClient(create_app()) as client:
            response = client.get(
                "/api/v1/esim-access/exchange-rates/current",
                headers={"Authorization": f"Bearer {admin_token}"},
            )
            self.assertEqual(response.status_code, 200)
            payload = response.json()
            data = payload["data"]
            self.assertFalse(data["enableIQD"])
            self.assertEqual(data["exchangeRate"], "1320")
            self.assertEqual(data["markupPercent"], "0")

    def test_new_user_scoped_reads_require_token(self) -> None:
        with TestClient(create_app()) as client:
            exchange_response = client.get("/api/v1/esim-access/exchange-rates/current")
            profiles_response = client.get("/api/v1/esim-access/profiles/my")
            install_response = client.post("/api/v1/esim-access/profiles/install/my", json={"iccid": "ICCID-USER1"})
            activate_response = client.post("/api/v1/esim-access/profiles/activate/my", json={"iccid": "ICCID-USER1"})
            self.assertEqual(exchange_response.status_code, 401)
            self.assertEqual(profiles_response.status_code, 401)
            self.assertEqual(install_response.status_code, 401)
            self.assertEqual(activate_response.status_code, 401)


if __name__ == "__main__":
    unittest.main()
