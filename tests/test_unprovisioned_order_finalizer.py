"""Recovery tests for paid-but-unprovisioned FIB orders.

A confirmed FIB payment only becomes an eSIM when the app calls /orders/managed.
If the app dies in between, the customer is charged and nothing is delivered —
and we do not refund. ``finalize_paid_fib_order`` / ``sweep_unprovisioned_fib_orders``
close that gap. These tests pin the contract:

  * a paid, unbound payment carrying an orderIntent provisions exactly once and
    ends up bound to the resulting order;
  * recovery reuses the SAME gate as the client path, so an unpaid, underpaid,
    mis-owned or already-bound payment never reaches the provider;
  * recovery is idempotent (a second sweep is a no-op) and never races a live
    checkout still inside the grace window.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
import uuid
from datetime import timedelta
from typing import Generator

from fastapi import FastAPI
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from config import get_settings
from esim_access_api import (
    finalize_paid_fib_order,
    finalize_paid_fib_topup,
    register_esim_access_routes,
    sweep_unprovisioned_fib_orders,
)
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
    utcnow,
)


class _RecordingEsimProvider:
    def __init__(self) -> None:
        self.order_calls = 0
        self.transaction_ids: list[str] = []

    async def order_profiles(self, request):
        self.order_calls += 1
        txn = request.transaction_id
        self.transaction_ids.append(txn)
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


class UnprovisionedOrderFinalizerTest(unittest.TestCase):
    PACKAGE_CODE = "PKG-FIB-1"
    PROVIDER_PRICE_MINOR = 100000

    def setUp(self) -> None:
        temp_db = tempfile.NamedTemporaryFile(prefix="finalize_order_", suffix=".db", delete=False)
        temp_db.close()
        self.db_path = temp_db.name
        self.user_id = str(uuid.uuid4())
        self.user_phone = "+9647701230001"
        self.other_user_id = str(uuid.uuid4())
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
            session.add(AppUser(id=self.user_id, phone=self.user_phone, name="Buyer", status="active"))
            session.add(
                AppUser(id=self.other_user_id, phone="+9647701230002", name="Other", status="active")
            )
            session.add(ExchangeRate(base_currency="USD", quote_currency="IQD", rate=1450.0, active=True))
            session.commit()

        app = FastAPI()
        self.esim_provider = _RecordingEsimProvider()

        def _get_db() -> Generator[Session, None, None]:
            session = self.session_factory()
            try:
                yield session
            finally:
                session.close()

        register_esim_access_routes(app, _get_db, lambda: self.esim_provider)
        self.app = app
        self.engine = engine

    def tearDown(self) -> None:
        self.engine.dispose()
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
        get_settings.cache_clear()

    # -- helpers ---------------------------------------------------------------

    def _expected_total(self) -> int:
        with self.session_factory() as session:
            quote = SupabaseStore(session).quote_esim_sale_prices(
                [
                    {
                        "packageCode": self.PACKAGE_CODE,
                        "countryCode": "US",
                        "providerPriceMinor": self.PROVIDER_PRICE_MINOR,
                    }
                ],
                currency_code="IQD",
            )
        return quote[self.PACKAGE_CODE]

    def _order_intent(self, *, transaction_id: str, provider_price_minor: int | None = None) -> dict:
        return {
            "transactionId": transaction_id,
            "packageCode": self.PACKAGE_CODE,
            "count": 1,
            "periodNum": 7,
            "providerPriceMinor": provider_price_minor or self.PROVIDER_PRICE_MINOR,
            "countryCode": "US",
            "countryName": "United States",
            "packageName": "United States 5 GB · 7d",
            "currencyCode": "IQD",
            "providerCurrencyCode": "USD",
            "platformCode": "tulip-mobile-app",
            "platformName": "Tulip Mobile App",
        }

    def _seed_attempt(
        self,
        *,
        provider_payment_id: str,
        amount_minor: int,
        status: str = "paid",
        user_id: str | None = None,
        metadata: dict | None = None,
        paid_minutes_ago: int = 60,
        bound_to_order: bool = False,
    ) -> str:
        with self.session_factory() as session:
            store = SupabaseStore(session)
            customer_order_id = None
            order_item_id = None
            if bound_to_order:
                order = CustomerOrder(order_number=store.build_order_number())
                session.add(order)
                item = OrderItem(service_type="esim", provider_transaction_id="ALREADY-ORDERED")
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
                status=status,
                amount_minor=amount_minor,
                currency_code="IQD",
                user_id=user_id or self.user_id,
                service_type="esim",
                customer_order_id=customer_order_id,
                order_item_id=order_item_id,
                metadata=metadata or {},
            )
            session.flush()
            attempt.paid_at = utcnow() - timedelta(minutes=paid_minutes_ago)
            session.commit()
            return attempt.id

    def _finalize(self, attempt_id: str, *, fib: _FakeFibProvider, grace_seconds: int = 0):
        async def _run():
            with self.session_factory() as session:
                return await finalize_paid_fib_order(
                    attempt_id=attempt_id,
                    esim_provider=self.esim_provider,
                    fib_provider=fib,
                    db=session,
                    grace_seconds=grace_seconds,
                )

        return asyncio.run(_run())

    def _sweep(self, *, fib: _FakeFibProvider, grace_seconds: int = 0) -> dict:
        async def _run():
            with self.session_factory() as session:
                return await sweep_unprovisioned_fib_orders(
                    esim_provider=self.esim_provider,
                    fib_provider=fib,
                    db=session,
                    grace_seconds=grace_seconds,
                )

        return asyncio.run(_run())

    def _attempt(self, attempt_id: str) -> PaymentAttempt:
        with self.session_factory() as session:
            row = session.scalar(select(PaymentAttempt).where(PaymentAttempt.id == attempt_id))
            assert row is not None
            return row

    # -- tests -----------------------------------------------------------------

    def test_recovers_paid_order_and_binds_payment(self) -> None:
        expected = self._expected_total()
        attempt_id = self._seed_attempt(
            provider_payment_id="PAY-REC-1",
            amount_minor=expected,
            metadata={"orderIntent": self._order_intent(transaction_id="APP-REC-1")},
        )
        fib = _FakeFibProvider(status="PAID", amount=expected)

        provider_order_no = self._finalize(attempt_id, fib=fib)

        self.assertEqual(provider_order_no, "ORD-PROVIDER-1")
        self.assertEqual(self.esim_provider.order_calls, 1)
        # The recovered order must use the id the app already reserved.
        self.assertEqual(self.esim_provider.transaction_ids, ["APP-REC-1"])
        attempt = self._attempt(attempt_id)
        self.assertEqual(attempt.status, "paid")
        self.assertIsNotNone(attempt.customer_order_id)
        self.assertIsNotNone(attempt.order_item_id)
        with self.session_factory() as session:
            order = session.scalar(
                select(CustomerOrder).where(CustomerOrder.id == attempt.customer_order_id)
            )
            assert order is not None
            self.assertEqual(order.user_id, self.user_id)
            self.assertEqual(order.total_minor, expected)

    def test_second_run_is_a_no_op(self) -> None:
        expected = self._expected_total()
        attempt_id = self._seed_attempt(
            provider_payment_id="PAY-REC-2",
            amount_minor=expected,
            metadata={"orderIntent": self._order_intent(transaction_id="APP-REC-2")},
        )
        fib = _FakeFibProvider(status="PAID", amount=expected)

        self._finalize(attempt_id, fib=fib)
        self.assertEqual(self.esim_provider.order_calls, 1)

        # Re-running must not place a second provider order for the same charge.
        again = self._finalize(attempt_id, fib=fib)
        self.assertIsNone(again)
        self.assertEqual(self.esim_provider.order_calls, 1)
        self.assertEqual(len(self._orders()), 1)

    def test_grace_window_defers_a_live_checkout(self) -> None:
        expected = self._expected_total()
        attempt_id = self._seed_attempt(
            provider_payment_id="PAY-REC-3",
            amount_minor=expected,
            metadata={"orderIntent": self._order_intent(transaction_id="APP-REC-3")},
            paid_minutes_ago=0,
        )
        fib = _FakeFibProvider(status="PAID", amount=expected)

        # Just-paid: the app may still be placing the order itself.
        self.assertIsNone(self._finalize(attempt_id, fib=fib, grace_seconds=300))
        self.assertEqual(self.esim_provider.order_calls, 0)

    def test_payment_already_used_for_an_order_is_skipped(self) -> None:
        expected = self._expected_total()
        attempt_id = self._seed_attempt(
            provider_payment_id="PAY-REC-4",
            amount_minor=expected,
            metadata={"orderIntent": self._order_intent(transaction_id="APP-REC-4")},
            bound_to_order=True,
        )
        fib = _FakeFibProvider(status="PAID", amount=expected)

        self.assertIsNone(self._finalize(attempt_id, fib=fib))
        self.assertEqual(self.esim_provider.order_calls, 0)

    def test_unpaid_payment_is_never_provisioned(self) -> None:
        expected = self._expected_total()
        attempt_id = self._seed_attempt(
            provider_payment_id="PAY-REC-5",
            amount_minor=expected,
            status="pending",
            metadata={"orderIntent": self._order_intent(transaction_id="APP-REC-5")},
        )
        fib = _FakeFibProvider(status="PAID", amount=expected)

        self.assertIsNone(self._finalize(attempt_id, fib=fib))
        self.assertEqual(self.esim_provider.order_calls, 0)

    def test_payment_without_order_intent_is_skipped(self) -> None:
        expected = self._expected_total()
        attempt_id = self._seed_attempt(
            provider_payment_id="PAY-REC-6",
            amount_minor=expected,
            metadata={"packageCode": self.PACKAGE_CODE},
        )
        fib = _FakeFibProvider(status="PAID", amount=expected)

        self.assertIsNone(self._finalize(attempt_id, fib=fib))
        self.assertEqual(self.esim_provider.order_calls, 0)

    def test_provider_says_not_paid_blocks_provisioning(self) -> None:
        expected = self._expected_total()
        attempt_id = self._seed_attempt(
            provider_payment_id="PAY-REC-7",
            amount_minor=expected,
            metadata={"orderIntent": self._order_intent(transaction_id="APP-REC-7")},
        )
        # Our row says paid, but FIB is the authority and says otherwise.
        fib = _FakeFibProvider(status="DECLINED", amount=expected)

        with self.assertRaises(Exception):
            self._finalize(attempt_id, fib=fib)
        self.assertEqual(self.esim_provider.order_calls, 0)

    def test_underpaid_amount_blocks_provisioning(self) -> None:
        expected = self._expected_total()
        attempt_id = self._seed_attempt(
            provider_payment_id="PAY-REC-8",
            amount_minor=expected,
            metadata={"orderIntent": self._order_intent(transaction_id="APP-REC-8")},
        )
        # Paid a fraction of the server-recomputed total.
        fib = _FakeFibProvider(status="PAID", amount=max(1, expected // 2))

        with self.assertRaises(Exception):
            self._finalize(attempt_id, fib=fib)
        self.assertEqual(self.esim_provider.order_calls, 0)

    def test_sweep_recovers_eligible_and_reports_failures(self) -> None:
        expected = self._expected_total()
        good_id = self._seed_attempt(
            provider_payment_id="PAY-SWEEP-OK",
            amount_minor=expected,
            metadata={"orderIntent": self._order_intent(transaction_id="APP-SWEEP-OK")},
        )
        # No intent -> silently skipped, not a failure.
        self._seed_attempt(
            provider_payment_id="PAY-SWEEP-SKIP",
            amount_minor=expected,
            metadata={},
        )
        fib = _FakeFibProvider(status="PAID", amount=expected)

        result = self._sweep(fib=fib)

        self.assertEqual(self.esim_provider.order_calls, 1)
        self.assertEqual(result["failed"], [])
        self.assertEqual(
            [entry["paymentAttemptId"] for entry in result["finalized"]], [good_id]
        )

    def _orders(self) -> list[CustomerOrder]:
        with self.session_factory() as session:
            return list(session.scalars(select(CustomerOrder)).all())


class _RecordingTopUpProvider:
    """Prices a TOPUP package from a catalog and records applied top-ups."""

    def __init__(self, *, package_code: str, price_minor: int) -> None:
        self.package_code = package_code
        self.price_minor = price_minor
        self.topup_calls = 0
        self.transaction_ids: list[str] = []

    async def get_packages(self, request):
        package_code = self.package_code
        price_minor = self.price_minor
        return type(
            "CatalogResponse",
            (),
            {
                "model_dump": staticmethod(
                    lambda **_: {
                        "success": True,
                        "obj": {
                            "packageList": [
                                {
                                    "packageCode": package_code,
                                    "price": price_minor,
                                    "location": "US",
                                }
                            ]
                        },
                    }
                )
            },
        )()

    async def top_up(self, request):
        self.topup_calls += 1
        self.transaction_ids.append(request.transaction_id)
        return type(
            "TopUpResponse",
            (),
            {
                "success": True,
                "model_dump": staticmethod(
                    lambda **_: {"success": True, "obj": {"orderNo": "TOPUP-ORDER-1"}}
                ),
            },
        )()


class UnappliedTopUpFinalizerTest(unittest.TestCase):
    """A top-up is 'buying again' and abandons the same way, so it gets the same
    recovery: paid via FIB but the app never called /topups/managed."""

    PACKAGE_CODE = "TOPUP-PKG-1"
    PROVIDER_PRICE_MINOR = 50000
    ICCID = "8964012345678901234"

    def setUp(self) -> None:
        temp_db = tempfile.NamedTemporaryFile(prefix="finalize_topup_", suffix=".db", delete=False)
        temp_db.close()
        self.db_path = temp_db.name
        self.user_id = str(uuid.uuid4())
        self.other_user_id = str(uuid.uuid4())
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
                AppUser(id=self.user_id, phone="+9647701230011", name="Topper", status="active")
            )
            session.add(
                AppUser(id=self.other_user_id, phone="+9647701230012", name="Other", status="active")
            )
            session.add(ExchangeRate(base_currency="USD", quote_currency="IQD", rate=1450.0, active=True))
            session.commit()
        self.engine = engine
        self.provider = _RecordingTopUpProvider(
            package_code=self.PACKAGE_CODE, price_minor=self.PROVIDER_PRICE_MINOR
        )

    def tearDown(self) -> None:
        self.engine.dispose()
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
        get_settings.cache_clear()

    # -- helpers ---------------------------------------------------------------

    def _seed_profile(self, *, user_id: str | None = None) -> None:
        from supabase_store import ESimProfile

        with self.session_factory() as session:
            session.add(
                ESimProfile(iccid=self.ICCID, user_id=user_id or self.user_id, app_status="active")
            )
            session.commit()

    def _expected_total(self) -> int:
        with self.session_factory() as session:
            quote = SupabaseStore(session).quote_esim_sale_prices(
                [
                    {
                        "packageCode": self.PACKAGE_CODE,
                        "countryCode": "US",
                        "providerPriceMinor": self.PROVIDER_PRICE_MINOR,
                    }
                ],
                currency_code="IQD",
            )
        return quote[self.PACKAGE_CODE]

    def _topup_intent(self, *, transaction_id: str = "TOPUP-REC-1") -> dict:
        return {
            "transactionId": transaction_id,
            "iccid": self.ICCID,
            "packageCode": self.PACKAGE_CODE,
        }

    def _seed_attempt(
        self,
        *,
        provider_payment_id: str,
        amount_minor: int,
        metadata: dict,
        status: str = "paid",
        user_id: str | None = None,
        paid_minutes_ago: int = 60,
    ) -> str:
        with self.session_factory() as session:
            store = SupabaseStore(session)
            attempt = store.create_payment_attempt(
                transaction_id=f"fib-topup-{provider_payment_id}",
                payment_method="fib",
                provider="fib",
                provider_payment_id=provider_payment_id,
                status=status,
                amount_minor=amount_minor,
                currency_code="IQD",
                user_id=user_id or self.user_id,
                service_type="esim",
                metadata=metadata,
            )
            session.flush()
            attempt.paid_at = utcnow() - timedelta(minutes=paid_minutes_ago)
            session.commit()
            return attempt.id

    def _finalize(self, attempt_id: str, *, fib: _FakeFibProvider, grace_seconds: int = 0):
        async def _run():
            with self.session_factory() as session:
                return await finalize_paid_fib_topup(
                    attempt_id=attempt_id,
                    esim_provider=self.provider,
                    fib_provider=fib,
                    db=session,
                    grace_seconds=grace_seconds,
                )

        return asyncio.run(_run())

    # -- tests -----------------------------------------------------------------

    def test_recovers_paid_topup(self) -> None:
        self._seed_profile()
        expected = self._expected_total()
        attempt_id = self._seed_attempt(
            provider_payment_id="PAYT-1",
            amount_minor=expected,
            metadata={"topupIntent": self._topup_intent()},
        )
        fib = _FakeFibProvider(status="PAID", amount=expected)

        result = self._finalize(attempt_id, fib=fib)

        self.assertEqual(result, "TOPUP-REC-1")
        self.assertEqual(self.provider.topup_calls, 1)
        # Applied under the id the app already reserved.
        self.assertEqual(self.provider.transaction_ids, ["TOPUP-REC-1"])

    def test_second_run_does_not_apply_twice(self) -> None:
        self._seed_profile()
        expected = self._expected_total()
        attempt_id = self._seed_attempt(
            provider_payment_id="PAYT-2",
            amount_minor=expected,
            metadata={"topupIntent": self._topup_intent(transaction_id="TOPUP-REC-2")},
        )
        fib = _FakeFibProvider(status="PAID", amount=expected)

        self._finalize(attempt_id, fib=fib)
        self.assertEqual(self.provider.topup_calls, 1)

        self.assertIsNone(self._finalize(attempt_id, fib=fib))
        self.assertEqual(self.provider.topup_calls, 1)

    def test_client_already_claimed_is_skipped(self) -> None:
        """The app applied the top-up itself — the claim must block a re-apply."""
        self._seed_profile()
        expected = self._expected_total()
        attempt_id = self._seed_attempt(
            provider_payment_id="PAYT-3",
            amount_minor=expected,
            metadata={
                "topupIntent": self._topup_intent(transaction_id="TOPUP-REC-3"),
                "topupClaim": {"transactionId": "TOPUP-REC-3"},
            },
        )
        fib = _FakeFibProvider(status="PAID", amount=expected)

        self.assertIsNone(self._finalize(attempt_id, fib=fib))
        self.assertEqual(self.provider.topup_calls, 0)

    def test_grace_window_defers_a_live_topup(self) -> None:
        self._seed_profile()
        expected = self._expected_total()
        attempt_id = self._seed_attempt(
            provider_payment_id="PAYT-4",
            amount_minor=expected,
            metadata={"topupIntent": self._topup_intent(transaction_id="TOPUP-REC-4")},
            paid_minutes_ago=0,
        )
        fib = _FakeFibProvider(status="PAID", amount=expected)

        self.assertIsNone(self._finalize(attempt_id, fib=fib, grace_seconds=300))
        self.assertEqual(self.provider.topup_calls, 0)

    def test_profile_owned_by_another_account_is_skipped(self) -> None:
        self._seed_profile(user_id=self.other_user_id)
        expected = self._expected_total()
        attempt_id = self._seed_attempt(
            provider_payment_id="PAYT-5",
            amount_minor=expected,
            metadata={"topupIntent": self._topup_intent(transaction_id="TOPUP-REC-5")},
        )
        fib = _FakeFibProvider(status="PAID", amount=expected)

        self.assertIsNone(self._finalize(attempt_id, fib=fib))
        self.assertEqual(self.provider.topup_calls, 0)

    def test_underpaid_topup_is_never_applied(self) -> None:
        self._seed_profile()
        expected = self._expected_total()
        attempt_id = self._seed_attempt(
            provider_payment_id="PAYT-6",
            amount_minor=expected,
            metadata={"topupIntent": self._topup_intent(transaction_id="TOPUP-REC-6")},
        )
        # Paid less than the server re-quoted total from the provider catalog.
        fib = _FakeFibProvider(status="PAID", amount=max(1, expected // 2))

        with self.assertRaises(Exception):
            self._finalize(attempt_id, fib=fib)
        self.assertEqual(self.provider.topup_calls, 0)

    def test_provider_says_not_paid_blocks_topup(self) -> None:
        self._seed_profile()
        expected = self._expected_total()
        attempt_id = self._seed_attempt(
            provider_payment_id="PAYT-7",
            amount_minor=expected,
            metadata={"topupIntent": self._topup_intent(transaction_id="TOPUP-REC-7")},
        )
        fib = _FakeFibProvider(status="DECLINED", amount=expected)

        with self.assertRaises(Exception):
            self._finalize(attempt_id, fib=fib)
        self.assertEqual(self.provider.topup_calls, 0)

    def test_order_payment_is_not_treated_as_a_topup(self) -> None:
        """An orderIntent payment must never be delivered as a top-up."""
        self._seed_profile()
        expected = self._expected_total()
        attempt_id = self._seed_attempt(
            provider_payment_id="PAYT-8",
            amount_minor=expected,
            metadata={"orderIntent": {"transactionId": "APP-X", "packageCode": "PKG", "providerPriceMinor": 100}},
        )
        fib = _FakeFibProvider(status="PAID", amount=expected)

        self.assertIsNone(self._finalize(attempt_id, fib=fib))
        self.assertEqual(self.provider.topup_calls, 0)


if __name__ == "__main__":
    unittest.main()
