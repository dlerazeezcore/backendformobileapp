from __future__ import annotations

import os
import tempfile
import unittest

from fastapi.testclient import TestClient

from app import create_app
from config import get_settings


class CorsPreflightTest(unittest.TestCase):
    def setUp(self) -> None:
        temp_db = tempfile.NamedTemporaryFile(prefix="cors_preflight_", suffix=".db", delete=False)
        temp_db.close()
        self.db_path = temp_db.name
        os.environ["ESIM_ACCESS_ACCESS_CODE"] = "test-code"
        os.environ["ESIM_ACCESS_SECRET_KEY"] = "test-secret"
        os.environ["DATABASE_URL"] = f"sqlite:///{self.db_path}"
        os.environ["AUTH_SECRET_KEY"] = "test-auth-secret"
        get_settings.cache_clear()

    def tearDown(self) -> None:
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
        get_settings.cache_clear()

    def test_support_messages_preflight_accepts_extended_request_headers(self) -> None:
        with TestClient(create_app()) as client:
            response = client.options(
                "/api/v1/support/telegram/messages?limit=200&offset=0",
                headers={
                    "Origin": "http://localhost:5173",
                    "Access-Control-Request-Method": "GET",
                    "Access-Control-Request-Headers": "authorization,content-type,baggage,sentry-trace,x-client-info,apikey",
                },
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers.get("access-control-allow-origin"), "http://localhost:5173")
            allowed_headers = str(response.headers.get("access-control-allow-headers") or "").lower()
            self.assertIn("authorization", allowed_headers)


if __name__ == "__main__":
    unittest.main()
