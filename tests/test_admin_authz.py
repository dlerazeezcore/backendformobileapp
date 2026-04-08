from __future__ import annotations

import os
import tempfile
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from auth import create_access_token, hash_password
from config import get_settings
from supabase_store import AdminUser, AppUser, Base, normalize_database_url
from app import create_app


class AdminAuthorizationTest(unittest.TestCase):
    def setUp(self) -> None:
        temp_db = tempfile.NamedTemporaryFile(prefix="admin_authz_", suffix=".db", delete=False)
        temp_db.close()
        self.db_path = temp_db.name
        os.environ["ESIM_ACCESS_ACCESS_CODE"] = "test-code"
        os.environ["ESIM_ACCESS_SECRET_KEY"] = "test-secret"
        os.environ["DATABASE_URL"] = f"sqlite:///{self.db_path}"
        os.environ["AUTH_SECRET_KEY"] = "test-auth-secret"
        get_settings.cache_clear()

        engine = create_engine(
            normalize_database_url(os.environ["DATABASE_URL"]),
            connect_args={"check_same_thread": False},
            future=True,
        )
        Base.metadata.create_all(engine)
        session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
        with session_factory() as session:
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
            session.add(
                AppUser(
                    id="22222222-2222-2222-2222-222222222222",
                    phone="+9647700000002",
                    name="Standard User",
                    status="active",
                )
            )
            session.commit()
        engine.dispose()

    def tearDown(self) -> None:
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
        get_settings.cache_clear()

    def test_admin_route_requires_token(self) -> None:
        with TestClient(create_app()) as client:
            response = client.get("/api/v1/admin/users")
            self.assertEqual(response.status_code, 401)

    def test_admin_route_accepts_admin_token(self) -> None:
        token = create_access_token(
            subject_id="11111111-1111-1111-1111-111111111111",
            phone="+9647700000001",
            subject_type="admin",
            secret_key="test-auth-secret",
            ttl_seconds=3600,
        )
        with TestClient(create_app()) as client:
            response = client.get("/api/v1/admin/users", headers={"Authorization": f"Bearer {token}"})
            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertIn("users", payload)
            self.assertEqual(payload["users"][0].get("id"), payload["users"][0].get("userId"))

    def test_user_delete_allows_admin_token_for_self_delete(self) -> None:
        token = create_access_token(
            subject_id="11111111-1111-1111-1111-111111111111",
            phone="+9647700000001",
            subject_type="admin",
            secret_key="test-auth-secret",
            ttl_seconds=3600,
        )
        with TestClient(create_app()) as client:
            response = client.delete("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertTrue(payload.get("deleted"))
            self.assertEqual(payload.get("subjectType"), "admin")

    def test_users_save_with_user_token_updates_only_self(self) -> None:
        token = create_access_token(
            subject_id="22222222-2222-2222-2222-222222222222",
            phone="+9647700000002",
            subject_type="user",
            secret_key="test-auth-secret",
            ttl_seconds=3600,
        )
        with TestClient(create_app()) as client:
            response = client.post(
                "/api/v1/admin/users",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "phone": "+9647700000002",
                    "name": "Updated Name",
                    "email": "updated@example.com",
                    "status": "active",
                },
            )
            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertEqual(payload["user"]["id"], "22222222-2222-2222-2222-222222222222")
            self.assertEqual(payload["user"]["name"], "Updated Name")

    def test_users_list_with_user_token_returns_only_self(self) -> None:
        token = create_access_token(
            subject_id="22222222-2222-2222-2222-222222222222",
            phone="+9647700000002",
            subject_type="user",
            secret_key="test-auth-secret",
            ttl_seconds=3600,
        )
        with TestClient(create_app()) as client:
            response = client.get("/api/v1/admin/users", headers={"Authorization": f"Bearer {token}"})
            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertEqual(len(payload.get("users", [])), 1)
            user_row = payload["users"][0]
            self.assertEqual(user_row.get("id"), "22222222-2222-2222-2222-222222222222")
            self.assertEqual(user_row.get("id"), user_row.get("userId"))


if __name__ == "__main__":
    unittest.main()
