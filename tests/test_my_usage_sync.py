from __future__ import annotations

import os
import unittest
import uuid
from typing import Generator

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from auth import create_access_token
from config import get_settings
from esim_access_api import register_esim_access_routes
from supabase_store import AppUser, Base, ESimProfile, normalize_database_url


class _UsageProvider:
    async def usage_check(self, payload):
        records = []
        for esim_tran_no in payload.esim_tran_no_list:
            records.append(
                {
                    "esimTranNo": esim_tran_no,
                    "totalDataMb": 1024,
                    "usedDataMb": 321,
                    "lastUpdateTime": "2026-04-26T00:00:00Z",
                }
            )
        return type(
            "ProviderResponse",
            (),
            {
                "model_dump": staticmethod(
                    lambda **_: {
                        "success": True,
                        "errorCode": "0",
                        "obj": {"esimUsageList": records},
                    }
                )
            },
        )()


class MyUsageSyncTest(unittest.TestCase):
    def setUp(self) -> None:
        os.environ["AUTH_SECRET_KEY"] = "test-auth-secret"
        os.environ["ESIM_ACCESS_ACCESS_CODE"] = "test-code"
        os.environ["ESIM_ACCESS_SECRET_KEY"] = "test-secret"
        get_settings.cache_clear()

        self.engine = create_engine(
            normalize_database_url("sqlite://"),
            future=True,
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.session_factory = sessionmaker(bind=self.engine, autoflush=False, autocommit=False, future=True)

        self.user_a_id = str(uuid.uuid4())
        self.user_b_id = str(uuid.uuid4())
        with self.session_factory() as session:
            session.add_all(
                [
                    AppUser(
                        id=self.user_a_id,
                        phone="+9647701000001",
                        name="User A",
                        status="active",
                    ),
                    AppUser(
                        id=self.user_b_id,
                        phone="+9647701000002",
                        name="User B",
                        status="active",
                    ),
                ]
            )
            session.flush()
            session.add_all(
                [
                    ESimProfile(
                        user_id=self.user_a_id,
                        esim_tran_no="ESIM-USER-A",
                        iccid="ICCID-USER-A",
                        app_status="ACTIVE",
                        installed=True,
                    ),
                    ESimProfile(
                        user_id=self.user_b_id,
                        esim_tran_no="ESIM-USER-B",
                        iccid="ICCID-USER-B",
                        app_status="ACTIVE",
                        installed=True,
                    ),
                ]
            )
            session.commit()

        app = FastAPI()

        def _get_db() -> Generator[Session, None, None]:
            session = self.session_factory()
            try:
                yield session
            finally:
                session.close()

        def _get_provider() -> _UsageProvider:
            return _UsageProvider()

        register_esim_access_routes(app, _get_db, _get_provider)
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.engine.dispose()
        get_settings.cache_clear()

    def _headers_for_user(self, *, user_id: str, phone: str) -> dict[str, str]:
        token = create_access_token(
            subject_id=user_id,
            phone=phone,
            subject_type="user",
            secret_key="test-auth-secret",
            ttl_seconds=3600,
        )
        return {"Authorization": f"Bearer {token}"}

    def test_sync_my_usage_updates_only_caller_profiles(self) -> None:
        response = self.client.post(
            "/api/v1/esim-access/usage/sync/my",
            headers=self._headers_for_user(user_id=self.user_a_id, phone="+9647701000001"),
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload.get("success"))
        sync = payload["data"]["sync"]
        self.assertEqual(sync["esimTranNosRequested"], 1)
        self.assertEqual(sync["profilesSynced"], 1)

        with self.session_factory() as session:
            profile_a = session.scalar(select(ESimProfile).where(ESimProfile.esim_tran_no == "ESIM-USER-A"))
            profile_b = session.scalar(select(ESimProfile).where(ESimProfile.esim_tran_no == "ESIM-USER-B"))
            self.assertIsNotNone(profile_a)
            self.assertIsNotNone(profile_b)
            assert profile_a is not None
            assert profile_b is not None
            self.assertEqual(profile_a.used_data_mb, 321)
            self.assertEqual(profile_a.remaining_data_mb, 703)
            self.assertIsNone(profile_b.used_data_mb)

    def test_sync_my_usage_requires_auth(self) -> None:
        response = self.client.post("/api/v1/esim-access/usage/sync/my")
        self.assertEqual(response.status_code, 401)


if __name__ == "__main__":
    unittest.main()
