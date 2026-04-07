from __future__ import annotations

import unittest
from typing import Iterator

from fastapi import FastAPI
from fastapi.testclient import TestClient

from esim_access_api import ESimAccessAPIError, register_esim_access_routes


class _FailingTopUpProvider:
    async def top_up(self, payload):  # pragma: no cover - exercised by integration request path
        _ = payload
        raise ESimAccessAPIError(
            error_code="ESIM_INVALID_TRAN_NO",
            error_message="Invalid esimTranNo for selected package",
            status_code=200,
            request_id="trace-invalid-topup-1",
        )


def _get_provider() -> _FailingTopUpProvider:
    return _FailingTopUpProvider()


def _get_db() -> Iterator[object]:
    yield object()


class ManagedTopUpErrorHandlingTest(unittest.TestCase):
    def setUp(self) -> None:
        app = FastAPI()
        register_esim_access_routes(app, _get_db, _get_provider)
        self.client = TestClient(app)

    def test_invalid_esim_tran_no_returns_json_4xx_envelope(self) -> None:
        response = self.client.post(
            "/api/v1/esim-access/topup/managed",
            json={
                "providerRequest": {
                    "packageCode": "PKG-001",
                    "transactionId": "TOPUP-TRX-001",
                    "esimTranNo": "FAKE-ESIM-TRAN-NO",
                    "iccid": "8986000000000000000",
                },
                "platformCode": "tulip_mobile_app",
                "platformName": "Tulip Mobile App",
                "actorPhone": "+9647500000000",
                "syncAfterTopup": False,
            },
        )

        self.assertGreaterEqual(response.status_code, 400)
        self.assertLess(response.status_code, 500)
        self.assertNotEqual(response.status_code, 502)
        self.assertEqual(response.headers["content-type"].split(";")[0], "application/json")

        body = response.json()
        self.assertEqual(body.get("success"), False)
        self.assertEqual(body.get("errorCode"), "ESIM_INVALID_TRAN_NO")
        self.assertTrue(body.get("message"))
        self.assertIn("invalid", (body.get("providerMessage") or "").lower())
        self.assertEqual(body.get("requestId"), "trace-invalid-topup-1")
        self.assertEqual(body.get("traceId"), "trace-invalid-topup-1")


if __name__ == "__main__":
    unittest.main()
