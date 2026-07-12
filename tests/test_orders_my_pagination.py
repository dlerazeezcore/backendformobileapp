from __future__ import annotations

from datetime import timedelta
import os
import tempfile
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import create_app
from auth import create_access_token
from config import get_settings
from supabase_store import (
    AppUser,
    Base,
    CustomerOrder,
    OrderItem,
    normalize_database_url,
    utcnow,
)

USER_ONE = "22222222-2222-2222-2222-222222222222"
USER_TWO = "33333333-3333-3333-3333-333333333333"


class OrdersMyPaginationTest(unittest.TestCase):
    """GET /api/v1/esim-access/orders/my pages in SQL: correct total + slice."""

    def setUp(self) -> None:
        temp_db = tempfile.NamedTemporaryFile(prefix="orders_my_pagination_", suffix=".db", delete=False)
        temp_db.close()
        self.db_path = temp_db.name
        os.environ["ESIM_ACCESS_ACCESS_CODE"] = "test-code"
        os.environ["ESIM_ACCESS_SECRET_KEY"] = "test-secret"
        os.environ["DATABASE_URL"] = f"sqlite:///{self.db_path}"
        os.environ["AUTH_SECRET_KEY"] = "test-auth-secret"
        get_settings.cache_clear()

        self.engine = create_engine(
            normalize_database_url(os.environ["DATABASE_URL"]),
            connect_args={"check_same_thread": False},
            future=True,
        )
        Base.metadata.create_all(self.engine)
        self.session_factory = sessionmaker(bind=self.engine, autoflush=False, autocommit=False, future=True)

        base_time = utcnow() - timedelta(days=1)
        with self.session_factory() as session:
            session.add_all(
                [
                    AppUser(id=USER_ONE, phone="+9647700000002", name="User One", status="active"),
                    AppUser(id=USER_TWO, phone="+9647700000003", name="User Two", status="active"),
                ]
            )

            # Five orders for USER_ONE with strictly increasing booked_at, so the
            # endpoint's coalesce(booked_at, created_at) DESC ordering makes
            # ORD-USER1-0005 the newest. Each order carries TWO items so a
            # row-multiplying join would corrupt LIMIT/OFFSET if pagination
            # were not applied to the orders table itself.
            orders = []
            for idx in range(1, 6):
                orders.append(
                    CustomerOrder(
                        user_id=USER_ONE,
                        order_number=f"ORD-USER1-{idx:04d}",
                        order_status="BOOKED",
                        booked_at=base_time + timedelta(minutes=idx),
                    )
                )
            # One order for another user: must never leak into USER_ONE's total.
            orders.append(
                CustomerOrder(
                    user_id=USER_TWO,
                    order_number="ORD-USER2-0001",
                    order_status="BOOKED",
                    booked_at=base_time,
                )
            )
            session.add_all(orders)
            session.flush()

            items = []
            for order in orders:
                for item_no in range(2):
                    items.append(
                        OrderItem(
                            customer_order_id=order.id,
                            country_code="IQ",
                            country_name="Iraq",
                            item_status="ACTIVE",
                            service_type="esim",
                            package_code=f"{order.order_number}-PKG-{item_no}",
                        )
                    )
            session.add_all(items)
            session.commit()

    def tearDown(self) -> None:
        self.engine.dispose()
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
        get_settings.cache_clear()

    def _token(self) -> str:
        return create_access_token(
            subject_id=USER_ONE,
            phone="+9647700000002",
            subject_type="user",
            secret_key="test-auth-secret",
            ttl_seconds=3600,
        )

    def _fetch(self, client: TestClient, *, limit: int, offset: int) -> dict:
        response = client.get(
            f"/api/v1/esim-access/orders/my?limit={limit}&offset={offset}",
            headers={"Authorization": f"Bearer {self._token()}"},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload.get("success"))
        return payload["data"]

    def test_first_page_returns_newest_orders_and_full_total(self) -> None:
        with TestClient(create_app()) as client:
            data = self._fetch(client, limit=2, offset=0)
            self.assertEqual(data["total"], 5)
            self.assertEqual(data["limit"], 2)
            self.assertEqual(data["offset"], 0)
            self.assertEqual(
                [order["orderNumber"] for order in data["orders"]],
                ["ORD-USER1-0005", "ORD-USER1-0004"],
            )
            # Items survive pagination intact (2 per order, not join-multiplied).
            for order in data["orders"]:
                self.assertEqual(len(order["items"]), 2)

    def test_middle_page_returns_correct_slice(self) -> None:
        with TestClient(create_app()) as client:
            data = self._fetch(client, limit=2, offset=2)
            self.assertEqual(data["total"], 5)
            self.assertEqual(
                [order["orderNumber"] for order in data["orders"]],
                ["ORD-USER1-0003", "ORD-USER1-0002"],
            )

    def test_last_page_is_partial(self) -> None:
        with TestClient(create_app()) as client:
            data = self._fetch(client, limit=2, offset=4)
            self.assertEqual(data["total"], 5)
            self.assertEqual(
                [order["orderNumber"] for order in data["orders"]],
                ["ORD-USER1-0001"],
            )

    def test_offset_beyond_total_returns_empty_page_with_total(self) -> None:
        with TestClient(create_app()) as client:
            data = self._fetch(client, limit=2, offset=10)
            self.assertEqual(data["total"], 5)
            self.assertEqual(data["orders"], [])
            self.assertEqual(data["offset"], 10)

    def test_pages_cover_all_orders_without_overlap(self) -> None:
        with TestClient(create_app()) as client:
            seen: list[str] = []
            for offset in range(0, 6, 2):
                data = self._fetch(client, limit=2, offset=offset)
                seen.extend(order["orderNumber"] for order in data["orders"])
            self.assertEqual(
                seen,
                [
                    "ORD-USER1-0005",
                    "ORD-USER1-0004",
                    "ORD-USER1-0003",
                    "ORD-USER1-0002",
                    "ORD-USER1-0001",
                ],
            )


if __name__ == "__main__":
    unittest.main()
