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

    def test_user_scoped_read_routes_support_preflight(self) -> None:
        with TestClient(create_app()) as client:
            exchange_preflight = client.options(
                "/api/v1/esim-access/exchange-rates/current",
                headers={
                    "Origin": "http://localhost:5173",
                    "Access-Control-Request-Method": "GET",
                    "Access-Control-Request-Headers": "authorization,content-type",
                },
            )
            profiles_preflight = client.options(
                "/api/v1/esim-access/profiles/my?limit=100&offset=0",
                headers={
                    "Origin": "http://localhost:5173",
                    "Access-Control-Request-Method": "GET",
                    "Access-Control-Request-Headers": "authorization,content-type",
                },
            )
            self.assertEqual(exchange_preflight.status_code, 200)
            self.assertEqual(profiles_preflight.status_code, 200)
            self.assertEqual(exchange_preflight.headers.get("access-control-allow-origin"), "http://localhost:5173")
            self.assertEqual(profiles_preflight.headers.get("access-control-allow-origin"), "http://localhost:5173")

            install_preflight = client.options(
                "/api/v1/esim-access/profiles/install/my",
                headers={
                    "Origin": "http://localhost:5173",
                    "Access-Control-Request-Method": "POST",
                    "Access-Control-Request-Headers": "authorization,content-type",
                },
            )
            activate_preflight = client.options(
                "/api/v1/esim-access/profiles/activate/my",
                headers={
                    "Origin": "http://localhost:5173",
                    "Access-Control-Request-Method": "POST",
                    "Access-Control-Request-Headers": "authorization,content-type",
                },
            )
            self.assertEqual(install_preflight.status_code, 200)
            self.assertEqual(activate_preflight.status_code, 200)
            self.assertEqual(install_preflight.headers.get("access-control-allow-origin"), "http://localhost:5173")
            self.assertEqual(activate_preflight.headers.get("access-control-allow-origin"), "http://localhost:5173")


if __name__ == "__main__":
    unittest.main()
