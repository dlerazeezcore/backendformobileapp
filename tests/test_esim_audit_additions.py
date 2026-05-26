from __future__ import annotations

import os
import tempfile
import unittest
import uuid
from datetime import timedelta

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("AUTH_SECRET_KEY", "test-auth-secret")
os.environ.setdefault("ESIM_ACCESS_ACCESS_CODE", "test-code")
os.environ.setdefault("ESIM_ACCESS_SECRET_KEY", "test-secret")

from auth import _login_subject_with_password, hash_password  # noqa: E402
from config import get_settings  # noqa: E402
from esim_access_api import _serialize_profile  # noqa: E402
from supabase_store import (  # noqa: E402
    AppUser,
    Base,
    CustomerOrder,
    ESimProfile,
    ExchangeRate,
    OrderItem,
    SupabaseStore,
    utcnow,
)


class _DbTestCase(unittest.TestCase):
    def setUp(self) -> None:
        get_settings.cache_clear()
        tmp = tempfile.NamedTemporaryFile(prefix="esim_audit_", suffix=".db", delete=False)
        tmp.close()
        self.db_path = tmp.name
        self.engine = create_engine(f"sqlite:///{self.db_path}", connect_args={"check_same_thread": False}, future=True)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, autoflush=False, autocommit=False, future=True)

    def tearDown(self) -> None:
        self.engine.dispose()
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
        get_settings.cache_clear()


class SerializeInstallTargetsTest(unittest.TestCase):
    def _profile(self, *, activated_at, validity_days=7) -> ESimProfile:
        p = ESimProfile()
        p.id = 1
        p.user_id = "u1"
        p.esim_tran_no = "TRAN1"
        p.iccid = "8910000000000000001"
        p.activation_code = "LPA:1$rsp-eu.simlessly.com$ABC123DEF456"
        p.qr_code_url = "https://p.qrsim.net/abc.png"
        p.install_url = "https://p.qrsim.net/abc"
        p.app_status = "ACTIVE"
        p.provider_status = "RELEASED"
        p.installed = True
        p.activated_at = activated_at
        p.validity_days = validity_days
        p.total_data_mb = 1024
        p.used_data_mb = 200
        p.remaining_data_mb = 824
        p.custom_fields = {}
        return p

    def test_active_profile_exposes_apple_link_and_manual_entry(self) -> None:
        now = utcnow()
        data = _serialize_profile(self._profile(activated_at=now), now=now)
        self.assertEqual(data["status"], "active")
        self.assertEqual(
            data["appleInstallUrl"],
            "https://esimsetup.apple.com/esim_qrcode_provisioning?carddata=LPA:1$rsp-eu.simlessly.com$ABC123DEF456",
        )
        self.assertEqual(data["smdpAddress"], "rsp-eu.simlessly.com")
        self.assertEqual(data["matchingId"], "ABC123DEF456")
        self.assertEqual(data["manualEntry"], {"smdpAddress": "rsp-eu.simlessly.com", "activationCode": "LPA:1$rsp-eu.simlessly.com$ABC123DEF456"})
        self.assertEqual(data["qrCodeUrl"], "https://p.qrsim.net/abc.png")

    def test_expired_profile_hides_apple_link_and_manual_entry(self) -> None:
        now = utcnow()
        data = _serialize_profile(self._profile(activated_at=now - timedelta(days=30), validity_days=7), now=now)
        self.assertEqual(data["status"], "expired")
        self.assertIsNone(data["appleInstallUrl"])
        self.assertIsNone(data["manualEntry"])


class ServerAuthoritativePricingTest(_DbTestCase):
    def test_tampered_client_price_is_ignored(self) -> None:
        with self.Session() as session:
            session.add(
                ExchangeRate(
                    base_currency="USD",
                    quote_currency="IQD",
                    rate=1500.0,
                    source="tulip-admin",
                    active=True,
                    effective_at=utcnow(),
                    custom_fields={"enableIQD": True, "markupPercent": "100"},
                )
            )
            session.commit()

            store = SupabaseStore(session)
            # Provider price 23000 == $2.30 (eSIM Access quotes 1/10000 USD).
            order, item = store.save_managed_order(
                user_data={"phone": "+9647501234567", "name": "Pricing User", "email": None},
                platform_code="mobile_app",
                platform_name=None,
                order_request={"transactionId": "TX-PRICE-1", "packageInfoList": [{"packageCode": "PK1", "count": 1, "price": 23000, "periodNum": 7}]},
                provider_response={"success": True, "obj": {"orderNo": "ORD-PRICE-1", "transactionId": "TX-PRICE-1"}},
                currency_code="IQD",
                provider_currency_code="USD",
                exchange_rate=1500.0,
                sale_price_minor=1,  # tampered: far below the real price
                provider_price_minor=23000,
                country_code="US",
                country_name="United States",
                package_code="PK1",
            )
            # 2.30 USD * 1500 = 3450 IQD subtotal; +100% markup = 6900 authoritative total.
            self.assertEqual(item.provider_price_minor, 3450)
            self.assertEqual(item.markup_minor, 3450)
            self.assertEqual(item.sale_price_minor, 6900)
            self.assertEqual(order.total_minor, 6900)
            self.assertEqual(item.custom_fields.get("clientSalePriceMinor"), 1)


class EmailLoginTest(_DbTestCase):
    def test_login_by_email_and_password(self) -> None:
        user_id = str(uuid.uuid4())
        with self.Session() as session:
            session.add(
                AppUser(
                    id=user_id,
                    phone="+9647501112233",
                    name="Email User",
                    email="Person@Example.com",
                    password_hash=hash_password("supersecret1"),
                    status="active",
                )
            )
            session.commit()
        # case-insensitive email lookup + password verification
        result = _login_subject_with_password(self.Session, email="person@example.com", password="supersecret1")
        self.assertEqual(result["subjectType"], "user")
        self.assertEqual(result["id"], user_id)
        self.assertIn("accessToken", result)


class UsageBytesNormalizationTest(_DbTestCase):
    def test_usage_only_bytes_record_normalizes_to_mb(self) -> None:
        with self.Session() as session:
            order = CustomerOrder(order_number="ORD-USAGE-1", order_status="ACTIVE", booked_at=utcnow())
            session.add(order)
            session.flush()
            item = OrderItem(customer_order_id=order.id, service_type="esim", provider_order_no="ORD-USAGE-1", booked_at=utcnow())
            session.add(item)
            session.flush()
            session.add(ESimProfile(order_item_id=item.id, esim_tran_no="TRAN-USAGE-1", iccid="ICCID-USAGE-1", total_data_mb=1024))
            session.commit()

            store = SupabaseStore(session)
            # ~3 MB used reported in bytes (3145728). Heuristic would mis-read this as KB.
            store.sync_usage_records({"obj": {"esimUsageList": [{"esimTranNo": "TRAN-USAGE-1", "dataUsage": 3145728}]}})
            profile = session.scalar(select(ESimProfile).where(ESimProfile.esim_tran_no == "TRAN-USAGE-1"))
            self.assertEqual(profile.used_data_mb, 3)


class PlaceholderReconciliationTest(_DbTestCase):
    def test_sync_reuses_placeholder_no_duplicate(self) -> None:
        with self.Session() as session:
            order = CustomerOrder(order_number="ORD-DUP-1", order_status="BOOKED", booked_at=utcnow())
            session.add(order)
            session.flush()
            item = OrderItem(customer_order_id=order.id, service_type="esim", provider_order_no="ORD-DUP-1", booked_at=utcnow())
            session.add(item)
            session.flush()
            # purchase-time placeholder: no iccid / no esim_tran_no
            session.add(ESimProfile(order_item_id=item.id, app_status="BOOKED"))
            session.commit()

            store = SupabaseStore(session)
            store.sync_profiles(
                {"obj": {"esimList": [{"orderNo": "ORD-DUP-1", "iccid": "ICCID-DUP-1", "esimTranNo": "TRAN-DUP-1", "esimStatus": "GOT_RESOURCE", "ac": "LPA:1$smdp$MID"}]}}
            )
            rows = session.scalars(select(ESimProfile).where(ESimProfile.order_item_id == item.id)).all()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].iccid, "ICCID-DUP-1")


if __name__ == "__main__":
    unittest.main()
