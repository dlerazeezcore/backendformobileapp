from __future__ import annotations

import os
import tempfile
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
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
            session.add(
                AppUser(
                    id="33333333-3333-3333-3333-333333333333",
                    phone="+9647701234567",
                    name="Alice Example",
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

    def test_admin_login_returns_subject_metadata(self) -> None:
        with TestClient(create_app()) as client:
            response = client.post(
                "/api/v1/auth/admin/login",
                json={"phone": "+9647700000001", "password": "StrongPass123"},
            )
            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertEqual(payload.get("subjectType"), "admin")
            self.assertEqual(payload.get("isAdmin"), True)
            self.assertEqual(payload.get("id"), payload.get("adminUserId"))
            self.assertEqual(payload.get("phone"), "+9647700000001")
            self.assertTrue(payload.get("accessToken"))

    def test_user_login_with_admin_credentials_returns_admin_subject(self) -> None:
        with TestClient(create_app()) as client:
            response = client.post(
                "/api/v1/auth/user/login",
                json={"phone": "+9647700000001", "password": "StrongPass123"},
            )
            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertEqual(payload.get("subjectType"), "admin")
            self.assertEqual(payload.get("isAdmin"), True)
            self.assertEqual(payload.get("id"), payload.get("adminUserId"))
            self.assertTrue(payload.get("accessToken"))

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

    def test_admin_can_update_own_name_via_auth_me_patch(self) -> None:
        token = create_access_token(
            subject_id="11111111-1111-1111-1111-111111111111",
            phone="+9647700000001",
            subject_type="admin",
            secret_key="test-auth-secret",
            ttl_seconds=3600,
        )
        with TestClient(create_app()) as client:
            update_response = client.patch(
                "/api/v1/auth/me",
                headers={"Authorization": f"Bearer {token}"},
                json={"name": "Updated Admin Name"},
            )
            self.assertEqual(update_response.status_code, 200)
            payload = update_response.json()
            self.assertEqual(payload.get("subjectType"), "admin")
            self.assertEqual(payload.get("id"), payload.get("adminUserId"))
            self.assertEqual(payload.get("name"), "Updated Admin Name")

            me_response = client.get(
                "/api/v1/auth/me",
                headers={"Authorization": f"Bearer {token}"},
            )
            self.assertEqual(me_response.status_code, 200)
            self.assertEqual(me_response.json().get("name"), "Updated Admin Name")

    def test_admin_can_update_own_email_via_auth_me_patch(self) -> None:
        token = create_access_token(
            subject_id="11111111-1111-1111-1111-111111111111",
            phone="+9647700000001",
            subject_type="admin",
            secret_key="test-auth-secret",
            ttl_seconds=3600,
        )
        with TestClient(create_app()) as client:
            update_response = client.patch(
                "/api/v1/auth/me",
                headers={"Authorization": f"Bearer {token}"},
                json={"email": "ADMIN@example.com"},
            )
            self.assertEqual(update_response.status_code, 200)
            payload = update_response.json()
            self.assertEqual(payload.get("subjectType"), "admin")
            self.assertEqual(payload.get("email"), "admin@example.com")

            me_response = client.get(
                "/api/v1/auth/me",
                headers={"Authorization": f"Bearer {token}"},
            )
            self.assertEqual(me_response.status_code, 200)
            self.assertEqual(me_response.json().get("email"), "admin@example.com")

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

    def test_admin_users_contract_has_stable_flags(self) -> None:
        token = create_access_token(
            subject_id="11111111-1111-1111-1111-111111111111",
            phone="+9647700000001",
            subject_type="admin",
            secret_key="test-auth-secret",
            ttl_seconds=3600,
        )
        with TestClient(create_app()) as client:
            response = client.get("/api/v1/admin/users?limit=20&offset=0", headers={"Authorization": f"Bearer {token}"})
            self.assertEqual(response.status_code, 200)
            rows = response.json().get("users", [])
            self.assertGreaterEqual(len(rows), 2)
            row = rows[0]
            for key in ("id", "name", "phone", "status", "isBlocked", "isLoyalty", "updatedAt"):
                self.assertIn(key, row)
            self.assertIsInstance(row.get("isBlocked"), bool)
            self.assertIsInstance(row.get("isLoyalty"), bool)

    def test_admin_users_search_supports_name_and_phone_prefix(self) -> None:
        token = create_access_token(
            subject_id="11111111-1111-1111-1111-111111111111",
            phone="+9647700000001",
            subject_type="admin",
            secret_key="test-auth-secret",
            ttl_seconds=3600,
        )
        with TestClient(create_app()) as client:
            by_name = client.get(
                "/api/v1/admin/users?search=alice&limit=20&offset=0",
                headers={"Authorization": f"Bearer {token}"},
            )
            self.assertEqual(by_name.status_code, 200)
            name_rows = by_name.json().get("users", [])
            self.assertEqual(len(name_rows), 1)
            self.assertEqual(name_rows[0]["name"], "Alice Example")

            by_phone = client.get(
                "/api/v1/admin/users?search=+964770123&limit=20&offset=0",
                headers={"Authorization": f"Bearer {token}"},
            )
            self.assertEqual(by_phone.status_code, 200)
            phone_rows = by_phone.json().get("users", [])
            self.assertEqual(len(phone_rows), 1)
            self.assertEqual(phone_rows[0]["phone"], "+9647701234567")

    def test_admin_users_post_mutation_read_is_immediate(self) -> None:
        token = create_access_token(
            subject_id="11111111-1111-1111-1111-111111111111",
            phone="+9647700000001",
            subject_type="admin",
            secret_key="test-auth-secret",
            ttl_seconds=3600,
        )
        with TestClient(create_app()) as client:
            update = client.post(
                "/api/v1/admin/users",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "phone": "+9647701234567",
                    "name": "Alice Example",
                    "status": "blocked",
                    "isLoyalty": True,
                    "blockedAt": "2026-04-10T10:00:00Z",
                },
            )
            self.assertEqual(update.status_code, 200)

            read_back = client.get(
                "/api/v1/admin/users?search=+964770123&limit=20&offset=0",
                headers={"Authorization": f"Bearer {token}"},
            )
            self.assertEqual(read_back.status_code, 200)
            rows = read_back.json().get("users", [])
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(row["status"], "blocked")
            self.assertTrue(row["isBlocked"])
            self.assertTrue(row["isLoyalty"])

    def test_admin_users_list_includes_stable_status_flags(self) -> None:
        token = create_access_token(
            subject_id="11111111-1111-1111-1111-111111111111",
            phone="+9647700000001",
            subject_type="admin",
            secret_key="test-auth-secret",
            ttl_seconds=3600,
        )
        with TestClient(create_app()) as client:
            response = client.get("/api/v1/admin/admin-users", headers={"Authorization": f"Bearer {token}"})
            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertIn("adminUsers", payload)
            self.assertGreaterEqual(len(payload["adminUsers"]), 1)
            row = payload["adminUsers"][0]
            self.assertIn("status", row)
            self.assertIn("isLoyalty", row)
            self.assertIn("blockedAt", row)
            self.assertIn("deletedAt", row)

    def test_admin_can_delete_user_by_id(self) -> None:
        token = create_access_token(
            subject_id="11111111-1111-1111-1111-111111111111",
            phone="+9647700000001",
            subject_type="admin",
            secret_key="test-auth-secret",
            ttl_seconds=3600,
        )
        with TestClient(create_app()) as client:
            delete_response = client.delete(
                "/api/v1/admin/users/22222222-2222-2222-2222-222222222222",
                headers={"Authorization": f"Bearer {token}"},
            )
            self.assertEqual(delete_response.status_code, 200)
            payload = delete_response.json()
            self.assertTrue(payload.get("deleted"))
            self.assertEqual(payload.get("id"), "22222222-2222-2222-2222-222222222222")
            self.assertEqual(payload.get("id"), payload.get("userId"))
            self.assertEqual(payload.get("status"), "deleted")
            self.assertTrue(payload.get("deletedAt"))

            list_response = client.get(
                "/api/v1/admin/users?limit=20&offset=0",
                headers={"Authorization": f"Bearer {token}"},
            )
            self.assertEqual(list_response.status_code, 200)
            user_ids = {row.get("id") for row in list_response.json().get("users", [])}
            self.assertNotIn("22222222-2222-2222-2222-222222222222", user_ids)

            include_deleted_response = client.get(
                "/api/v1/admin/users?limit=20&offset=0&includeDeleted=true",
                headers={"Authorization": f"Bearer {token}"},
            )
            self.assertEqual(include_deleted_response.status_code, 200)
            deleted_rows = [
                row
                for row in include_deleted_response.json().get("users", [])
                if row.get("id") == "22222222-2222-2222-2222-222222222222"
            ]
            self.assertEqual(len(deleted_rows), 1)
            self.assertEqual(deleted_rows[0].get("status"), "deleted")

        with TestClient(create_app()) as client:
            with client.app.state.db_session_factory() as session:
                row = session.scalar(select(AppUser).where(AppUser.id == "22222222-2222-2222-2222-222222222222"))
                self.assertIsNotNone(row)
                assert row is not None
                self.assertEqual(row.status, "deleted")
                self.assertIsNotNone(row.deleted_at)

    def test_admin_can_delete_user_by_collection_route_query_param(self) -> None:
        token = create_access_token(
            subject_id="11111111-1111-1111-1111-111111111111",
            phone="+9647700000001",
            subject_type="admin",
            secret_key="test-auth-secret",
            ttl_seconds=3600,
        )
        with TestClient(create_app()) as client:
            response = client.delete(
                "/api/v1/admin/users",
                params={"userId": "33333333-3333-3333-3333-333333333333"},
                headers={"Authorization": f"Bearer {token}"},
            )
            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertTrue(payload.get("deleted"))
            self.assertEqual(payload.get("userId"), "33333333-3333-3333-3333-333333333333")

    def test_admin_user_delete_requires_admin_token(self) -> None:
        user_token = create_access_token(
            subject_id="22222222-2222-2222-2222-222222222222",
            phone="+9647700000002",
            subject_type="user",
            secret_key="test-auth-secret",
            ttl_seconds=3600,
        )
        with TestClient(create_app()) as client:
            response = client.delete(
                "/api/v1/admin/users/33333333-3333-3333-3333-333333333333",
                headers={"Authorization": f"Bearer {user_token}"},
            )
            self.assertEqual(response.status_code, 403)


if __name__ == "__main__":
    unittest.main()
