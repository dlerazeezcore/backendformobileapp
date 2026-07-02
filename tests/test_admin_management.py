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

os.environ.setdefault("AUTH_SECRET_KEY", "test-auth-secret")
os.environ.setdefault("ESIM_ACCESS_ACCESS_CODE", "test-code")
os.environ.setdefault("ESIM_ACCESS_SECRET_KEY", "test-secret")

from auth import create_access_token  # noqa: E402
from config import get_settings  # noqa: E402
from esim_access_api import register_esim_access_routes  # noqa: E402
from users import register_user_routes  # noqa: E402
from supabase_store import AdminUser, AppUser, Base, utcnow  # noqa: E402


class _DummyProvider:
    pass


class AdminManagementTest(unittest.TestCase):
    def setUp(self) -> None:
        get_settings.cache_clear()
        tmp = tempfile.NamedTemporaryFile(prefix="admin_mgmt_", suffix=".db", delete=False)
        tmp.close()
        self.db_path = tmp.name
        self.admin_id = str(uuid.uuid4())
        self.limited_admin_id = str(uuid.uuid4())
        self.user_id = str(uuid.uuid4())
        self.engine = create_engine(f"sqlite:///{self.db_path}", connect_args={"check_same_thread": False}, future=True)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, autoflush=False, autocommit=False, future=True)
        with self.Session() as s:
            # SEC-3: user-management writes require the can_manage_users grant;
            # the detailed orders read requires can_manage_orders.
            s.add(AdminUser(id=self.admin_id, phone="+9647500000000", name="Admin", status="active", can_manage_users=True, can_manage_orders=True))
            s.add(AdminUser(id=self.limited_admin_id, phone="+9647500000099", name="Limited Admin", status="active"))
            s.add(AppUser(id=self.user_id, phone="+9647501112222", name="Cust One", status="active"))
            s.commit()

        app = FastAPI()

        def _get_db() -> Generator[Session, None, None]:
            s = self.Session()
            try:
                yield s
            finally:
                s.close()

        register_user_routes(app, _get_db)
        register_esim_access_routes(app, _get_db, lambda: _DummyProvider())
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.engine.dispose()
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
        get_settings.cache_clear()

    def _admin_headers(self) -> dict[str, str]:
        token = create_access_token(subject_id=self.admin_id, phone="+9647500000000", subject_type="admin", secret_key="test-auth-secret", ttl_seconds=3600)
        return {"Authorization": f"Bearer {token}"}

    def test_admin_can_grant_loyalty_and_block(self) -> None:
        r = self.client.patch(f"/api/v1/admin/users/{self.user_id}", headers=self._admin_headers(), json={"isLoyalty": True})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["user"]["isLoyalty"])

        r2 = self.client.patch(f"/api/v1/admin/users/{self.user_id}", headers=self._admin_headers(), json={"blocked": True})
        self.assertEqual(r2.status_code, 200)
        self.assertTrue(r2.json()["user"]["isBlocked"])

        with self.Session() as s:
            row = s.scalar(select(AppUser).where(AppUser.id == self.user_id))
            self.assertTrue(row.is_loyalty)
            self.assertEqual(row.status, "blocked")
            self.assertIsNotNone(row.blocked_at)

        # unblock
        r3 = self.client.patch(f"/api/v1/admin/users/{self.user_id}", headers=self._admin_headers(), json={"blocked": False})
        self.assertFalse(r3.json()["user"]["isBlocked"])

    def test_admin_detailed_orders_shape(self) -> None:
        r = self.client.get("/api/v1/admin/orders/detailed", headers=self._admin_headers())
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["success"])
        self.assertIn("orders", body["data"])
        self.assertIsInstance(body["data"]["orders"], list)

    def test_admin_without_can_manage_users_gets_403_on_user_writes(self) -> None:
        token = create_access_token(subject_id=self.limited_admin_id, phone="+9647500000099", subject_type="admin", secret_key="test-auth-secret", ttl_seconds=3600)
        h = {"Authorization": f"Bearer {token}"}
        self.assertEqual(self.client.patch(f"/api/v1/admin/users/{self.user_id}", headers=h, json={"isLoyalty": True}).status_code, 403)
        self.assertEqual(self.client.delete(f"/api/v1/admin/users/{self.user_id}", headers=h).status_code, 403)

    def test_admin_without_can_manage_orders_gets_403_on_detailed_orders(self) -> None:
        # SEC-3: the all-orders reconciliation read is order-scoped, not any-admin.
        token = create_access_token(subject_id=self.limited_admin_id, phone="+9647500000099", subject_type="admin", secret_key="test-auth-secret", ttl_seconds=3600)
        h = {"Authorization": f"Bearer {token}"}
        self.assertEqual(self.client.get("/api/v1/admin/orders/detailed", headers=h).status_code, 403)

    def test_admin_endpoints_reject_non_admin(self) -> None:
        user_token = create_access_token(subject_id=self.user_id, phone="+9647501112222", subject_type="user", secret_key="test-auth-secret", ttl_seconds=3600)
        h = {"Authorization": f"Bearer {user_token}"}
        self.assertEqual(self.client.get("/api/v1/admin/orders/detailed", headers=h).status_code, 403)
        self.assertEqual(self.client.patch(f"/api/v1/admin/users/{self.user_id}", headers=h, json={"isLoyalty": True}).status_code, 403)


if __name__ == "__main__":
    unittest.main()
