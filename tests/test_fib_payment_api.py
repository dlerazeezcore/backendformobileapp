from __future__ import annotations

import json
import unittest
from typing import Any

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fib_payment_api import CreatePaymentRequest, FIBPaymentAPI, register_fib_payment_routes


class FIBPaymentAPITest(unittest.IsolatedAsyncioTestCase):
    async def test_create_payment_uses_default_callback_and_redirect(self) -> None:
        seen_requests: list[tuple[str, dict[str, Any]]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/protocol/openid-connect/token"):
                return httpx.Response(
                    status_code=200,
                    json={
                        "access_token": "token-123",
                        "expires_in": 60,
                        "token_type": "Bearer",
                    },
                )
            if request.url.path == "/protected/v1/payments":
                body = json.loads(request.content.decode("utf-8"))
                seen_requests.append((request.headers.get("Authorization", ""), body))
                return httpx.Response(
                    status_code=201,
                    json={
                        "paymentId": "pay-1",
                        "readableCode": "ABC-123",
                        "qrCode": "data:image/png;base64,test",
                    },
                )
            return httpx.Response(status_code=404, json={"message": "not found"})

        api = FIBPaymentAPI(
            client_id="cid",
            client_secret="secret",
            base_url="https://fib.stage.fib.iq",
            default_status_callback_url="https://backend.example.com/api/v1/fib-payments/webhooks/events",
            default_redirect_uri="tulip://payment/result",
            transport=httpx.MockTransport(handler),
        )

        payload = CreatePaymentRequest(
            monetaryValue={"amount": "1300", "currency": "IQD"},
            description="Test payment",
        )
        result = await api.create_payment(payload)
        await api.close()

        self.assertEqual(result.payment_id, "pay-1")
        self.assertEqual(len(seen_requests), 1)
        auth_header, request_body = seen_requests[0]
        self.assertEqual(auth_header, "Bearer token-123")
        self.assertEqual(
            request_body.get("statusCallbackUrl"),
            "https://backend.example.com/api/v1/fib-payments/webhooks/events",
        )
        self.assertEqual(request_body.get("redirectUri"), "tulip://payment/result")


class FIBPaymentWebhookRouteTest(unittest.TestCase):
    def setUp(self) -> None:
        app = FastAPI()

        class _Provider:
            webhook_secret = "whsec"

            async def create_payment(self, payload):  # pragma: no cover - route plumbing only
                _ = payload
                return None

            async def get_payment_status(self, payment_id):  # pragma: no cover - route plumbing only
                _ = payment_id
                return None

            async def cancel_payment(self, payment_id):  # pragma: no cover - route plumbing only
                _ = payment_id
                return None

            async def refund_payment(self, payment_id):  # pragma: no cover - route plumbing only
                _ = payment_id
                return None

        def get_fib_provider() -> _Provider:
            return _Provider()

        register_fib_payment_routes(app, get_fib_provider)
        self.client = TestClient(app)

    def test_webhook_requires_secret_when_configured(self) -> None:
        rejected = self.client.post(
            "/api/v1/fib-payments/webhooks/events",
            json={"id": "pay-1", "status": {"status": "PAID"}},
        )
        self.assertEqual(rejected.status_code, 401)

        accepted = self.client.post(
            "/api/v1/fib-payments/webhooks/events",
            headers={"X-FIB-WEBHOOK-SECRET": "whsec"},
            json={"id": "pay-1", "status": {"status": "PAID"}},
        )
        self.assertEqual(accepted.status_code, 202)
        self.assertEqual(accepted.json().get("status"), "accepted")
        self.assertEqual(accepted.json().get("paymentId"), "pay-1")
        self.assertEqual(accepted.json().get("paymentStatus"), "PAID")


if __name__ == "__main__":
    unittest.main()
