from __future__ import annotations

import os
import unittest
import uuid
from typing import Generator

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from auth import create_access_token
from config import get_settings
from esim_access_api import register_esim_access_routes
from supabase_store import AdminUser, Base, normalize_database_url


class _ContractProvider:
    async def get_packages(self, payload):
        _ = payload
        return type(
            "ProviderResponse",
            (),
            {
                "model_dump": staticmethod(
                    lambda **_: {
                        "success": True,
                        "errorCode": "0",
                        "obj": {
                            "packageList": [
                                {
                                    "packageCode": "REGION-1",
                                    "name": "Europe Regional",
                                    "includedCountryList": [
                                        {"countryCode": "FR", "countryName": "France"},
                                        {"countryCode": "DE", "countryName": "Germany"},
                                    ],
                                }
                            ]
                        },
                    }
                )
            },
        )()

    async def query_profiles(self, payload):
        _ = payload
        return type(
            "ProviderResponse",
            (),
            {
                "model_dump": staticmethod(
                    lambda **_: {
                        "success": True,
                        "errorCode": "0",
                        "obj": {
                            "esimList": [
                                {
                                    "esimTranNo": "ESIM-1",
                                    "totalDataBytes": 2 * 1024 * 1024,
                                    "usedDataBytes": 512 * 1024,
                                },
                                {
                                    "esimTranNo": "ESIM-2",
                                    "totalVolume": 1024,
                                    "orderUsage": 250,
                                },
                            ]
                        },
                    }
                )
            },
        )()


class EsimAccessContractTest(unittest.TestCase):
    def setUp(self) -> None:
        os.environ["AUTH_SECRET_KEY"] = "test-auth-secret"
        get_settings.cache_clear()
        engine = create_engine(
            normalize_database_url("sqlite://"),
            future=True,
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        self.session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
        self.admin_id = str(uuid.uuid4())
        with self.session_factory() as session:
            session.add(
                AdminUser(
                    id=self.admin_id,
                    phone="+9647700000888",
                    name="Contract Admin",
                    status="active",
                    role="admin",
                )
            )
            session.commit()
        self.admin_headers = {
            "Authorization": "Bearer "
            + create_access_token(
                subject_id=self.admin_id,
                phone="+9647700000888",
                subject_type="admin",
                secret_key="test-auth-secret",
                ttl_seconds=3600,
            )
        }

        app = FastAPI()

        def _get_db() -> Generator[Session, None, None]:
            session = self.session_factory()
            try:
                yield session
            finally:
                session.close()

        def _get_provider() -> _ContractProvider:
            return _ContractProvider()

        register_esim_access_routes(app, _get_db, _get_provider)
        self.client = TestClient(app)
        self.engine = engine

    def tearDown(self) -> None:
        self.engine.dispose()
        get_settings.cache_clear()

    def test_packages_query_includes_machine_readable_included_countries(self) -> None:
        response = self.client.post(
            "/api/v1/esim-access/packages/query",
            json={"locationCode": "EU"},
        )
        self.assertEqual(response.status_code, 200)
        package = response.json().get("obj", {}).get("packageList", [])[0]
        included = package.get("includedCountries")
        self.assertIsInstance(included, list)
        self.assertEqual(included[0], {"code": "FR", "name": "France"})
        self.assertEqual(included[1], {"code": "DE", "name": "Germany"})

    def test_profiles_query_returns_canonical_mb_usage_fields(self) -> None:
        response = self.client.post(
            "/api/v1/esim-access/profiles/query",
            json={"iccid": "dummy"},
            headers=self.admin_headers,
        )
        self.assertEqual(response.status_code, 200)
        profiles = response.json().get("obj", {}).get("esimList", [])
        self.assertEqual(profiles[0].get("totalDataMb"), 2)
        self.assertEqual(profiles[0].get("usedDataMb"), 0)
        self.assertEqual(profiles[0].get("remainingDataMb"), 2)
        self.assertEqual(profiles[0].get("dataUsageUnit"), "MB")
        self.assertEqual(profiles[1].get("totalDataMb"), 1024)
        self.assertEqual(profiles[1].get("usedDataMb"), 250)
        self.assertEqual(profiles[1].get("remainingDataMb"), 774)

    def test_profiles_query_requires_admin_token(self) -> None:
        response = self.client.post(
            "/api/v1/esim-access/profiles/query",
            json={"iccid": "dummy"},
        )
        self.assertEqual(response.status_code, 401)

    def test_webhook_requires_configured_secret(self) -> None:
        payload = {
            "notifyType": "profileStatusChange",
            "notifyId": "evt-esim-1",
            "content": {"iccid": "8986000000000000000", "esimStatus": "ACTIVE"},
        }

        missing = self.client.post("/api/v1/esim-access/webhooks/events", json=payload)
        self.assertEqual(missing.status_code, 503)

        os.environ["ESIM_ACCESS_WEBHOOK_SECRET"] = "esim-whsec"
        get_settings.cache_clear()
        try:
            invalid = self.client.post(
                "/api/v1/esim-access/webhooks/events",
                headers={"X-ESIM-ACCESS-WEBHOOK-SECRET": "wrong"},
                json=payload,
            )
            self.assertEqual(invalid.status_code, 403)

            accepted = self.client.post(
                "/api/v1/esim-access/webhooks/events",
                headers={"X-ESIM-ACCESS-WEBHOOK-SECRET": "esim-whsec"},
                json=payload,
            )
            self.assertEqual(accepted.status_code, 200)
            self.assertEqual(accepted.json().get("status"), "accepted")
        finally:
            os.environ.pop("ESIM_ACCESS_WEBHOOK_SECRET", None)
            get_settings.cache_clear()


if __name__ == "__main__":
    unittest.main()
