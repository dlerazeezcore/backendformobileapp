from __future__ import annotations

import os
import tempfile
import unittest
from datetime import timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from supabase_store import (
    Base,
    ExchangeRate,
    PricingRule,
    SupabaseStore,
    normalize_database_url,
    utcnow,
)


class PricingAbsoluteTest(unittest.TestCase):
    """The 'absolute' pricing type sets an EXACT sale price (no cost/markup, no
    250-rounding). Percent/fixed behaviour must stay unchanged."""

    def setUp(self) -> None:
        tmp = tempfile.NamedTemporaryFile(prefix="pricing_abs_", suffix=".db", delete=False)
        tmp.close()
        self.db_path = tmp.name
        self.engine = create_engine(
            normalize_database_url(f"sqlite:///{self.db_path}"),
            connect_args={"check_same_thread": False},
            future=True,
        )
        Base.metadata.create_all(self.engine)
        self.session_factory = sessionmaker(bind=self.engine, autoflush=False, autocommit=False, future=True)
        with self.session_factory() as s:
            s.add(ExchangeRate(
                base_currency="USD", quote_currency="IQD", rate=1550.0,
                effective_at=utcnow() - timedelta(days=1), active=True,
            ))
            # $10.00 provider cost → 15,500 IQD converted (providerPriceMinor = USD*10000)
            s.add(PricingRule(service_type="esim", rule_scope="package", package_code="ABSPKG",
                              adjustment_type="absolute", adjustment_value=4990, applies_to="provider_cost",
                              active=True, priority=100))
            s.add(PricingRule(service_type="esim", rule_scope="package", package_code="PCTPKG",
                              adjustment_type="percent", adjustment_value=100, applies_to="provider_cost",
                              active=True, priority=100))
            s.add(PricingRule(service_type="esim", rule_scope="package", package_code="FIXPKG",
                              adjustment_type="fixed", adjustment_value=500, applies_to="provider_cost",
                              active=True, priority=100))
            s.commit()

    def tearDown(self) -> None:
        self.engine.dispose()
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def test_quote_prices(self) -> None:
        with self.session_factory() as s:
            store = SupabaseStore(s)
            items = [
                {"packageCode": "ABSPKG", "countryCode": None, "providerPriceMinor": 100000},
                {"packageCode": "PCTPKG", "countryCode": None, "providerPriceMinor": 100000},
                {"packageCode": "FIXPKG", "countryCode": None, "providerPriceMinor": 100000},
            ]
            out = store.quote_esim_sale_prices(items, currency_code="IQD")

        # absolute → EXACT value, NOT rounded to 250 (4990 stays 4990, not 5000)
        self.assertEqual(out["ABSPKG"], 4990)
        # percent 100% on 15,500 IQD → 31,000 (unchanged)
        self.assertEqual(out["PCTPKG"], 31000)
        # fixed +500 on 15,500 IQD → 16,000 (unchanged)
        self.assertEqual(out["FIXPKG"], 16000)


if __name__ == "__main__":
    unittest.main()
