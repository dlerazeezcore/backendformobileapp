"""BE-3 regression tests for managed top-up payment verification.

The managed top-up endpoint spends real provider credit, so — exactly like the
managed-order endpoint — it MUST verify payment server-side BEFORE calling the
provider: loyalty is comp-gated on the account, FIB is re-verified against the
provider at the server-recomputed IQD price, and a paid FIB payment may fund
exactly ONE top-up (claimed before the spend). Admin top-ups remain a support
action that bypasses payment.
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
    AdminUser,
    AppUser,
    Base,
    CustomerOrder,
    ESimProfile,
    ExchangeRate,
    OrderItem,
    PaymentAttempt,
    SupabaseStore,
    normalize_database_url,
)

PACKAGE_CODE = "PKG-TOPUP-1"
PROVIDER_PRICE_MINOR = 100000


class _RecordingTopUpProvider:
    """Records top-up spends and serves the provider top-up catalog the endpoint
    uses to recompute the authoritative price server-side."""

    def __init__(self) -> None:
        self.topup_calls = 0

    async def top_up(self, request):
        self.topup_calls += 1
        txn = request.transaction_id
        return type(
            "ProviderResponse",
            (),
            {
                "success": True,
                "model_dump": staticmethod(
                    lambda **_: {"success": True, "errorCode": "0", "obj": {"transactionId": txn}}
                ),
            },
        )()

    async def get_packages(self, request):
        code = request.package_code or PACKAGE_CODE
        return type(
            "PackagesResponse",
            (),
            {
                "model_dump": staticmethod(
                    lambda **_: {
                        "success": True,
                        "obj": {
                            "packageList": [
                                {"packageCode": code, "price": PROVIDER_PRICE_MINOR, "location": "US"}
                            ]
                        },
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


class ManagedTopUpPaymentVerificationTest(unittest.TestCase):
    def setUp(self) -> None:
        temp_db = tempfile.NamedTemporaryFile(prefix="managed_topup_pay_", suffix=".db", delete=False)
        temp_db.close()
        self.db_path = temp_db.name
        self.user_id = str(uuid.uuid4())
        self.user_phone = "+9647701240001"
        self.vip_id = str(uuid.uuid4())
        self.vip_phone = "+9647701240002"
        self.admin_id = str(uuid.uuid4())
        self.admin_phone = "+9647701240003"
        self.iccid = "8986000000000000100"
        self.vip_iccid = "8986000000000000200"
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
                AppUser(
                    id=self.vip_id,
                    phone=self.vip_phone,
                    name="VIP",
                    status="active",
                    is_loyalty=True,
                )
            )
            session.add(
                AdminUser(
                    id=self.admin_id,
                    phone=self.admin_phone,
                    name="Support Admin",
                    status="active",
                    role="admin",
                )
            )
            session.add(ExchangeRate(base_currency="USD", quote_currency="IQD", rate=1450.0, active=True))
            store = SupabaseStore(session)
            for owner_id, iccid, tran in (
                (self.user_id, self.iccid, "TRAN-TOPUP-1"),
                (self.vip_id, self.vip_iccid, "TRAN-TOPUP-2"),
            ):
                order = CustomerOrder(order_number=store.build_order_number())
                session.add(order)
                item = OrderItem(service_type="esim", provider_transaction_id=f"SEED-{tran}")
                item.customer_order = order
                session.add(item)
                session.flush()
                session.add(
                    ESimProfile(
                        order_item_id=item.id,
                        user_id=owner_id,
                        iccid=iccid,
                        esim_tran_no=tran,
                        app_status="ACTIVE",
                    )
                )
            session.commit()

        app = FastAPI()
        self.esim_provider = _RecordingTopUpProvider()

        def _get_db() -> Generator[Session, None, None]:
            session = self.session_factory()
            try:
                yield session
            finally:
                session.close()

        def _get_provider() -> _RecordingTopUpProvider:
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

    def _auth_header(self, subject_id: str, phone: str, subject_type: str = "user") -> dict[str, str]:
        token = create_access_token(
            subject_id=subject_id,
            phone=phone,
            subject_type=subject_type,
            secret_key="test-auth-secret",
            ttl_seconds=3600,
        )
        return {"Authorization": f"Bearer {token}"}

    def _expected_total(self) -> int:
        with self.session_factory() as session:
            quote = SupabaseStore(session).quote_esim_sale_prices(
                [
                    {
                        "packageCode": PACKAGE_CODE,
                        "countryCode": "US",
                        "providerPriceMinor": PROVIDER_PRICE_MINOR,
                    }
                ],
                currency_code="IQD",
            )
        return quote[PACKAGE_CODE]

    def _seed_fib_attempt(self, *, provider_payment_id: str, amount_minor: int, user_id: str) -> str:
        with self.session_factory() as session:
            attempt = SupabaseStore(session).create_payment_attempt(
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
            return attempt.id

    def _topup_payload(
        self,
        *,
        transaction_id: str,
        iccid: str | None = None,
        provider_payment_id: str | None = None,
        method: str | None = "fib",
    ) -> dict:
        body: dict = {
            "providerRequest": {
                "iccid": iccid or self.iccid,
                "packageCode": PACKAGE_CODE,
                "transactionId": transaction_id,
            },
            "platformCode": "mobile_app",
            "syncAfterTopup": False,
        }
        if method is not None:
            body["paymentMethod"] = method
        if provider_payment_id is not None:
            body["paymentProviderPaymentId"] = provider_payment_id
        return body

    def _attempt(self, provider_payment_id: str) -> PaymentAttempt:
        with self.session_factory() as session:
            row = session.scalar(
                select(PaymentAttempt).where(PaymentAttempt.provider_payment_id == provider_payment_id)
            )
            assert row is not None
            session.expunge(row)
            return row

    # -- tests -----------------------------------------------------------------

    def test_missing_payment_reference_blocks_topup(self) -> None:
        for method in ("fib", None):
            resp = self.client.post(
                "/api/v1/esim-access/topups/managed",
                headers=self._auth_header(self.user_id, self.user_phone),
                json=self._topup_payload(transaction_id="TOPUP-NOPAY-1", method=method),
            )
            self.assertEqual(resp.status_code, 402, resp.text)
        self.assertEqual(self.esim_provider.topup_calls, 0)

    def test_loyalty_topup_requires_loyalty_account(self) -> None:
        resp = self.client.post(
            "/api/v1/esim-access/topups/managed",
            headers=self._auth_header(self.user_id, self.user_phone),
            json=self._topup_payload(transaction_id="TOPUP-LOY-1", method="loyalty"),
        )
        self.assertEqual(resp.status_code, 403, resp.text)
        self.assertEqual(self.esim_provider.topup_calls, 0)

    def test_loyalty_account_tops_up_without_fib(self) -> None:
        resp = self.client.post(
            "/api/v1/esim-access/topups/managed",
            headers=self._auth_header(self.vip_id, self.vip_phone),
            json=self._topup_payload(
                transaction_id="TOPUP-LOY-2", iccid=self.vip_iccid, method="loyalty"
            ),
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(self.esim_provider.topup_calls, 1)

    def test_verified_fib_payment_tops_up_and_claims_single_use(self) -> None:
        expected = self._expected_total()
        self.assertGreater(expected, 0)
        self._seed_fib_attempt(provider_payment_id="PAY-TU-1", amount_minor=expected, user_id=self.user_id)
        self.app.state.fib_payment_api = _FakeFibProvider(status="PAID", amount=expected)

        resp = self.client.post(
            "/api/v1/esim-access/topups/managed",
            headers=self._auth_header(self.user_id, self.user_phone),
            json=self._topup_payload(transaction_id="TOPUP-OK-1", provider_payment_id="PAY-TU-1"),
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(self.esim_provider.topup_calls, 1)
        claim = (self._attempt("PAY-TU-1").metadata_payload or {}).get("topupClaim")
        assert claim is not None
        self.assertEqual(claim["transactionId"], "TOPUP-OK-1")

        # Reusing the same payment for a DIFFERENT top-up must be rejected
        # without a second provider spend; the same transaction id stays
        # idempotent-retryable.
        resp2 = self.client.post(
            "/api/v1/esim-access/topups/managed",
            headers=self._auth_header(self.user_id, self.user_phone),
            json=self._topup_payload(transaction_id="TOPUP-REPLAY-1", provider_payment_id="PAY-TU-1"),
        )
        self.assertEqual(resp2.status_code, 409, resp2.text)
        self.assertEqual(self.esim_provider.topup_calls, 1)

    def test_underpaid_fib_payment_rejected(self) -> None:
        expected = self._expected_total()
        self._seed_fib_attempt(provider_payment_id="PAY-TU-2", amount_minor=expected, user_id=self.user_id)
        self.app.state.fib_payment_api = _FakeFibProvider(status="PAID", amount=max(expected - 500, 1))

        resp = self.client.post(
            "/api/v1/esim-access/topups/managed",
            headers=self._auth_header(self.user_id, self.user_phone),
            json=self._topup_payload(transaction_id="TOPUP-UNDER-1", provider_payment_id="PAY-TU-2"),
        )
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertEqual(self.esim_provider.topup_calls, 0)

    def test_foreign_payment_rejected(self) -> None:
        expected = self._expected_total()
        self._seed_fib_attempt(provider_payment_id="PAY-TU-3", amount_minor=expected, user_id=self.vip_id)
        self.app.state.fib_payment_api = _FakeFibProvider(status="PAID", amount=expected)

        resp = self.client.post(
            "/api/v1/esim-access/topups/managed",
            headers=self._auth_header(self.user_id, self.user_phone),
            json=self._topup_payload(transaction_id="TOPUP-FOREIGN-1", provider_payment_id="PAY-TU-3"),
        )
        self.assertEqual(resp.status_code, 403, resp.text)
        self.assertEqual(self.esim_provider.topup_calls, 0)

    def test_admin_topup_bypasses_payment(self) -> None:
        resp = self.client.post(
            "/api/v1/esim-access/topups/managed",
            headers=self._auth_header(self.admin_id, self.admin_phone, subject_type="admin"),
            json=self._topup_payload(transaction_id="TOPUP-ADMIN-1", method=None),
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(self.esim_provider.topup_calls, 1)


if __name__ == "__main__":
    unittest.main()
