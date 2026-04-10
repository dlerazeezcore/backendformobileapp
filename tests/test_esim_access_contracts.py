from __future__ import annotations

import unittest
from typing import Generator

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from esim_access_api import register_esim_access_routes
from supabase_store import Base, normalize_database_url


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
        engine = create_engine(normalize_database_url("sqlite://"), future=True)
        Base.metadata.create_all(engine)
        self.session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

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


if __name__ == "__main__":
    unittest.main()
