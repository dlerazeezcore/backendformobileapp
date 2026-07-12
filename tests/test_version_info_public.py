"""AUDIT-8: the public, unauthenticated GET /api/v1/app/version-info must be
strictly read-only. When migration 0040's seed row is absent it answers with the
in-code defaults and must NOT INSERT the singleton (which raced concurrent
boot-time callers into an id=1 PK collision). Row creation is reserved for the
admin PUT path."""
from __future__ import annotations

import os
import tempfile
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import create_app
from app_meta import DEFAULT_APP_STORE_URL, DEFAULT_PLAY_STORE_URL
from config import get_settings
from supabase_store import AppReleaseInfo, Base, normalize_database_url


class PublicVersionInfoTest(unittest.TestCase):
    def setUp(self) -> None:
        temp_db = tempfile.NamedTemporaryFile(prefix="version_info_public_", suffix=".db", delete=False)
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
        engine.dispose()

    def tearDown(self) -> None:
        try:
            if os.path.exists(self.db_path):
                os.remove(self.db_path)
        except PermissionError:
            pass  # Windows can still hold the sqlite file briefly; harmless temp file.
        get_settings.cache_clear()

    def _rows(self) -> list[AppReleaseInfo]:
        engine = create_engine(
            normalize_database_url(os.environ["DATABASE_URL"]),
            connect_args={"check_same_thread": False},
            future=True,
        )
        session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
        with session_factory() as session:
            rows = list(session.scalars(select(AppReleaseInfo)).all())
        engine.dispose()
        return rows

    def _seed_row(self) -> None:
        engine = create_engine(
            normalize_database_url(os.environ["DATABASE_URL"]),
            connect_args={"check_same_thread": False},
            future=True,
        )
        session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
        with session_factory() as session:
            session.add(
                AppReleaseInfo(
                    id=1,
                    latest_version="2.3.4",
                    min_supported_version="2.0.0",
                    app_store_url="https://apps.apple.com/custom",
                    play_store_url="https://play.google.com/custom",
                    release_notes_en="Bug fixes",
                )
            )
            session.commit()
        engine.dispose()

    def test_missing_row_returns_defaults_without_writing(self) -> None:
        self.assertEqual(self._rows(), [])  # migration seed row deliberately absent
        with TestClient(create_app()) as client:
            res = client.get("/api/v1/app/version-info")
            self.assertEqual(res.status_code, 200, res.text)
            body = res.json()
            self.assertEqual(body["latestVersion"], "1.0.0")
            self.assertEqual(body["minSupportedVersion"], "1.0.0")
            self.assertEqual(body["appStoreUrl"], DEFAULT_APP_STORE_URL)
            self.assertEqual(body["playStoreUrl"], DEFAULT_PLAY_STORE_URL)
            self.assertEqual(body["releaseNotes"], {"en": None, "ar": None, "ku": None})
            self.assertIsNone(body["updatedAt"])  # nothing has ever been published

            # A second call must behave identically (no first-call side effect).
            self.assertEqual(client.get("/api/v1/app/version-info").json(), body)
        # AUDIT-8: the public GET must not have created the singleton.
        self.assertEqual(self._rows(), [])

    def test_existing_row_is_served_unchanged(self) -> None:
        self._seed_row()
        with TestClient(create_app()) as client:
            res = client.get("/api/v1/app/version-info")
            self.assertEqual(res.status_code, 200, res.text)
            body = res.json()
            self.assertEqual(body["latestVersion"], "2.3.4")
            self.assertEqual(body["minSupportedVersion"], "2.0.0")
            self.assertEqual(body["appStoreUrl"], "https://apps.apple.com/custom")
            self.assertEqual(body["playStoreUrl"], "https://play.google.com/custom")
            self.assertEqual(body["releaseNotes"], {"en": "Bug fixes", "ar": None, "ku": None})
            self.assertIsNotNone(body["updatedAt"])
        self.assertEqual(len(self._rows()), 1)


if __name__ == "__main__":
    unittest.main()
