from __future__ import annotations

import os
import tempfile
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import create_app
from config import get_settings
from supabase_store import Base, ExchangeRate, PricingRule, normalize_database_url, utcnow


class PublicCurrenciesTest(unittest.TestCase):
    def setUp(self) -> None:
        temp_db = tempfile.NamedTemporaryFile(prefix="currencies_public_", suffix=".db", delete=False)
        temp_db.close()
        self.db_path = temp_db.name
        os.environ["ESIM_ACCESS_ACCESS_CODE"] = "test-code"
        os.environ["ESIM_ACCESS_SECRET_KEY"] = "test-secret"
        os.environ["DATABASE_URL"] = f"sqlite:///{self.db_path}"
        os.environ["AUTH_SECRET_KEY"] = "test-auth-secret"
        get_settings.cache_clear()

        engine = create_engine(
            normalize_database_url(os.environ["DATABASE_URL"]),
            connect_args={"check_same_thread": False},
            future=True,
        )
        Base.metadata.create_all(engine)
        session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
        with session_factory() as session:
            # Universal FX rows: rate is IQD per 1 unit; markup lives separately.
            session.add(
                ExchangeRate(
                    base_currency="USD",
                    quote_currency="IQD",
                    rate=1550.0,
                    source="tulip-admin",
                    active=True,
                    custom_fields={"enableIQD": True, "markupPercent": "100"},
                )
            )
            session.add(
                ExchangeRate(
                    base_currency="EUR",
                    quote_currency="IQD",
                    rate=1700.0,
                    source="tulip-admin",
                    active=True,
                    custom_fields={"symbol": "€", "decimals": 2, "enabled": True},
                )
            )
            session.commit()
        engine.dispose()

    def tearDown(self) -> None:
        try:
            if os.path.exists(self.db_path):
                os.remove(self.db_path)
        except PermissionError:
            pass  # Windows can still hold the sqlite file briefly; harmless temp file.
        get_settings.cache_clear()

    def _add_rows(self, *rows: object) -> None:
        engine = create_engine(
            normalize_database_url(os.environ["DATABASE_URL"]),
            connect_args={"check_same_thread": False},
            future=True,
        )
        session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
        with session_factory() as session:
            session.add_all(list(rows))
            session.commit()
        engine.dispose()

    def test_currencies_lists_iqd_usd_eur_with_pure_rates(self) -> None:
        with TestClient(create_app()) as client:
            res = client.get("/api/v1/currencies")
            self.assertEqual(res.status_code, 200)
            self.assertIn("max-age=300", res.headers.get("cache-control", ""))
            body = res.json()
            self.assertEqual(body["baseCurrency"], "IQD")
            by_code = {c["code"]: c for c in body["currencies"]}
            self.assertEqual(set(by_code), {"IQD", "USD", "EUR"})
            self.assertEqual(by_code["IQD"]["rate"], 1.0)
            self.assertEqual(by_code["IQD"]["decimals"], 0)
            self.assertEqual(by_code["USD"]["rate"], 1550.0)
            self.assertEqual(by_code["EUR"]["rate"], 1700.0)
            self.assertEqual(by_code["EUR"]["symbol"], "€")
            self.assertEqual(by_code["EUR"]["decimals"], 2)
            self.assertTrue(by_code["EUR"]["enabled"])
            # No pricing rule yet → markup falls back to the USD FX row's markupPercent.
            self.assertEqual(body["esimPricing"]["markupPercent"], 100.0)

    def test_global_esim_pricing_rule_overrides_fx_markup(self) -> None:
        self._add_rows(
            PricingRule(
                service_type="esim",
                rule_scope="global",
                adjustment_type="percent",
                adjustment_value=80.0,
                applies_to="provider_cost",
                priority=100,
                active=True,
            )
        )
        with TestClient(create_app()) as client:
            body = client.get("/api/v1/currencies").json()
            # The service-scoped rule wins over the legacy FX-row markup.
            self.assertEqual(body["esimPricing"]["markupPercent"], 80.0)


if __name__ == "__main__":
    unittest.main()
