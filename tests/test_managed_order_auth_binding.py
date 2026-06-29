from __future__ import annotations

import os
import tempfile
import unittest
import uuid
from typing import Generator

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from auth import create_access_token
from config import get_settings
from esim_access_api import register_esim_access_routes
from fib_payment_api import PaymentAmount, PaymentStatusResponse
from supabase_store import (
    AppUser,
    Base,
    CustomerOrder,
    ExchangeRate,
    OrderItem,
    PaymentAttempt,
    SupabaseStore,
    normalize_database_url,
)


class _ManagedOrderProvider:
    async def order_profiles(self, payload):
        # Echo the request transactionId like the real eSIM Access provider does
        # so OrderItem.provider_transaction_id matches the request.
        txn = payload.transaction_id
        return type(
            "ProviderResponse",
            (),
            {
                "model_dump": staticmethod(
                    lambda **_: {
                        "success": True,
                        "errorCode": "0",
                        "obj": {"orderNo": "ORD-PROVIDER-1", "transactionId": txn},
                    }
                )
            },
        )()


class _FakeFibProvider:
    def __init__(self, *, status: str = "PAID", amount: int = 0, currency: str = "IQD") -> None:
        self.status = status
        self.amount = amount
        self.currency = currency

    async def get_payment_status(self, payment_id: str) -> PaymentStatusResponse:
        return PaymentStatusResponse(
            payment_id=payment_id,
            status=self.status,
            amount=PaymentAmount(amount=self.amount, currency=self.currency),
        )


class ManagedOrderAuthBindingTest(unittest.TestCase):
    def setUp(self) -> None:
        temp_db = tempfile.NamedTemporaryFile(prefix="managed_order_", suffix=".db", delete=False)
        temp_db.close()
        self.db_path = temp_db.name
        self.user_id = str(uuid.uuid4())
        self.user_phone = "+9647701230001"
        self.loyalty_user_id = str(uuid.uuid4())
        self.loyalty_user_phone = "+9647701230002"
        os.environ["AUTH_SECRET_KEY"] = "test-auth-secret"
        os.environ["ESIM_ACCESS_ACCESS_CODE"] = "test-code"
        os.environ["ESIM_ACCESS_SECRET_KEY"] = "test-secret"
        get_settings.cache_clear()

        engine = create_engine(
            normalize_database_url(f"sqlite:///{self.db_path}"),
            connect_args={"check_same_thread": False},
            future=True,
        )
        Base.metadata.create_all(engine)
        self.session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
        with self.session_factory() as session:
            session.add(
                AppUser(
                    id=self.user_id,
                    phone=self.user_phone,
                    name="Token User",
                    email="token-user@example.com",
                    status="active",
                )
            )
            session.add(
                AppUser(
                    id=self.loyalty_user_id,
                    phone=self.loyalty_user_phone,
                    name="Loyalty User",
                    email="loyalty-user@example.com",
                    status="active",
                    is_loyalty=True,
                )
            )
            # USD->IQD rate so server-recomputed FIB totals are realistic (>0).
            session.add(
                ExchangeRate(base_currency="USD", quote_currency="IQD", rate=1450.0, active=True)
            )
            session.commit()

        app = FastAPI()

        def _get_db() -> Generator[Session, None, None]:
            session = self.session_factory()
            try:
                yield session
            finally:
                session.close()

        def _get_provider() -> _ManagedOrderProvider:
            return _ManagedOrderProvider()

        register_esim_access_routes(app, _get_db, _get_provider)
        self.app = app
        self.client = TestClient(app)
        self.engine = engine

    def _expected_total(self, package_code: str, provider_price_minor: int, country_code: str | None = None) -> int:
        with self.session_factory() as session:
            quote = SupabaseStore(session).quote_esim_sale_prices(
                [
                    {
                        "packageCode": package_code,
                        "countryCode": country_code,
                        "providerPriceMinor": provider_price_minor,
                    }
                ],
                currency_code="IQD",
            )
        return quote[package_code]

    def _seed_fib_attempt(self, *, provider_payment_id: str, amount_minor: int, user_id: str) -> None:
        with self.session_factory() as session:
            store = SupabaseStore(session)
            store.create_payment_attempt(
                transaction_id=f"fib-int-{provider_payment_id}",
                payment_method="fib",
                provider="fib",
                provider_payment_id=provider_payment_id,
                status="pending",
                amount_minor=amount_minor,
                currency_code="IQD",
                user_id=user_id,
                service_type="esim",
            )
            session.commit()

    def tearDown(self) -> None:
        self.engine.dispose()
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
        get_settings.cache_clear()

    def _user_auth_header(self) -> dict[str, str]:
        token = create_access_token(
            subject_id=self.user_id,
            phone=self.user_phone,
            subject_type="user",
            secret_key="test-auth-secret",
            ttl_seconds=3600,
        )
        return {"Authorization": f"Bearer {token}"}

    def _loyalty_auth_header(self) -> dict[str, str]:
        token = create_access_token(
            subject_id=self.loyalty_user_id,
            phone=self.loyalty_user_phone,
            subject_type="user",
            secret_key="test-auth-secret",
            ttl_seconds=3600,
        )
        return {"Authorization": f"Bearer {token}"}

    def test_managed_order_requires_authenticated_user(self) -> None:
        response = self.client.post(
            "/api/v1/esim-access/orders/managed",
            json={
                "providerRequest": {
                    "transactionId": "APP-ORDER-10001",
                    "packageInfoList": [{"packageCode": "PKG-001", "count": 1, "price": 1000}],
                },
                "user": {"phone": "+9647700000000", "name": "Payload User"},
                "platformCode": "mobile_app",
            },
        )
        self.assertEqual(response.status_code, 401)

    def test_managed_order_uses_token_owner_not_payload_user(self) -> None:
        # The order must bind to the TOKEN subject, never the (spoofable) payload
        # user — verified here through the real FIB-verified checkout path.
        expected = self._expected_total("PKG-001", 100000, country_code="US")
        self._seed_fib_attempt(provider_payment_id="PAY-OWNER-1", amount_minor=expected, user_id=self.user_id)
        self.app.state.fib_payment_api = _FakeFibProvider(status="PAID", amount=expected, currency="IQD")
        response = self.client.post(
            "/api/v1/esim-access/orders/managed",
            headers=self._user_auth_header(),
            json={
                "providerRequest": {
                    "transactionId": "APP-ORDER-20001",
                    "packageInfoList": [{"packageCode": "PKG-001", "count": 1, "price": 100000}],
                },
                "user": {
                    "phone": "+9647711111111",
                    "name": "Spoofed Payload User",
                    "email": "spoof@example.com",
                },
                "platformCode": "mobile_app",
                "currencyCode": "IQD",
                "providerCurrencyCode": "USD",
                "countryCode": "US",
                "countryName": "United States",
                "packageCode": "PKG-001",
                "paymentMethod": "fib",
                "paymentTransactionId": "PAY-OWNER-1",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json().get("success"))
        self.assertEqual(response.json().get("providerOrderNo"), "ORD-PROVIDER-1")
        order_id = response.json()["database"]["customerOrderId"]
        with self.session_factory() as session:
            order = session.scalar(select(CustomerOrder).where(CustomerOrder.id == order_id))
            self.assertIsNotNone(order)
            assert order is not None
            self.assertEqual(order.user_id, self.user_id)

    def test_managed_order_loyalty_creates_payment_attempt_and_order_payment_fields(self) -> None:
        response = self.client.post(
            "/api/v1/esim-access/orders/managed",
            headers=self._loyalty_auth_header(),
            json={
                "providerRequest": {
                    "transactionId": "APP-ORDER-LOYALTY-1",
                    "packageInfoList": [{"packageCode": "PKG-LOYALTY-1", "count": 1, "price": 900}],
                },
                "user": {
                    "phone": self.loyalty_user_phone,
                    "name": "Loyalty User",
                    "email": "loyalty-user@example.com",
                },
                "platformCode": "mobile_app",
                "currencyCode": "IQD",
                "providerCurrencyCode": "IQD",
                "customFields": {
                    "paymentMethod": "loyalty",
                    "paymentStatus": "approved",
                },
            },
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        payment = payload.get("database", {}).get("payment")
        self.assertIsNotNone(payment)
        assert payment is not None
        self.assertEqual(payment.get("paymentMethod"), "loyalty")
        self.assertEqual(payment.get("status"), "paid")

        order_id = payload["database"]["customerOrderId"]
        order_item_id = payload["database"]["orderItemId"]
        with self.session_factory() as session:
            attempt = session.scalar(select(PaymentAttempt).where(PaymentAttempt.order_item_id == order_item_id))
            self.assertIsNotNone(attempt)
            assert attempt is not None
            self.assertEqual(attempt.payment_method, "loyalty")
            self.assertEqual(attempt.status, "paid")

            order = session.scalar(select(CustomerOrder).where(CustomerOrder.id == order_id))
            self.assertIsNotNone(order)
            assert order is not None
            self.assertEqual(order.payment_method, "loyalty")

            item = session.scalar(select(OrderItem).where(OrderItem.id == order_item_id))
            self.assertIsNotNone(item)
            assert item is not None
            self.assertEqual(item.payment_method, "loyalty")

    def test_managed_order_loyalty_rejected_for_non_loyalty_user(self) -> None:
        # A normal (non-loyalty) account must NOT be able to comp a purchase via
        # paymentMethod=loyalty, even if the client sends it. The guard rejects
        # with 403 before any provider order is placed, and no order/payment row
        # is created.
        response = self.client.post(
            "/api/v1/esim-access/orders/managed",
            headers=self._user_auth_header(),
            json={
                "providerRequest": {
                    "transactionId": "APP-ORDER-LOYALTY-DENY-1",
                    "packageInfoList": [{"packageCode": "PKG-LOYALTY-2", "count": 1, "price": 900}],
                },
                "user": {
                    "phone": self.user_phone,
                    "name": "Token User",
                    "email": "token-user@example.com",
                },
                "platformCode": "mobile_app",
                "currencyCode": "IQD",
                "providerCurrencyCode": "IQD",
                "customFields": {
                    "paymentMethod": "loyalty",
                    "paymentStatus": "approved",
                },
            },
        )
        self.assertEqual(response.status_code, 403)
        with self.session_factory() as session:
            orders = session.scalars(
                select(CustomerOrder).where(CustomerOrder.user_id == self.user_id)
            ).all()
            self.assertEqual(orders, [])


if __name__ == "__main__":
    unittest.main()
