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
from supabase_store import AdminUser, AppUser, Base, normalize_database_url


class PublicUserSignupTest(unittest.TestCase):
    def setUp(self) -> None:
        temp_db = tempfile.NamedTemporaryFile(prefix="user_signup_", suffix=".db", delete=False)
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
                    phone="+9647700000002",
                    name="Existing User",
                    status="active",
                    password_hash=hash_password("StrongPass123"),
                )
            )
            session.add(
                AdminUser(
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

    def test_user_signup_creates_account_and_returns_session(self) -> None:
        with TestClient(create_app()) as client:
            response = client.post(
                "/api/v1/auth/user/signup",
                json={
                    "phone": "+9647700000100",
                    "name": "New Customer",
                    "password": "StrongPass123",
                },
            )
            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertTrue(payload.get("accessToken"))
            self.assertEqual(payload.get("phone"), "+9647700000100")
            self.assertEqual(payload.get("name"), "New Customer")
            self.assertTrue(payload.get("userId"))
            self.assertEqual(payload.get("id"), payload.get("userId"))
            self.assertEqual(payload.get("subjectType"), "user")

            me_response = client.get(
                "/api/v1/auth/me",
                headers={"Authorization": f"Bearer {payload['accessToken']}"},
            )
            self.assertEqual(me_response.status_code, 200)
            self.assertEqual(me_response.json().get("subjectType"), "user")
            self.assertEqual(me_response.json().get("phone"), "+9647700000100")

            login_response = client.post(
                "/api/v1/auth/user/login",
                json={"phone": "+9647700000100", "password": "StrongPass123"},
            )
            self.assertEqual(login_response.status_code, 200)
            login_payload = login_response.json()
            self.assertTrue(login_payload.get("accessToken"))
            self.assertEqual(login_payload.get("subjectType"), "user")
            self.assertEqual(login_payload.get("isAdmin"), False)
            self.assertEqual(login_payload.get("id"), login_payload.get("userId"))
            self.assertEqual(login_payload.get("phone"), "+9647700000100")

        with self.session_factory() as session:
            row = session.scalar(select(AppUser).where(AppUser.phone == "+9647700000100"))
            self.assertIsNotNone(row)
            assert row is not None
            self.assertEqual(row.status, "active")
            self.assertTrue(verify_password("StrongPass123", row.password_hash))

    def test_user_register_alias_works(self) -> None:
        with TestClient(create_app()) as client:
            response = client.post(
                "/api/v1/auth/user/register",
                json={
                    "phone": "+9647700000101",
                    "name": "Alias User",
                    "password": "StrongPass123",
                },
            )
            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertTrue(payload.get("accessToken"))
            self.assertEqual(payload.get("phone"), "+9647700000101")

    def test_signup_duplicate_user_returns_409(self) -> None:
        with TestClient(create_app()) as client:
            response = client.post(
                "/api/v1/auth/user/signup",
                json={
                    "phone": "+9647700000002",
                    "name": "Duplicate User",
                    "password": "StrongPass123",
                },
            )
            self.assertEqual(response.status_code, 409)
            self.assertIn("already exists", str(response.json().get("detail", "")).lower())

    def test_signup_admin_phone_returns_409(self) -> None:
        with TestClient(create_app()) as client:
            response = client.post(
                "/api/v1/auth/user/signup",
                json={
                    "phone": "+9647700000001",
                    "name": "Conflicting User",
                    "password": "StrongPass123",
                },
            )
            self.assertEqual(response.status_code, 409)
            self.assertIn("admin", str(response.json().get("detail", "")).lower())

    def test_signup_invalid_phone_returns_422(self) -> None:
        with TestClient(create_app()) as client:
            response = client.post(
                "/api/v1/auth/user/signup",
                json={
                    "phone": "07700000123",
                    "name": "Invalid Phone",
                    "password": "StrongPass123",
                },
            )
            self.assertEqual(response.status_code, 422)

    def test_authenticated_user_can_self_delete(self) -> None:
        with TestClient(create_app()) as client:
            signup_response = client.post(
                "/api/v1/auth/user/signup",
                json={
                    "phone": "+9647700000199",
                    "name": "Delete Me",
                    "password": "StrongPass123",
                },
            )
            self.assertEqual(signup_response.status_code, 200)
            access_token = signup_response.json()["accessToken"]

            delete_response = client.delete(
                "/api/v1/auth/me",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            self.assertEqual(delete_response.status_code, 200)
            delete_payload = delete_response.json()
            self.assertTrue(delete_payload.get("deleted"))
            self.assertEqual(delete_payload.get("status"), "deleted")
            self.assertEqual(delete_payload.get("id"), delete_payload.get("userId"))

            delete_alias_response = client.post(
                "/api/v1/auth/user/delete",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            self.assertEqual(delete_alias_response.status_code, 200)
            self.assertTrue(delete_alias_response.json().get("deleted"))

            delete_unversioned_alias = client.delete(
                "/auth/me",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            self.assertEqual(delete_unversioned_alias.status_code, 200)
            self.assertEqual(delete_unversioned_alias.json().get("status"), "deleted")

        with self.session_factory() as session:
            row = session.scalar(select(AppUser).where(AppUser.phone == "+9647700000199"))
            self.assertIsNotNone(row)
            assert row is not None
            self.assertEqual(row.status, "deleted")
            self.assertIsNotNone(row.deleted_at)

    def test_authenticated_user_can_update_own_name_via_auth_me_patch(self) -> None:
        with TestClient(create_app()) as client:
            signup_response = client.post(
                "/api/v1/auth/user/signup",
                json={
                    "phone": "+9647700000201",
                    "name": "Before Name",
                    "password": "StrongPass123",
                },
            )
            self.assertEqual(signup_response.status_code, 200)
            access_token = signup_response.json()["accessToken"]

            update_response = client.patch(
                "/api/v1/auth/me",
                headers={"Authorization": f"Bearer {access_token}"},
                json={"name": "After Name"},
            )
            self.assertEqual(update_response.status_code, 200)
            payload = update_response.json()
            self.assertEqual(payload.get("subjectType"), "user")
            self.assertEqual(payload.get("name"), "After Name")
            self.assertEqual(payload.get("id"), payload.get("userId"))

            me_response = client.get(
                "/api/v1/auth/me",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            self.assertEqual(me_response.status_code, 200)
            self.assertEqual(me_response.json().get("name"), "After Name")

        with self.session_factory() as session:
            row = session.scalar(select(AppUser).where(AppUser.phone == "+9647700000201"))
            self.assertIsNotNone(row)
            assert row is not None
            self.assertEqual(row.name, "After Name")


if __name__ == "__main__":
    unittest.main()
