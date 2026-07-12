"""Audit finding #14: API_DOCS_ENABLED must gate /docs, /redoc and /openapi.json.

The interactive docs enumerate the full admin/payment surface, so production
deployments set API_DOCS_ENABLED=false. Default stays True for local dev.
"""

from __future__ import annotations

import os
import tempfile
import unittest

from fastapi.testclient import TestClient

from app import create_app
from config import get_settings

DOC_ROUTES = ("/docs", "/redoc", "/openapi.json")


class ApiDocsFlagTest(unittest.TestCase):
    def setUp(self) -> None:
        temp_db = tempfile.NamedTemporaryFile(prefix="api_docs_flag_", suffix=".db", delete=False)
        temp_db.close()
        self.db_path = temp_db.name
        os.environ["ESIM_ACCESS_ACCESS_CODE"] = "test-code"
        os.environ["ESIM_ACCESS_SECRET_KEY"] = "test-secret"
        os.environ["DATABASE_URL"] = f"sqlite:///{self.db_path}"
        os.environ["AUTH_SECRET_KEY"] = "test-auth-secret"
        os.environ.pop("API_DOCS_ENABLED", None)
        get_settings.cache_clear()

    def tearDown(self) -> None:
        os.environ.pop("API_DOCS_ENABLED", None)
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
        get_settings.cache_clear()

    def test_docs_routes_served_by_default(self) -> None:
        with TestClient(create_app()) as client:
            for route in DOC_ROUTES:
                with self.subTest(route=route):
                    self.assertEqual(client.get(route).status_code, 200)

    def test_flag_disables_docs_routes(self) -> None:
        os.environ["API_DOCS_ENABLED"] = "false"
        get_settings.cache_clear()
        with TestClient(create_app()) as client:
            for route in DOC_ROUTES:
                with self.subTest(route=route):
                    self.assertEqual(client.get(route).status_code, 404)


if __name__ == "__main__":
    unittest.main()
