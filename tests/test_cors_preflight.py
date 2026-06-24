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

    def test_allowed_origins_support_esim_preflight(self) -> None:
        allowed_origins = [
            "https://tulipbookings.com",
            "https://www.tulipbookings.com",
            "https://dlerazeezcore.github.io",
            "capacitor://localhost",
        ]
        with TestClient(create_app()) as client:
            for origin in allowed_origins:
                with self.subTest(origin=origin):
                    response = client.options(
                        "/api/v1/esim-access/packages/query",
                        headers={
                            "Origin": origin,
                            "Access-Control-Request-Method": "POST",
                            "Access-Control-Request-Headers": "authorization,content-type,x-client-info",
                        },
                    )
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.headers.get("access-control-allow-origin"), origin)
                    # Credentialed CORS must echo the specific origin (never "*").
                    self.assertEqual(response.headers.get("access-control-allow-credentials"), "true")

    def test_unknown_origins_are_not_reflected(self) -> None:
        unknown_origins = [
            "https://evil.example.com",
            "https://customer-mobile.example.com",
            "https://tulip-mobile.vercel.app",
            "https://preview.pages.dev",
            "https://www.figma.com",
            "http://192.168.1.25:8100",
        ]
        with TestClient(create_app()) as client:
            for origin in unknown_origins:
                with self.subTest(origin=origin):
                    response = client.options(
                        "/api/v1/esim-access/packages/query",
                        headers={
                            "Origin": origin,
                            "Access-Control-Request-Method": "POST",
                            "Access-Control-Request-Headers": "authorization,content-type,x-client-info",
                        },
                    )
                    # An unknown origin must never be reflected back.
                    self.assertNotEqual(response.headers.get("access-control-allow-origin"), origin)


if __name__ == "__main__":
    unittest.main()
