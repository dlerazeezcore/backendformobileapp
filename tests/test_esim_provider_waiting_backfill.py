from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
import uuid

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from supabase_store import AppUser, Base, CustomerOrder, ESimProfile, OrderItem, normalize_database_url, utcnow


class ProviderWaitingBackfillTest(unittest.TestCase):
    def setUp(self) -> None:
        temp_db = tempfile.NamedTemporaryFile(prefix="provider_waiting_backfill_", suffix=".db", delete=False)
        temp_db.close()
        self.db_path = temp_db.name
        self.engine = create_engine(
            normalize_database_url(f"sqlite:///{self.db_path}"),
            connect_args={"check_same_thread": False},
            future=True,
        )
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, autoflush=False, autocommit=False, future=True)

    def tearDown(self) -> None:
        self.engine.dispose()
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def _run_upgrade(self) -> None:
        migration_path = (
            Path(__file__).resolve().parents[1]
            / "alembic"
            / "versions"
            / "0042_provider_waiting_lifecycle_backfill.py"
        )
        spec = importlib.util.spec_from_file_location("provider_waiting_backfill", migration_path)
        assert spec is not None and spec.loader is not None
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        with self.engine.begin() as connection:
            context = MigrationContext.configure(connection)
            operations = Operations(context)
            original_op = migration.op
            migration.op = operations
            try:
                migration.upgrade()
            finally:
                migration.op = original_op

    def test_backfill_demotes_uninstalled_active_and_creates_orphan_placeholder(self) -> None:
        user_id = str(uuid.uuid4())
        now = utcnow()
        with self.Session() as session:
            session.add(AppUser(id=user_id, phone="+9647700000042", name="Backfill User", status="active"))
            session.flush()

            active_order = CustomerOrder(user_id=user_id, order_number="ORD-BACKFILL-A", order_status="ACTIVE", booked_at=now)
            orphan_order = CustomerOrder(user_id=user_id, order_number="ORD-BACKFILL-B", order_status="BOOKED", booked_at=now)
            session.add_all([active_order, orphan_order])
            session.flush()

            active_item = OrderItem(
                customer_order_id=active_order.id,
                service_type="esim",
                provider_order_no="ORD-BACKFILL-A",
                item_status="ACTIVE",
                provider_status="ENABLED",
                booked_at=now,
            )
            orphan_item = OrderItem(
                customer_order_id=orphan_order.id,
                service_type="esim",
                provider_order_no="ORD-BACKFILL-B",
                item_status="BOOKED",
                provider_status="SUCCESS",
                booked_at=now,
            )
            session.add_all([active_item, orphan_item])
            session.flush()
            active_order_id = active_order.id
            active_item_id = active_item.id
            orphan_item_id = orphan_item.id
            session.add(
                ESimProfile(
                    order_item_id=active_item.id,
                    user_id=user_id,
                    app_status="ACTIVE",
                    provider_status="ENABLED",
                    installed=False,
                    activated_at=now,
                    validity_days=7,
                )
            )
            session.commit()

        self._run_upgrade()

        with self.Session() as session:
            active_profile = session.scalar(select(ESimProfile).where(ESimProfile.order_item_id == active_item_id))
            orphan_profile = session.scalar(select(ESimProfile).where(ESimProfile.order_item_id == orphan_item_id))
            active_item_row = session.get(OrderItem, active_item_id)
            active_order_row = session.get(CustomerOrder, active_order_id)

            self.assertIsNotNone(active_profile)
            self.assertEqual(active_profile.app_status, "PROVIDER_WAITING")
            self.assertFalse(active_profile.installed)
            self.assertEqual(active_item_row.item_status, "PROVIDER_WAITING")
            self.assertEqual(active_order_row.order_status, "PROVIDER_WAITING")

            self.assertIsNotNone(orphan_profile)
            self.assertEqual(orphan_profile.user_id, user_id)
            self.assertEqual(orphan_profile.app_status, "BOOKED")
            self.assertFalse(orphan_profile.installed)


if __name__ == "__main__":
    unittest.main()
