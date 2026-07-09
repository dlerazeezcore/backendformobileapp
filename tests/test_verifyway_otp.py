from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import verifyway
from app import create_app
from config import get_settings
from supabase_store import Base, OtpCode, normalize_database_url


class VerifyWayOtpTest(unittest.TestCase):
    def setUp(self) -> None:
        temp_db = tempfile.NamedTemporaryFile(prefix="verifyway_otp_", suffix=".db", delete=False)
        temp_db.close()
        self.db_path = temp_db.name
        os.environ["ESIM_ACCESS_ACCESS_CODE"] = "test-code"
        os.environ["ESIM_ACCESS_SECRET_KEY"] = "test-secret"
        os.environ["DATABASE_URL"] = f"sqlite:///{self.db_path}"
        os.environ["AUTH_SECRET_KEY"] = "test-auth-secret"
        os.environ["VERIFYWAY_API_KEY"] = "test-verifyway-key"
        get_settings.cache_clear()

        self.engine = create_engine(
            normalize_database_url(os.environ["DATABASE_URL"]),
            connect_args={"check_same_thread": False},
            future=True,
        )
        Base.metadata.create_all(self.engine)
        self.session_factory = sessionmaker(bind=self.engine, autoflush=False, autocommit=False, future=True)

    def tearDown(self) -> None:
        self.engine.dispose()
        os.environ.pop("VERIFYWAY_API_KEY", None)
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
        get_settings.cache_clear()

    def _send(self, client: TestClient, phone: str) -> tuple[dict, str]:
        """POST /otp/send with the upstream call mocked; returns (json, sent code)."""
        with patch.object(verifyway, "_post_otp", new=AsyncMock(return_value={"status": "success"})) as mock_post:
            response = client.post("/api/v1/otp/send", json={"phone": phone})
        self.assertEqual(response.status_code, 200, response.text)
        recipient, code = mock_post.await_args.args
        self.assertFalse(recipient.startswith("+"))
        return response.json(), code

    def test_send_and_verify_happy_path(self) -> None:
        with TestClient(create_app()) as client:
            body, code = self._send(client, "07507343635")
            self.assertTrue(body["ok"])
            self.assertEqual(body["channel"], "whatsapp")

            verify = client.post("/api/v1/otp/verify", json={"phone": "07507343635", "code": code})
            self.assertEqual(verify.status_code, 200, verify.text)
            self.assertTrue(verify.json()["verified"])
            # Normalized Iraqi E.164 comes back so the FE can persist it.
            self.assertEqual(verify.json()["phone"], "+9647507343635")

    def test_code_is_single_use(self) -> None:
        with TestClient(create_app()) as client:
            _, code = self._send(client, "+9647507343635")
            first = client.post("/api/v1/otp/verify", json={"phone": "+9647507343635", "code": code})
            self.assertEqual(first.status_code, 200)
            second = client.post("/api/v1/otp/verify", json={"phone": "+9647507343635", "code": code})
            self.assertEqual(second.status_code, 400)

    def test_wrong_code_burns_attempts_until_invalidated(self) -> None:
        with TestClient(create_app()) as client:
            _, code = self._send(client, "+9647507343635")
            wrong = "0000" if code != "0000" else "1111"
            for _ in range(get_settings().verifyway_otp_max_attempts):
                attempt = client.post("/api/v1/otp/verify", json={"phone": "+9647507343635", "code": wrong})
                self.assertEqual(attempt.status_code, 400)
            # Attempts exhausted: even the correct code is now rejected.
            final = client.post("/api/v1/otp/verify", json={"phone": "+9647507343635", "code": code})
            self.assertEqual(final.status_code, 400)

    def test_resend_within_cooldown_is_throttled(self) -> None:
        with TestClient(create_app()) as client:
            self._send(client, "+9647507343635")
            with patch.object(verifyway, "_post_otp", new=AsyncMock(return_value={"status": "success"})):
                retry = client.post("/api/v1/otp/send", json={"phone": "+9647507343635"})
            self.assertEqual(retry.status_code, 429)
            self.assertIn("Retry-After", retry.headers)

    def test_expired_code_is_rejected(self) -> None:
        with TestClient(create_app()) as client:
            _, code = self._send(client, "+9647507343635")
            with self.session_factory() as session:
                row = session.get(OtpCode, "+9647507343635")
                row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
                session.commit()
            verify = client.post("/api/v1/otp/verify", json={"phone": "+9647507343635", "code": code})
            self.assertEqual(verify.status_code, 400)

    def test_verify_without_send_is_rejected(self) -> None:
        with TestClient(create_app()) as client:
            verify = client.post("/api/v1/otp/verify", json={"phone": "+9647507343635", "code": "1234"})
            self.assertEqual(verify.status_code, 400)

    def test_invalid_phone_is_rejected(self) -> None:
        with TestClient(create_app()) as client:
            response = client.post("/api/v1/otp/send", json={"phone": "not-a-phone"})
            self.assertEqual(response.status_code, 400)

    def test_send_without_api_key_returns_503(self) -> None:
        # Empty string (not pop): an env var must override any real key in a
        # local .env so this test can never fall through to the live API.
        os.environ["VERIFYWAY_API_KEY"] = ""
        get_settings.cache_clear()
        # No _post_otp mock here: the 503 must fire before any network call.
        with TestClient(create_app()) as client:
            response = client.post("/api/v1/otp/send", json={"phone": "+9647507343635"})
        self.assertEqual(response.status_code, 503)

    def test_upstream_failure_does_not_burn_cooldown(self) -> None:
        with TestClient(create_app()) as client:
            from fastapi import HTTPException

            failing = AsyncMock(side_effect=HTTPException(status_code=502, detail="down"))
            with patch.object(verifyway, "_post_otp", new=failing):
                first = client.post("/api/v1/otp/send", json={"phone": "+9647507343635"})
            self.assertEqual(first.status_code, 502)
            # Delivery failed, so an immediate retry must NOT hit the cooldown.
            _, code = self._send(client, "+9647507343635")
            verify = client.post("/api/v1/otp/verify", json={"phone": "+9647507343635", "code": code})
            self.assertEqual(verify.status_code, 200)

    def test_recent_verification_is_consumable_once(self) -> None:
        with TestClient(create_app()) as client:
            _, code = self._send(client, "+9647507343635")
            client.post("/api/v1/otp/verify", json={"phone": "+9647507343635", "code": code})
        with self.session_factory() as session:
            self.assertTrue(verifyway.consume_recent_verification(session, "07507343635"))
        with self.session_factory() as session:
            self.assertFalse(verifyway.consume_recent_verification(session, "07507343635"))

    def test_health_reports_configuration(self) -> None:
        with TestClient(create_app()) as client:
            response = client.get("/api/v1/otp/health")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertTrue(body["api_key_configured"])
        self.assertEqual(body["channel"], "whatsapp")


if __name__ == "__main__":
    unittest.main()
