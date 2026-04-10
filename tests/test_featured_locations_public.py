from __future__ import annotations

import os
import tempfile
import unittest
from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import create_app
from auth import create_access_token, hash_password
from config import get_settings
from supabase_store import AdminUser, AppUser, Base, FeaturedLocation, normalize_database_url, utcnow


class PublicFeaturedLocationsTest(unittest.TestCase):
    def setUp(self) -> None:
        temp_db = tempfile.NamedTemporaryFile(prefix="featured_public_", suffix=".db", delete=False)
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
        now = utcnow()
        with session_factory() as session:
            session.add(
                AdminUser(
                    id="11111111-1111-1111-1111-111111111111",
                    phone="+9647700000001",
                    name="Admin",
                    status="active",
                    role="admin",
                    can_manage_users=True,
                    can_manage_orders=True,
                    can_manage_pricing=True,
                    can_manage_content=True,
                    can_send_push=True,
                    password_hash=hash_password("StrongPass123"),
                )
            )
            session.add(
                AppUser(
                    id="22222222-2222-2222-2222-222222222222",
                    phone="+9647700000002",
                    name="Standard User",
                    status="active",
                )
            )

            session.add_all(
                [
                    FeaturedLocation(
                        code="IQ",
                        name="Iraq Old",
                        service_type="esim",
                        location_type="country",
                        sort_order=99,
                        is_popular=True,
                        enabled=True,
                        starts_at=now - timedelta(days=10),
                        created_at=now - timedelta(days=10),
                        updated_at=now - timedelta(days=10),
                    ),
                    FeaturedLocation(
                        code="IQ",
                        name="Iraq",
                        service_type="esim",
                        location_type="country",
                        sort_order=1,
                        is_popular=True,
                        enabled=True,
                        starts_at=now - timedelta(days=1),
                        created_at=now - timedelta(days=1),
                        updated_at=now - timedelta(hours=1),
                    ),
                    FeaturedLocation(
                        code="US",
                        name="United States",
                        service_type="esim",
                        location_type="country",
                        sort_order=2,
                        is_popular=True,
                        enabled=False,
                        starts_at=now - timedelta(days=1),
                    ),
                    FeaturedLocation(
                        code="FR",
                        name="France",
                        service_type="esim",
                        location_type="country",
                        sort_order=3,
                        is_popular=False,
                        enabled=True,
                        starts_at=now - timedelta(days=1),
                    ),
                    FeaturedLocation(
                        code="DE",
                        name="Germany",
                        service_type="esim",
                        location_type="country",
                        sort_order=4,
                        is_popular=True,
                        enabled=True,
                        starts_at=now + timedelta(days=1),
                    ),
                    FeaturedLocation(
                        code="TR",
                        name="Turkey",
                        service_type="esim",
                        location_type="country",
                        sort_order=5,
                        is_popular=True,
                        enabled=True,
                        starts_at=now - timedelta(days=3),
                        ends_at=now - timedelta(hours=1),
                    ),
                    FeaturedLocation(
                        code="AE",
                        name="UAE Flight",
                        service_type="flight",
                        location_type="country",
                        sort_order=1,
                        is_popular=True,
                        enabled=True,
                        starts_at=now - timedelta(days=1),
                    ),
                ]
            )
            session.commit()
        engine.dispose()

    def tearDown(self) -> None:
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
        get_settings.cache_clear()

    def _user_token(self) -> str:
        return create_access_token(
            subject_id="22222222-2222-2222-2222-222222222222",
            phone="+9647700000002",
            subject_type="user",
            secret_key="test-auth-secret",
            ttl_seconds=3600,
        )

    def test_public_featured_locations_guest_access(self) -> None:
        with TestClient(create_app()) as client:
            response = client.get("/api/v1/featured-locations/public?serviceType=esim")
            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertTrue(payload.get("success"))
            locations = payload["data"]["locations"]
            self.assertEqual(len(locations), 1)
            self.assertEqual(locations[0]["code"], "IQ")
            self.assertEqual(locations[0]["name"], "Iraq")
            self.assertEqual(locations[0]["serviceType"], "esim")
            self.assertEqual(locations[0]["locationType"], "country")
            self.assertTrue(locations[0]["isPopular"])
            self.assertTrue(locations[0]["enabled"])
            self.assertEqual(locations[0]["sortOrder"], 1)
            self.assertIn("updatedAt", locations[0])

    def test_featured_locations_alias_access_for_signed_user(self) -> None:
        with TestClient(create_app()) as client:
            response = client.get(
                "/api/v1/esim-access/featured-locations?serviceType=flight",
                headers={"Authorization": f"Bearer {self._user_token()}"},
            )
            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertTrue(payload.get("success"))
            locations = payload["data"]["locations"]
            self.assertEqual(len(locations), 1)
            self.assertEqual(locations[0]["code"], "AE")
            self.assertEqual(locations[0]["serviceType"], "flight")

    def test_admin_featured_location_write_still_protected(self) -> None:
        with TestClient(create_app()) as client:
            response = client.post(
                "/api/v1/admin/featured-locations",
                json={
                    "code": "JO",
                    "name": "Jordan",
                    "serviceType": "esim",
                    "locationType": "country",
                    "isPopular": True,
                    "enabled": True,
                    "sortOrder": 1,
                },
            )
            self.assertEqual(response.status_code, 401)

    def test_public_featured_locations_read_after_admin_write_is_fresh(self) -> None:
        admin_token = create_access_token(
            subject_id="11111111-1111-1111-1111-111111111111",
            phone="+9647700000001",
            subject_type="admin",
            secret_key="test-auth-secret",
            ttl_seconds=3600,
        )
        with TestClient(create_app()) as client:
            write_response = client.post(
                "/api/v1/admin/featured-locations",
                headers={"Authorization": f"Bearer {admin_token}"},
                json={
                    "code": "IQ",
                    "name": "Iraq Fresh",
                    "serviceType": "esim",
                    "locationType": "country",
                    "isPopular": True,
                    "enabled": True,
                    "sortOrder": 1,
                },
            )
            self.assertEqual(write_response.status_code, 200)

            read_response = client.get("/api/v1/featured-locations/public?serviceType=esim")
            self.assertEqual(read_response.status_code, 200)
            locations = read_response.json().get("data", {}).get("locations", [])
            self.assertEqual(len(locations), 1)
            self.assertEqual(locations[0]["code"], "IQ")
            self.assertEqual(locations[0]["name"], "Iraq Fresh")


if __name__ == "__main__":
    unittest.main()
