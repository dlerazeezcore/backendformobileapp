from __future__ import annotations

import os
import tempfile
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import create_app
from auth import hash_password
from config import get_settings
from dependencies import get_twilio_provider
from supabase_store import AdminUser, AppUser, Base, normalize_database_url


class _FakeTwilioVerifyProvider:
    async def start_verification(self, *, phone: str, channel: str = "whatsapp"):
        return {"sid": "VE123", "status": "pending", "to": phone, "channel": channel}

    async def check_verification(self, *, phone: str, code: str):
        if code != "123456":
            return {"sid": "VE123", "status": "pending", "to": phone}
        return {"sid": "VE123", "status": "approved", "to": phone}

    async def close(self) -> None:
        return None


class TwilioWhatsAppAuthTest(unittest.TestCase):
    def setUp(self) -> None:
        temp_db = tempfile.NamedTemporaryFile(prefix="twilio_whatsapp_", suffix=".db", delete=False)
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
        with self.session_factory() as session:
            session.add(
                AppUser(
                    id="aaaaaaaa-1111-2222-3333-bbbbbbbbbbbb",
                    phone="+9647700000002",
                    name="Existing User",
                    status="active",
                    password_hash=hash_password("StrongPass123"),
                )
            )
            session.add(
                AdminUser(
                    id="cccccccc-1111-2222-3333-dddddddddddd",
                    phone="+9647700000001",
                    name="Admin User",
                    status="active",
                    role="admin",
                    can_send_push=True,
                    password_hash=hash_password("StrongPass123"),
                )
            )
            session.commit()

    def tearDown(self) -> None:
        self.engine.dispose()
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
        get_settings.cache_clear()

    def _client_with_fake_twilio(self) -> TestClient:
        app = create_app()
        fake_provider = _FakeTwilioVerifyProvider()
        app.dependency_overrides[get_twilio_provider] = lambda: fake_provider
        app.state.twilio_whatsapp_api = fake_provider
        return TestClient(app)

    def test_request_user_otp_whatsapp(self) -> None:
        with self._client_with_fake_twilio() as client:
            response = client.post(
                "/api/v1/auth/user/otp/request",
                json={"phone": "+9647700000002", "channel": "whatsapp"},
            )
            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertTrue(payload["success"])
            self.assertEqual(payload["data"]["status"], "pending")
            self.assertEqual(payload["data"]["channel"], "whatsapp")

    def test_verify_user_otp_creates_new_user(self) -> None:
        with self._client_with_fake_twilio() as client:
            client.app.state.twilio_whatsapp_api = _FakeTwilioVerifyProvider()
            response = client.post(
                "/api/v1/auth/user/otp/verify",
                json={"phone": "+9647700000100", "code": "123456", "name": "OTP User"},
            )
            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertTrue(payload.get("accessToken"))
            self.assertEqual(payload.get("subjectType"), "user")
            self.assertEqual(payload.get("phone"), "+9647700000100")

        with self.session_factory() as session:
            row = session.scalar(select(AppUser).where(AppUser.phone == "+9647700000100"))
            self.assertIsNotNone(row)
            assert row is not None
            self.assertEqual(row.name, "OTP User")

    def test_user_login_supports_otp_code_for_existing_user(self) -> None:
        with self._client_with_fake_twilio() as client:
            client.app.state.twilio_whatsapp_api = _FakeTwilioVerifyProvider()
            response = client.post(
                "/api/v1/auth/user/login",
                json={"phone": "+9647700000002", "otpCode": "123456"},
            )
            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertTrue(payload.get("accessToken"))
            self.assertEqual(payload.get("tokenType"), "bearer")

    def test_signup_supports_otp_without_password(self) -> None:
        with self._client_with_fake_twilio() as client:
            client.app.state.twilio_whatsapp_api = _FakeTwilioVerifyProvider()
            response = client.post(
                "/api/v1/auth/user/signup",
                json={
                    "phone": "+9647700000101",
                    "name": "OTP Signup User",
                    "otpCode": "123456",
                },
            )
            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertTrue(payload.get("accessToken"))
            self.assertEqual(payload.get("phone"), "+9647700000101")

    def test_invalid_otp_rejected(self) -> None:
        with self._client_with_fake_twilio() as client:
            client.app.state.twilio_whatsapp_api = _FakeTwilioVerifyProvider()
            response = client.post(
                "/api/v1/auth/user/otp/verify",
                json={"phone": "+9647700000002", "code": "000000"},
            )
            self.assertEqual(response.status_code, 401)


if __name__ == "__main__":
    unittest.main()
