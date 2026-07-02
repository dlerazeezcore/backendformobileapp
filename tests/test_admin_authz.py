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
            # SEC-3: a plain admin with ONLY can_manage_pricing.
            session.add(
                AdminUser(
                    id="44444444-4444-4444-4444-444444444444",
                    phone="+9647700000044",
                    name="Pricing Only Admin",
                    status="active",
                    role="admin",
                    can_manage_pricing=True,
                )
            )
            # SEC-3: an owner with NO granular flags — must still bypass them.
            session.add(
                AdminUser(
                    id="55555555-5555-5555-5555-555555555555",
                    phone="+9647700000055",
                    name="Owner No Flags",
                    status="active",
                    role="owner",
                )
            )
            # BE-1: a super_admin — may manage admin users but not owner accounts.
            session.add(
                AdminUser(
                    id="66666666-6666-6666-6666-666666666666",
                    phone="+9647700000066",
                    name="Super Admin",
                    status="active",
                    role="super_admin",
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
                    email="alice@example.com",
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

    def test_admin_users_directory_is_owner_gated_with_stable_shape(self) -> None:
        # BE-2: the any-admin duplicate was removed; the owner/super-gated
        # handler in admin.py is the live one.
        plain_admin = self._token("11111111-1111-1111-1111-111111111111", "+9647700000001")
        owner = self._token("55555555-5555-5555-5555-555555555555", "+9647700000055")
        with TestClient(create_app()) as client:
            self.assertEqual(
                client.get("/api/v1/admin/admin-users", headers=plain_admin).status_code, 403
            )
            response = client.get("/api/v1/admin/admin-users", headers=owner)
            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertIn("admins", payload)
            self.assertGreaterEqual(len(payload["admins"]), 1)
            row = payload["admins"][0]
            for key in ("id", "phone", "name", "role", "status", "canManageUsers", "canSendPush"):
                self.assertIn(key, row)

    def test_admin_users_post_requires_owner_or_super(self) -> None:
        # BE-1: any-admin could previously create/overwrite admin accounts.
        plain_admin = self._token("11111111-1111-1111-1111-111111111111", "+9647700000001")
        super_admin = self._token("66666666-6666-6666-6666-666666666666", "+9647700000066")
        owner = self._token("55555555-5555-5555-5555-555555555555", "+9647700000055")
        new_admin = {"phone": "+9647700000077", "name": "Fresh Admin", "role": "admin"}
        with TestClient(create_app()) as client:
            denied = client.post("/api/v1/admin/admin-users", headers=plain_admin, json=new_admin)
            self.assertEqual(denied.status_code, 403)

            created = client.post("/api/v1/admin/admin-users", headers=owner, json=new_admin)
            self.assertEqual(created.status_code, 200, created.text)
            body = created.json()["adminUser"]
            self.assertEqual(body["phone"], "+9647700000077")
            self.assertEqual(body["role"], "admin")

            also_ok = client.post(
                "/api/v1/admin/admin-users",
                headers=super_admin,
                json={"phone": "+9647700000078", "name": "Second Admin", "role": "admin"},
            )
            self.assertEqual(also_ok.status_code, 200, also_ok.text)

    def test_super_admin_cannot_assign_or_overwrite_owner(self) -> None:
        super_admin = self._token("66666666-6666-6666-6666-666666666666", "+9647700000066")
        owner = self._token("55555555-5555-5555-5555-555555555555", "+9647700000055")
        with TestClient(create_app()) as client:
            elevate = client.post(
                "/api/v1/admin/admin-users",
                headers=super_admin,
                json={"phone": "+9647700000088", "name": "Sneaky Owner", "role": "owner"},
            )
            self.assertEqual(elevate.status_code, 403)

            # Upsert keyed on the existing owner's phone must not demote them.
            demote = client.post(
                "/api/v1/admin/admin-users",
                headers=super_admin,
                json={"phone": "+9647700000055", "name": "Demoted", "role": "admin"},
            )
            self.assertEqual(demote.status_code, 403)

            # A real owner can still do both.
            owner_ok = client.post(
                "/api/v1/admin/admin-users",
                headers=owner,
                json={"phone": "+9647700000089", "name": "Co Owner", "role": "owner"},
            )
            self.assertEqual(owner_ok.status_code, 200, owner_ok.text)
            self.assertEqual(owner_ok.json()["adminUser"]["role"], "owner")

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

    def _token(self, subject_id: str, phone: str) -> dict[str, str]:
        token = create_access_token(
            subject_id=subject_id,
            phone=phone,
            subject_type="admin",
            secret_key="test-auth-secret",
            ttl_seconds=3600,
        )
        return {"Authorization": f"Bearer {token}"}

    def test_permission_scoped_admin_allowed_on_matching_route(self) -> None:
        # The pricing-only admin can reach pricing routes.
        headers = self._token("44444444-4444-4444-4444-444444444444", "+9647700000044")
        with TestClient(create_app()) as client:
            resp = client.get("/api/v1/admin/pricing-rules", headers=headers)
            self.assertEqual(resp.status_code, 200, resp.text)

    def test_permission_scoped_admin_blocked_on_other_routes(self) -> None:
        # ...but not order- or content-scoped routes.
        headers = self._token("44444444-4444-4444-4444-444444444444", "+9647700000044")
        with TestClient(create_app()) as client:
            self.assertEqual(client.get("/api/v1/admin/orders", headers=headers).status_code, 403)
            self.assertEqual(
                client.get("/api/v1/admin/featured-locations", headers=headers).status_code, 403
            )

    def test_full_flag_admin_passes_all_gated_routes(self) -> None:
        headers = self._token("11111111-1111-1111-1111-111111111111", "+9647700000001")
        with TestClient(create_app()) as client:
            self.assertEqual(client.get("/api/v1/admin/orders", headers=headers).status_code, 200)
            self.assertEqual(client.get("/api/v1/admin/pricing-rules", headers=headers).status_code, 200)
            self.assertEqual(
                client.get("/api/v1/admin/featured-locations", headers=headers).status_code, 200
            )

    def test_owner_bypasses_granular_permission_flags(self) -> None:
        # Owner has every granular flag False but must still pass every route.
        headers = self._token("55555555-5555-5555-5555-555555555555", "+9647700000055")
        with TestClient(create_app()) as client:
            self.assertEqual(client.get("/api/v1/admin/orders", headers=headers).status_code, 200)
            self.assertEqual(client.get("/api/v1/admin/pricing-rules", headers=headers).status_code, 200)
            self.assertEqual(
                client.get("/api/v1/admin/featured-locations", headers=headers).status_code, 200
            )

    def test_user_write_routes_require_can_manage_users(self) -> None:
        # SEC-3: pricing-only admin (no can_manage_users) is blocked on every
        # AppUser write route; owner bypasses the flag entirely.
        pricing_only = self._token("44444444-4444-4444-4444-444444444444", "+9647700000044")
        owner = self._token("55555555-5555-5555-5555-555555555555", "+9647700000055")
        target = "33333333-3333-3333-3333-333333333333"
        with TestClient(create_app()) as client:
            self.assertEqual(
                client.patch(f"/api/v1/admin/users/{target}", headers=pricing_only, json={"isLoyalty": True}).status_code,
                403,
            )
            self.assertEqual(
                client.delete(f"/api/v1/admin/users/{target}", headers=pricing_only).status_code, 403
            )
            self.assertEqual(
                client.delete("/api/v1/admin/users", params={"userId": target}, headers=pricing_only).status_code,
                403,
            )
            self.assertEqual(
                client.post(
                    "/api/v1/admin/users",
                    headers=pricing_only,
                    json={"phone": "+9647701234567", "name": "Alice Example", "status": "active"},
                ).status_code,
                403,
            )

            allowed = client.patch(f"/api/v1/admin/users/{target}", headers=owner, json={"isLoyalty": True})
            self.assertEqual(allowed.status_code, 200, allowed.text)
            self.assertTrue(allowed.json()["user"]["isLoyalty"])

    def test_version_info_put_requires_can_manage_content(self) -> None:
        # SEC-3: publishing latestVersion drives the mandatory-update modal, so
        # the PUT is content-gated; owner bypasses the flag entirely.
        pricing_only = self._token("44444444-4444-4444-4444-444444444444", "+9647700000044")
        content_admin = self._token("11111111-1111-1111-1111-111111111111", "+9647700000001")
        owner = self._token("55555555-5555-5555-5555-555555555555", "+9647700000055")
        with TestClient(create_app()) as client:
            denied = client.put(
                "/api/v1/admin/app/version-info",
                headers=pricing_only,
                json={"latestVersion": "9.9.9"},
            )
            self.assertEqual(denied.status_code, 403)

            allowed = client.put(
                "/api/v1/admin/app/version-info",
                headers=content_admin,
                json={"latestVersion": "2.0.0"},
            )
            self.assertEqual(allowed.status_code, 200, allowed.text)
            self.assertEqual(allowed.json()["latestVersion"], "2.0.0")

            owner_ok = client.put(
                "/api/v1/admin/app/version-info",
                headers=owner,
                json={"latestVersion": "2.0.1"},
            )
            self.assertEqual(owner_ok.status_code, 200, owner_ok.text)
            self.assertEqual(owner_ok.json()["latestVersion"], "2.0.1")

    def test_users_save_omitted_email_preserves_stored_email(self) -> None:
        # M2: a profile save without the email field must not wipe the address.
        token = create_access_token(
            subject_id="22222222-2222-2222-2222-222222222222",
            phone="+9647700000002",
            subject_type="user",
            secret_key="test-auth-secret",
            ttl_seconds=3600,
        )
        headers = {"Authorization": f"Bearer {token}"}
        with TestClient(create_app()) as client:
            seeded = client.post(
                "/api/v1/admin/users",
                headers=headers,
                json={"phone": "+9647700000002", "name": "Standard User", "email": "keepme@example.com"},
            )
            self.assertEqual(seeded.status_code, 200, seeded.text)

            no_email = client.post(
                "/api/v1/admin/users",
                headers=headers,
                json={"phone": "+9647700000002", "name": "Renamed User"},
            )
            self.assertEqual(no_email.status_code, 200, no_email.text)

            with client.app.state.db_session_factory() as session:
                row = session.scalar(select(AppUser).where(AppUser.id == "22222222-2222-2222-2222-222222222222"))
                self.assertIsNotNone(row)
                assert row is not None
                self.assertEqual(row.email, "keepme@example.com")
                self.assertEqual(row.name, "Renamed User")

    def test_users_save_duplicate_email_returns_409(self) -> None:
        # M2: unique CI email index must surface as a conflict, not a 500.
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
                json={"phone": "+9647700000002", "name": "Standard User", "email": "alice@example.com"},
            )
            self.assertEqual(response.status_code, 409, response.text)

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
