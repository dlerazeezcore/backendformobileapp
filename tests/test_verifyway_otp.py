from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

import verifyway
from app import create_app
from config import get_settings


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

    def tearDown(self) -> None:
        os.environ.pop("VERIFYWAY_API_KEY", None)
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
        get_settings.cache_clear()

    def _send(self, client: TestClient, phone: str) -> tuple[str, str]:
        """POST /otp/send with the upstream call mocked; returns (challenge, sent code)."""
        with patch.object(verifyway, "_post_otp", new=AsyncMock(return_value={"status": "success"})) as mock_post:
            response = client.post("/api/v1/otp/send", json={"phone": phone})
        self.assertEqual(response.status_code, 200, response.text)
        recipient, code = mock_post.await_args.args
        self.assertFalse(recipient.startswith("+"))
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertNotIn(code, body["challenge"])  # code must never appear in the token
        return body["challenge"], code

    def _verify(self, client: TestClient, phone: str, code: str, challenge: str):
        return client.post(
            "/api/v1/otp/verify",
            json={"phone": phone, "code": code, "challenge": challenge},
        )

    def test_send_and_verify_happy_path(self) -> None:
        with TestClient(create_app()) as client:
            challenge, code = self._send(client, "07507343635")
            verify = self._verify(client, "07507343635", code, challenge)
            self.assertEqual(verify.status_code, 200, verify.text)
            body = verify.json()
            self.assertTrue(body["verified"])
            # Normalized Iraqi E.164 comes back so the FE can persist it.
            self.assertEqual(body["phone"], "+9647507343635")
            self.assertTrue(body["verificationToken"])

    def test_wrong_code_is_rejected(self) -> None:
        with TestClient(create_app()) as client:
            challenge, code = self._send(client, "+9647507343635")
            wrong = "0000" if code != "0000" else "1111"
            verify = self._verify(client, "+9647507343635", wrong, challenge)
            self.assertEqual(verify.status_code, 400)
            # The correct code still verifies afterwards.
            ok = self._verify(client, "+9647507343635", code, challenge)
            self.assertEqual(ok.status_code, 200)

    def test_expired_challenge_is_rejected(self) -> None:
        with TestClient(create_app()) as client:
            with patch.object(verifyway, "_post_otp", new=AsyncMock(return_value={"status": "success"})):
                client.post("/api/v1/otp/send", json={"phone": "+9647507343635"})
            expired = verifyway._build_challenge("+9647507343635", "1234", ttl_seconds=-1)
            verify = self._verify(client, "+9647507343635", "1234", expired)
            self.assertEqual(verify.status_code, 400)

    def test_challenge_is_bound_to_phone(self) -> None:
        with TestClient(create_app()) as client:
            challenge, code = self._send(client, "+9647507343635")
            verify = self._verify(client, "+9647501112233", code, challenge)
            self.assertEqual(verify.status_code, 400)

    def test_tampered_challenge_is_rejected(self) -> None:
        with TestClient(create_app()) as client:
            challenge, code = self._send(client, "+9647507343635")
            payload_segment, signature = challenge.split(".")
            forged = verifyway._urlsafe_b64encode(
                verifyway._json_dumps(
                    {"typ": "otp-challenge", "phone": "+9647507343635", "iat": 0, "exp": 2**31, "nonce": "00"}
                )
            )
            verify = self._verify(client, "+9647507343635", code, f"{forged}.{signature}")
            self.assertEqual(verify.status_code, 400)
            garbage = self._verify(client, "+9647507343635", code, "not-a-real-challenge-token")
            self.assertEqual(garbage.status_code, 400)

    def test_verify_without_send_is_rejected(self) -> None:
        with TestClient(create_app()) as client:
            verify = self._verify(client, "+9647507343635", "1234", "bogus.challenge-that-was-never-issued")
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

    def test_verification_token_roundtrip(self) -> None:
        with TestClient(create_app()) as client:
            challenge, code = self._send(client, "+9647507343635")
            verify = self._verify(client, "+9647507343635", code, challenge)
            token = verify.json()["verificationToken"]
        self.assertTrue(verifyway.validate_verification_token(token, "07507343635"))
        self.assertFalse(verifyway.validate_verification_token(token, "+9647501112233"))
        self.assertFalse(verifyway.validate_verification_token("garbage.token", "+9647507343635"))

    def test_health_reports_configuration(self) -> None:
        with TestClient(create_app()) as client:
            response = client.get("/api/v1/otp/health")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertTrue(body["api_key_configured"])
        self.assertTrue(body["stateless"])


if __name__ == "__main__":
    unittest.main()
