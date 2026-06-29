"""SEC-1 / SEC-2 regression tests for managed-order checkout.

The managed-order endpoint provisions a real eSIM (spends provider credit), so it
MUST verify the FIB payment server-side BEFORE calling the provider, and the paid
amount MUST match a server-recomputed total. These tests pin that contract:

  * an unpaid / forged / underpaid / mis-owned / replayed payment never reaches
    the provider (``order_profiles`` is asserted *not* called);
  * a genuinely paid, correctly-priced, owned payment provisions exactly once.
"""
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


class _RecordingEsimProvider:
    """Records how many times the provider order was placed, and echoes the
    request transactionId like the real eSIM Access provider does."""

    def __init__(self) -> None:
        self.order_calls = 0

    async def order_profiles(self, request):
        self.order_calls += 1
        txn = request.transaction_id
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
        self.calls: list[str] = []

    async def get_payment_status(self, payment_id: str) -> PaymentStatusResponse:
        self.calls.append(payment_id)
        return PaymentStatusResponse(
            payment_id=payment_id,
            status=self.status,
            amount=PaymentAmount(amount=self.amount, currency=self.currency),
        )


class ManagedOrderPaymentVerificationTest(unittest.TestCase):
    def setUp(self) -> None:
        temp_db = tempfile.NamedTemporaryFile(prefix="managed_order_pay_", suffix=".db", delete=False)
        temp_db.close()
        self.db_path = temp_db.name
        self.user_id = str(uuid.uuid4())
        self.user_phone = "+9647701230001"
        self.other_user_id = str(uuid.uuid4())
        self.other_user_phone = "+9647701230002"
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
                AppUser(id=self.user_id, phone=self.user_phone, name="Buyer", status="active")
            )
            session.add(
                AppUser(id=self.other_user_id, phone=self.other_user_phone, name="Other", status="active")
            )
            # A realistic USD->IQD rate so recomputed totals are non-zero and the
            # underpayment test is meaningful.
            session.add(
                ExchangeRate(base_currency="USD", quote_currency="IQD", rate=1450.0, active=True)
            )
            session.commit()

        app = FastAPI()
        self.esim_provider = _RecordingEsimProvider()

        def _get_db() -> Generator[Session, None, None]:
            session = self.session_factory()
            try:
                yield session
            finally:
                session.close()

        def _get_provider() -> _RecordingEsimProvider:
            return self.esim_provider

        register_esim_access_routes(app, _get_db, _get_provider)
        app.state.fib_payment_api = _FakeFibProvider()
        self.app = app
        self.client = TestClient(app)
        self.engine = engine

    def tearDown(self) -> None:
        self.engine.dispose()
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
        get_settings.cache_clear()

    # -- helpers ---------------------------------------------------------------

    def _auth_header(self, user_id: str, phone: str) -> dict[str, str]:
        token = create_access_token(
            subject_id=user_id,
            phone=phone,
            subject_type="user",
            secret_key="test-auth-secret",
            ttl_seconds=3600,
        )
        return {"Authorization": f"Bearer {token}"}

    def _expected_total(self, package_code: str, provider_price_minor: int, country_code: str | None = "US") -> int:
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

    def _seed_fib_attempt(
        self,
        *,
        provider_payment_id: str,
        amount_minor: int,
        user_id: str,
        bound_to_other_order: bool = False,
    ) -> str:
        with self.session_factory() as session:
            store = SupabaseStore(session)
            customer_order_id = None
            order_item_id = None
            if bound_to_other_order:
                order = CustomerOrder(order_number=store.build_order_number())
                session.add(order)
                item = OrderItem(service_type="esim", provider_transaction_id="OTHER-ORDER-TXN")
                item.customer_order = order
                session.add(item)
                session.flush()
                customer_order_id = order.id
                order_item_id = item.id
            attempt = store.create_payment_attempt(
                transaction_id=f"fib-int-{provider_payment_id}",
                payment_method="fib",
                provider="fib",
                provider_payment_id=provider_payment_id,
                status="pending",
                amount_minor=amount_minor,
                currency_code="IQD",
                user_id=user_id,
                service_type="esim",
                customer_order_id=customer_order_id,
                order_item_id=order_item_id,
            )
            session.commit()
            return attempt.id

    def _order_payload(self, *, transaction_id: str, provider_payment_id: str | None, price: int = 100000) -> dict:
        body: dict = {
            "providerRequest": {
                "transactionId": transaction_id,
                "packageInfoList": [{"packageCode": "PKG-FIB-1", "count": 1, "price": price}],
            },
            "user": {"phone": self.user_phone, "name": "Buyer"},
            "platformCode": "mobile_app",
            "currencyCode": "IQD",
            "providerCurrencyCode": "USD",
            "countryCode": "US",
            "countryName": "United States",
            "packageCode": "PKG-FIB-1",
            "paymentMethod": "fib",
        }
        if provider_payment_id is not None:
            body["paymentTransactionId"] = provider_payment_id
        return body

    def _orders(self) -> list[CustomerOrder]:
        with self.session_factory() as session:
            return list(session.scalars(select(CustomerOrder)).all())

    # -- tests -----------------------------------------------------------------

    def test_happy_path_provisions_only_after_verified_paid(self) -> None:
        expected = self._expected_total("PKG-FIB-1", 100000)
        self.assertGreater(expected, 0)
        self._seed_fib_attempt(provider_payment_id="PAY-1", amount_minor=expected, user_id=self.user_id)
        self.app.state.fib_payment_api = _FakeFibProvider(status="PAID", amount=expected, currency="IQD")

        resp = self.client.post(
            "/api/v1/esim-access/orders/managed",
            headers=self._auth_header(self.user_id, self.user_phone),
            json=self._order_payload(transaction_id="APP-OK-1", provider_payment_id="PAY-1"),
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(self.esim_provider.order_calls, 1)
        order_id = resp.json()["database"]["customerOrderId"]
        with self.session_factory() as session:
            order = session.scalar(select(CustomerOrder).where(CustomerOrder.id == order_id))
            assert order is not None
            self.assertEqual(order.user_id, self.user_id)
            self.assertEqual(order.total_minor, expected)
            attempt = session.scalar(select(PaymentAttempt).where(PaymentAttempt.provider_payment_id == "PAY-1"))
            assert attempt is not None
            self.assertEqual(attempt.status, "paid")
            self.assertEqual(attempt.customer_order_id, order_id)

    def test_unpaid_payment_blocks_provisioning(self) -> None:
        expected = self._expected_total("PKG-FIB-1", 100000)
        self._seed_fib_attempt(provider_payment_id="PAY-2", amount_minor=expected, user_id=self.user_id)
        self.app.state.fib_payment_api = _FakeFibProvider(status="DECLINED", amount=expected)

        resp = self.client.post(
            "/api/v1/esim-access/orders/managed",
            headers=self._auth_header(self.user_id, self.user_phone),
            json=self._order_payload(transaction_id="APP-UNPAID-1", provider_payment_id="PAY-2"),
        )
        self.assertEqual(resp.status_code, 402)
        self.assertEqual(self.esim_provider.order_calls, 0)
        self.assertEqual(self._orders(), [])

    def test_underpaid_amount_rejected(self) -> None:
        expected = self._expected_total("PKG-FIB-1", 100000)
        self._seed_fib_attempt(provider_payment_id="PAY-3", amount_minor=expected, user_id=self.user_id)
        # FIB reports a lower amount than the server-recomputed total.
        self.app.state.fib_payment_api = _FakeFibProvider(status="PAID", amount=expected - 250)

        resp = self.client.post(
            "/api/v1/esim-access/orders/managed",
            headers=self._auth_header(self.user_id, self.user_phone),
            json=self._order_payload(transaction_id="APP-UNDER-1", provider_payment_id="PAY-3"),
        )
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(self.esim_provider.order_calls, 0)
        self.assertEqual(self._orders(), [])

    def test_currency_mismatch_rejected(self) -> None:
        expected = self._expected_total("PKG-FIB-1", 100000)
        self._seed_fib_attempt(provider_payment_id="PAY-3b", amount_minor=expected, user_id=self.user_id)
        self.app.state.fib_payment_api = _FakeFibProvider(status="PAID", amount=expected, currency="USD")

        resp = self.client.post(
            "/api/v1/esim-access/orders/managed",
            headers=self._auth_header(self.user_id, self.user_phone),
            json=self._order_payload(transaction_id="APP-CUR-1", provider_payment_id="PAY-3b"),
        )
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(self.esim_provider.order_calls, 0)

    def test_payment_owned_by_other_user_rejected(self) -> None:
        expected = self._expected_total("PKG-FIB-1", 100000)
        # Attempt belongs to a different account than the token subject.
        self._seed_fib_attempt(provider_payment_id="PAY-4", amount_minor=expected, user_id=self.other_user_id)
        self.app.state.fib_payment_api = _FakeFibProvider(status="PAID", amount=expected)

        resp = self.client.post(
            "/api/v1/esim-access/orders/managed",
            headers=self._auth_header(self.user_id, self.user_phone),
            json=self._order_payload(transaction_id="APP-OWN-1", provider_payment_id="PAY-4"),
        )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(self.esim_provider.order_calls, 0)

    def test_missing_payment_reference_rejected(self) -> None:
        self.app.state.fib_payment_api = _FakeFibProvider(status="PAID", amount=1)
        resp = self.client.post(
            "/api/v1/esim-access/orders/managed",
            headers=self._auth_header(self.user_id, self.user_phone),
            json=self._order_payload(transaction_id="APP-NOREF-1", provider_payment_id=None),
        )
        self.assertEqual(resp.status_code, 402)
        self.assertEqual(self.esim_provider.order_calls, 0)

    def test_replayed_payment_rejected(self) -> None:
        expected = self._expected_total("PKG-FIB-1", 100000)
        # Attempt already finalized a different order.
        self._seed_fib_attempt(
            provider_payment_id="PAY-5",
            amount_minor=expected,
            user_id=self.user_id,
            bound_to_other_order=True,
        )
        self.app.state.fib_payment_api = _FakeFibProvider(status="PAID", amount=expected)

        resp = self.client.post(
            "/api/v1/esim-access/orders/managed",
            headers=self._auth_header(self.user_id, self.user_phone),
            json=self._order_payload(transaction_id="APP-REPLAY-1", provider_payment_id="PAY-5"),
        )
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(self.esim_provider.order_calls, 0)

    def test_fib_unconfigured_returns_503(self) -> None:
        expected = self._expected_total("PKG-FIB-1", 100000)
        self._seed_fib_attempt(provider_payment_id="PAY-6", amount_minor=expected, user_id=self.user_id)
        self.app.state.fib_payment_api = None

        resp = self.client.post(
            "/api/v1/esim-access/orders/managed",
            headers=self._auth_header(self.user_id, self.user_phone),
            json=self._order_payload(transaction_id="APP-503-1", provider_payment_id="PAY-6"),
        )
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(self.esim_provider.order_calls, 0)

    def test_idempotent_resubmit_does_not_create_second_order(self) -> None:
        expected = self._expected_total("PKG-FIB-1", 100000)
        self._seed_fib_attempt(provider_payment_id="PAY-7", amount_minor=expected, user_id=self.user_id)
        self.app.state.fib_payment_api = _FakeFibProvider(status="PAID", amount=expected)

        payload = self._order_payload(transaction_id="APP-IDEM-1", provider_payment_id="PAY-7")
        headers = self._auth_header(self.user_id, self.user_phone)
        first = self.client.post("/api/v1/esim-access/orders/managed", headers=headers, json=payload)
        second = self.client.post("/api/v1/esim-access/orders/managed", headers=headers, json=payload)

        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(
            first.json()["database"]["customerOrderId"],
            second.json()["database"]["customerOrderId"],
        )
        self.assertEqual(len(self._orders()), 1)


if __name__ == "__main__":
    unittest.main()
