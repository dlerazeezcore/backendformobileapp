from __future__ import annotations

import os
import tempfile
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from app import create_app
from config import get_settings
from supabase_store import Base, normalize_database_url


class AuthRoutingCorsTest(unittest.TestCase):
    def setUp(self) -> None:
        temp_db = tempfile.NamedTemporaryFile(prefix="auth_routing_", suffix=".db", delete=False)
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
        engine.dispose()

        self.client_cm = TestClient(create_app())
        self.client = self.client_cm.__enter__()

    def tearDown(self) -> None:
        self.client_cm.__exit__(None, None, None)
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
        get_settings.cache_clear()

    def test_canonical_auth_routes_exist(self) -> None:
        otp_request = self.client.post("/api/v1/auth/user/otp/request", json={"phone": "+9647507343635"})
        self.assertNotIn(otp_request.status_code, {404, 405})

        signup = self.client.post(
            "/api/v1/auth/user/signup",
            json={"phone": "+9647507343635", "name": "Test User", "otpCode": "123456"},
        )
        self.assertNotIn(signup.status_code, {404, 405})

        login = self.client.post("/api/v1/auth/user/login", json={"phone": "+9647507343635", "otpCode": "123456"})
        self.assertNotEqual(login.status_code, 405)

        forgot_reset = self.client.post(
            "/api/v1/auth/user/password/forgot/reset",
            json={"phone": "+9647507343635", "otpCode": "123456", "newPassword": "StrongPass123"},
        )
        self.assertNotIn(forgot_reset.status_code, {404, 405})

        auth_me = self.client.get("/api/v1/auth/me")
        self.assertEqual(auth_me.status_code, 401)

    def test_duplicate_prefix_returns_clear_404(self) -> None:
        response = self.client.post("/api/v1/api/v1/auth/user/otp/request", json={"phone": "+9647507343635"})
        self.assertEqual(response.status_code, 404)
        self.assertIn("detail", response.json())
        self.assertIn("duplicate '/api/v1' prefix", str(response.json()["detail"]))

    def test_auth_preflight_cors_allows_required_methods_and_headers(self) -> None:
        response = self.client.options(
            "/api/v1/auth/user/otp/request",
            headers={
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization,content-type",
            },
        )
        self.assertEqual(response.status_code, 200)

        allow_methods = response.headers.get("access-control-allow-methods", "").lower()
        for expected in ["get", "post", "patch", "delete", "options"]:
            self.assertIn(expected, allow_methods)

        allow_headers = response.headers.get("access-control-allow-headers", "").lower()
        self.assertIn("authorization", allow_headers)
        self.assertIn("content-type", allow_headers)


if __name__ == "__main__":
    unittest.main()
