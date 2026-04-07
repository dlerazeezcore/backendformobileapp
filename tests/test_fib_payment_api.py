from __future__ import annotations

import json
import os
import tempfile
import unittest
import uuid
from typing import Any, Generator

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from fib_payment_api import (
    CreatePaymentRequest,
    CreatePaymentResponse,
    FIBPaymentAPI,
    PaymentStatusResponse,
    register_fib_payment_routes,
)
from supabase_store import AdminUser, AppUser, Base, PaymentAttempt, PaymentProviderEvent


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
            default_status_callback_url="https://backend.example.com/api/v1/payments/fib/webhook",
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
            "https://backend.example.com/api/v1/payments/fib/webhook",
        )
        self.assertEqual(request_body.get("redirectUri"), "tulip://payment/result")


class _FakeProvider:
    webhook_secret = "whsec"

    async def create_payment(self, payload: CreatePaymentRequest) -> CreatePaymentResponse:
        _ = payload
        return CreatePaymentResponse(
            paymentId="fib-pay-1",
            readableCode="FIB-READABLE",
            qrCode="https://example.com/qr.png",
            validUntil="2026-04-08T00:00:00+03:00",
            personalAppLink="https://pay.example.com/fib-pay-1",
        )

    async def get_payment_status(self, payment_id: str) -> PaymentStatusResponse:
        return PaymentStatusResponse(
            paymentId=payment_id,
            status="PAID",
            paidAt="2026-04-07T00:10:00+03:00",
            validUntil="2026-04-08T00:00:00+03:00",
            amount={"amount": 5000, "currency": "IQD"},
        )

    async def cancel_payment(self, payment_id: str) -> None:
        _ = payment_id

    async def refund_payment(self, payment_id: str) -> None:
        _ = payment_id


class FIBPaymentRoutesTest(unittest.TestCase):
    def setUp(self) -> None:
        temp_db = tempfile.NamedTemporaryFile(prefix="fib_test_", suffix=".db", delete=False)
        temp_db.close()
        self.db_path = temp_db.name
        self.engine = create_engine(
            f"sqlite+pysqlite:///{self.db_path}",
            connect_args={"check_same_thread": False},
            future=True,
        )
        Base.metadata.create_all(self.engine)
        self.session_factory = sessionmaker(bind=self.engine, autoflush=False, autocommit=False, future=True)

        app = FastAPI()

        def _get_provider() -> _FakeProvider:
            return _FakeProvider()

        def _get_db() -> Generator[Session, None, None]:
            session = self.session_factory()
            try:
                yield session
            finally:
                session.close()

        register_fib_payment_routes(app, _get_provider, _get_db)
        self.client = TestClient(app)

        self.known_user_id = str(uuid.uuid4())
        self.known_admin_id = str(uuid.uuid4())
        with self.session_factory() as session:
            session.add(
                AppUser(
                    id=self.known_user_id,
                    phone="+9647700000999",
                    name="Known User",
                    status="active",
                )
            )
            session.add(
                AdminUser(
                    id=self.known_admin_id,
                    phone="+9647700000888",
                    name="Known Admin",
                    status="active",
                    role="admin",
                )
            )
            session.commit()

    def tearDown(self) -> None:
        self.engine.dispose()
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def test_checkout_aliases_are_idempotent_by_transaction_id(self) -> None:
        payload = {
            "amount": 5000,
            "currency": "IQD",
            "description": "Top-up checkout",
            "metadata": {
                "transactionId": "tx-1001",
                "serviceType": "esim",
                "customerUserId": self.known_user_id,
            },
        }

        first = self.client.post("/api/v1/payments/fib/checkout", json=payload)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json().get("paymentId"), "fib-pay-1")
        self.assertEqual(first.json().get("status"), "pending")

        second = self.client.post("/api/v1/payments/fib/create", json=payload)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json().get("paymentId"), "fib-pay-1")
        self.assertEqual(second.json().get("transactionId"), "tx-1001")

        with self.session_factory() as session:
            rows = session.scalars(select(PaymentAttempt)).all()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].transaction_id, "tx-1001")

    def test_checkout_with_non_uuid_user_ref_returns_422(self) -> None:
        payload = {
            "amount": 5000,
            "currency": "IQD",
            "description": "Checkout non-uuid user",
            "metadata": {
                "transactionId": "tx-non-uuid-user",
                "customerUserId": "mobile-user-abc",
            },
        }
        response = self.client.post("/api/v1/payments/fib/checkout", json=payload)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json().get("success"), False)
        self.assertEqual(response.json().get("errorCode"), "INVALID_PAYMENT_REQUEST")
        with self.session_factory() as session:
            row = session.scalar(
                select(PaymentAttempt).where(PaymentAttempt.transaction_id == "tx-non-uuid-user")
            )
            self.assertIsNone(row)

    def test_checkout_with_unknown_uuid_user_ref_returns_422(self) -> None:
        unknown_user_id = str(uuid.uuid4())
        payload = {
            "amount": 5000,
            "currency": "IQD",
            "description": "Checkout unknown uuid user",
            "metadata": {
                "transactionId": "tx-unknown-uuid-user",
                "customerUserId": unknown_user_id,
            },
        }
        response = self.client.post("/api/v1/payments/fib/checkout", json=payload)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json().get("success"), False)
        self.assertEqual(response.json().get("errorCode"), "INVALID_PAYMENT_REQUEST")
        with self.session_factory() as session:
            row = session.scalar(
                select(PaymentAttempt).where(PaymentAttempt.transaction_id == "tx-unknown-uuid-user")
            )
            self.assertIsNone(row)

    def test_checkout_with_known_uuid_user_ref_links_user(self) -> None:
        payload = {
            "amount": 5000,
            "currency": "IQD",
            "description": "Checkout known uuid user",
            "metadata": {
                "transactionId": "tx-known-uuid-user",
                "customerUserId": self.known_user_id,
                "userId": str(uuid.uuid4()),  # should prefer customerUserId
            },
        }
        response = self.client.post("/api/v1/payments/fib/checkout", json=payload)
        self.assertEqual(response.status_code, 200)
        with self.session_factory() as session:
            row = session.scalar(
                select(PaymentAttempt).where(PaymentAttempt.transaction_id == "tx-known-uuid-user")
            )
            self.assertIsNotNone(row)
            assert row is not None
            self.assertEqual(row.user_id, self.known_user_id)
            self.assertEqual(row.external_user_ref, self.known_user_id)

    def test_checkout_with_known_admin_uuid_links_admin_user(self) -> None:
        payload = {
            "amount": 5000,
            "currency": "IQD",
            "description": "Checkout known admin uuid user",
            "metadata": {
                "transactionId": "tx-known-admin-uuid-user",
                "customerUserId": self.known_admin_id,
            },
        }
        response = self.client.post("/api/v1/payments/fib/checkout", json=payload)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json().get("adminUserId"), self.known_admin_id)
        with self.session_factory() as session:
            row = session.scalar(
                select(PaymentAttempt).where(PaymentAttempt.transaction_id == "tx-known-admin-uuid-user")
            )
            self.assertIsNotNone(row)
            assert row is not None
            self.assertIsNone(row.user_id)
            self.assertEqual(row.admin_user_id, self.known_admin_id)

    def test_get_payment_updates_status_to_paid(self) -> None:
        payload = {
            "amount": 5000,
            "currency": "IQD",
            "description": "Top-up checkout",
            "metadata": {"transactionId": "tx-2002", "customerUserId": self.known_user_id},
        }
        create_response = self.client.post("/api/v1/payments/fib/checkout", json=payload)
        self.assertEqual(create_response.status_code, 200)

        status_response = self.client.get("/api/v1/payments/fib/fib-pay-1?refresh=true")
        self.assertEqual(status_response.status_code, 200)
        self.assertEqual(status_response.json().get("status"), "paid")
        self.assertTrue(status_response.json().get("paidAt"))

    def test_invalid_payment_id_returns_json_404(self) -> None:
        response = self.client.get("/api/v1/payments/fib/does-not-exist")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json().get("success"), False)
        self.assertEqual(response.json().get("errorCode"), "FIB_PAYMENT_NOT_FOUND")

    def test_webhook_requires_secret_when_configured(self) -> None:
        rejected = self.client.post(
            "/api/v1/payments/fib/webhook",
            json={"paymentId": "fib-pay-1", "status": "PAID"},
        )
        self.assertEqual(rejected.status_code, 401)

        accepted = self.client.post(
            "/api/v1/payments/fib/webhook",
            headers={"X-FIB-WEBHOOK-SECRET": "whsec"},
            json={"paymentId": "fib-pay-1", "status": "PAID"},
        )
        self.assertEqual(accepted.status_code, 202)
        self.assertEqual(accepted.json().get("success"), True)

    def test_webhook_is_idempotent_by_provider_event_id(self) -> None:
        payload = {
            "amount": 5000,
            "currency": "IQD",
            "description": "Top-up checkout",
            "metadata": {"transactionId": "tx-webhook-1", "customerUserId": self.known_user_id},
        }
        create_response = self.client.post("/api/v1/payments/fib/checkout", json=payload)
        self.assertEqual(create_response.status_code, 200)

        first = self.client.post(
            "/api/v1/payments/fib/webhook",
            headers={"X-FIB-WEBHOOK-SECRET": "whsec"},
            json={"eventId": "evt-100", "paymentId": "fib-pay-1", "status": "PAID"},
        )
        self.assertEqual(first.status_code, 202)
        self.assertEqual(first.json().get("transitionApplied"), True)

        second = self.client.post(
            "/api/v1/payments/fib/webhook",
            headers={"X-FIB-WEBHOOK-SECRET": "whsec"},
            json={"eventId": "evt-100", "paymentId": "fib-pay-1", "status": "PAID"},
        )
        self.assertEqual(second.status_code, 202)
        self.assertEqual(second.json().get("duplicateEvent"), True)

        with self.session_factory() as session:
            events = session.scalars(select(PaymentProviderEvent)).all()
            self.assertEqual(len(events), 1)


if __name__ == "__main__":
    unittest.main()
