from __future__ import annotations

import os
import tempfile
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import create_app
from auth import hash_password, verify_password
from config import get_settings
from phone_utils import normalize_phone
from supabase_store import AppUser, Base, normalize_database_url
from verifyway import _mint_verification_token


def _vtoken(phone: str) -> str:
    """Mint a valid WhatsApp-OTP verification token bound to ``phone``.

    The OTP auth flows (otp-login / reset-password / signup) require a proof of
    phone ownership. The token binds to normalize_phone(phone) and
    validate_verification_token re-normalizes the phone it is checked against,
    so already-E.164 numbers are idempotent. Call from inside a test method,
    AFTER setUp has set AUTH_SECRET_KEY and cleared the settings cache, so the
    mint secret matches the running app's.
    """
    return _mint_verification_token(normalize_phone(phone))


# Seeded fixtures.
ACTIVE_PHONE = "+9647700000300"
ACTIVE_PASSWORD = "StrongPass123"
INACTIVE_PHONE = "+9647700000301"
UNKNOWN_PHONE = "+9647700000399"


class AuthOtpFlowsTest(unittest.TestCase):
    def setUp(self) -> None:
        temp_db = tempfile.NamedTemporaryFile(prefix="auth_otp_flows_", suffix=".db", delete=False)
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
                    phone=ACTIVE_PHONE,
                    name="Active User",
                    status="active",
                    password_hash=hash_password(ACTIVE_PASSWORD),
                )
            )
            session.add(
                AppUser(
                    phone=INACTIVE_PHONE,
                    name="Inactive User",
                    status="disabled",
                    password_hash=hash_password(ACTIVE_PASSWORD),
                )
            )
            session.commit()

    def tearDown(self) -> None:
        self.engine.dispose()
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
        get_settings.cache_clear()

    # ------------------------------------------------------------------
    # otp-login
    # ------------------------------------------------------------------
    def test_otp_login_unknown_phone_returns_404(self) -> None:
        with TestClient(create_app()) as client:
            response = client.post(
                "/api/v1/auth/user/otp-login",
                json={"phone": UNKNOWN_PHONE, "verificationToken": _vtoken(UNKNOWN_PHONE)},
            )
            self.assertEqual(response.status_code, 404)
            self.assertEqual((response.json().get("detail") or {}).get("code"), "AUTH_NO_ACCOUNT")

    def test_otp_login_existing_user_returns_session(self) -> None:
        with TestClient(create_app()) as client:
            response = client.post(
                "/api/v1/auth/user/otp-login",
                json={"phone": ACTIVE_PHONE, "verificationToken": _vtoken(ACTIVE_PHONE)},
            )
            self.assertEqual(response.status_code, 200, response.text)
            payload = response.json()
            self.assertTrue(payload.get("accessToken"))
            self.assertTrue(payload.get("userId"))
            self.assertEqual(payload.get("id"), payload.get("userId"))
            self.assertEqual(payload.get("subjectType"), "user")
            self.assertEqual(payload.get("phone"), ACTIVE_PHONE)
            self.assertEqual(payload.get("isAdmin"), False)

            # The issued session token must authenticate on /auth/me.
            me = client.get(
                "/api/v1/auth/me",
                headers={"Authorization": f"Bearer {payload['accessToken']}"},
            )
            self.assertEqual(me.status_code, 200)
            self.assertEqual(me.json().get("subjectType"), "user")
            self.assertEqual(me.json().get("phone"), ACTIVE_PHONE)

    def test_otp_login_garbage_token_returns_400(self) -> None:
        with TestClient(create_app()) as client:
            response = client.post(
                "/api/v1/auth/user/otp-login",
                json={"phone": ACTIVE_PHONE, "verificationToken": "garbage-token"},
            )
            self.assertEqual(response.status_code, 400)
            self.assertEqual((response.json().get("detail") or {}).get("code"), "AUTH_OTP_INVALID")

    def test_otp_login_inactive_account_returns_403(self) -> None:
        with TestClient(create_app()) as client:
            response = client.post(
                "/api/v1/auth/user/otp-login",
                json={"phone": INACTIVE_PHONE, "verificationToken": _vtoken(INACTIVE_PHONE)},
            )
            self.assertEqual(response.status_code, 403)
            self.assertEqual((response.json().get("detail") or {}).get("code"), "AUTH_ACCOUNT_INACTIVE")

    # ------------------------------------------------------------------
    # reset-password
    # ------------------------------------------------------------------
    def test_reset_password_rotates_credentials_and_returns_session(self) -> None:
        new_password = "NewStrongPass456"
        with TestClient(create_app()) as client:
            reset = client.post(
                "/api/v1/auth/user/reset-password",
                json={
                    "phone": ACTIVE_PHONE,
                    "verificationToken": _vtoken(ACTIVE_PHONE),
                    "newPassword": new_password,
                },
            )
            self.assertEqual(reset.status_code, 200, reset.text)
            reset_payload = reset.json()
            # Auto-login: the reset response itself carries a usable session.
            self.assertTrue(reset_payload.get("accessToken"))
            self.assertEqual(reset_payload.get("subjectType"), "user")
            self.assertEqual(reset_payload.get("phone"), ACTIVE_PHONE)

            me = client.get(
                "/api/v1/auth/me",
                headers={"Authorization": f"Bearer {reset_payload['accessToken']}"},
            )
            self.assertEqual(me.status_code, 200)
            self.assertEqual(me.json().get("phone"), ACTIVE_PHONE)

            # Old password no longer works; the new one does.
            old_login = client.post(
                "/api/v1/auth/user/login",
                json={"phone": ACTIVE_PHONE, "password": ACTIVE_PASSWORD},
            )
            self.assertEqual(old_login.status_code, 401)

            new_login = client.post(
                "/api/v1/auth/user/login",
                json={"phone": ACTIVE_PHONE, "password": new_password},
            )
            self.assertEqual(new_login.status_code, 200)
            self.assertEqual(new_login.json().get("subjectType"), "user")

        with self.session_factory() as session:
            row = session.scalar(select(AppUser).where(AppUser.phone == ACTIVE_PHONE))
            assert row is not None
            self.assertTrue(verify_password(new_password, row.password_hash))
            self.assertFalse(verify_password(ACTIVE_PASSWORD, row.password_hash))

    def test_reset_password_unknown_phone_returns_404(self) -> None:
        with TestClient(create_app()) as client:
            response = client.post(
                "/api/v1/auth/user/reset-password",
                json={
                    "phone": UNKNOWN_PHONE,
                    "verificationToken": _vtoken(UNKNOWN_PHONE),
                    "newPassword": "AnotherStrongPass1",
                },
            )
            self.assertEqual(response.status_code, 404)
            self.assertEqual((response.json().get("detail") or {}).get("code"), "AUTH_NO_ACCOUNT")

    # ------------------------------------------------------------------
    # refresh
    # ------------------------------------------------------------------
    def test_refresh_rolls_session_forward_with_bearer(self) -> None:
        with TestClient(create_app()) as client:
            login = client.post(
                "/api/v1/auth/user/login",
                json={"phone": ACTIVE_PHONE, "password": ACTIVE_PASSWORD},
            )
            self.assertEqual(login.status_code, 200, login.text)
            access_token = login.json()["accessToken"]

            refresh = client.post(
                "/api/v1/auth/refresh",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            self.assertEqual(refresh.status_code, 200, refresh.text)
            refreshed_payload = refresh.json()
            fresh_token = refreshed_payload.get("accessToken")
            self.assertTrue(fresh_token)
            self.assertEqual(refreshed_payload.get("subjectType"), "user")
            self.assertEqual(refreshed_payload.get("phone"), ACTIVE_PHONE)

            # The freshly-issued token itself authenticates on /auth/me.
            me = client.get(
                "/api/v1/auth/me",
                headers={"Authorization": f"Bearer {fresh_token}"},
            )
            self.assertEqual(me.status_code, 200)
            self.assertEqual(me.json().get("phone"), ACTIVE_PHONE)

    def test_refresh_without_bearer_returns_401(self) -> None:
        with TestClient(create_app()) as client:
            response = client.post("/api/v1/auth/refresh")
            self.assertEqual(response.status_code, 401)

    # ------------------------------------------------------------------
    # signup (happy path proving the token requirement is satisfiable)
    # ------------------------------------------------------------------
    def test_signup_with_valid_token_succeeds(self) -> None:
        new_phone = "+9647700000400"
        with TestClient(create_app()) as client:
            response = client.post(
                "/api/v1/auth/user/signup",
                json={
                    "phone": new_phone,
                    "name": "Fresh Signup",
                    "password": "StrongPass123",
                    "verificationToken": _vtoken(new_phone),
                },
            )
            self.assertEqual(response.status_code, 200, response.text)
            payload = response.json()
            self.assertTrue(payload.get("accessToken"))
            self.assertEqual(payload.get("phone"), new_phone)
            self.assertEqual(payload.get("subjectType"), "user")

        with self.session_factory() as session:
            row = session.scalar(select(AppUser).where(AppUser.phone == new_phone))
            self.assertIsNotNone(row)
            assert row is not None
            self.assertEqual(row.status, "active")


if __name__ == "__main__":
    unittest.main()
