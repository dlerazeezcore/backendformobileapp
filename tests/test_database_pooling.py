from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from sqlalchemy.pool import NullPool
from sqlalchemy.pool import QueuePool

from supabase_store import create_database, normalize_database_url


class DatabasePoolingTest(unittest.TestCase):
    def tearDown(self) -> None:
        for name in (
            "DATABASE_POOL_SIZE",
            "DATABASE_MAX_OVERFLOW",
            "DATABASE_POOL_TIMEOUT_SECONDS",
            "DATABASE_POOL_RECYCLE_SECONDS",
            "DATABASE_CONNECT_TIMEOUT_SECONDS",
            "DATABASE_APPLICATION_NAME",
            "DATABASE_POOL_CLASS",
            "SUPABASE_FORCE_TRANSACTION_POOLER",
        ):
            os.environ.pop(name, None)

    def test_postgres_pool_defaults_are_conservative_for_supabase_pooler(self) -> None:
        session_factory = create_database("postgresql://user:password@example.com:5432/postgres")
        engine = session_factory.kw["bind"]
        try:
            self.assertIsInstance(engine.pool, QueuePool)
            self.assertEqual(engine.pool.size(), 2)
            self.assertEqual(engine.pool._max_overflow, 1)
            self.assertEqual(engine.pool._timeout, 15)
            self.assertEqual(engine.pool._recycle, 300)
            self.assertTrue(engine.pool._pre_ping)
        finally:
            engine.dispose()

    def test_supabase_transaction_pooler_url_uses_small_queue_pool_in_auto_mode(self) -> None:
        session_factory = create_database(
            "postgresql://user:password@aws-1-ap-southeast-2.pooler.supabase.com:6543/postgres"
        )
        engine = session_factory.kw["bind"]
        try:
            self.assertIsInstance(engine.pool, QueuePool)
            self.assertEqual(engine.pool.size(), 2)
            self.assertEqual(engine.pool._max_overflow, 0)
            self.assertEqual(engine.pool._timeout, 3)
        finally:
            engine.dispose()

    def test_supabase_session_pooler_url_is_normalized_and_uses_small_queue_pool_in_auto_mode(self) -> None:
        session_factory = create_database(
            "postgresql://user:password@aws-1-ap-southeast-2.pooler.supabase.com:5432/postgres"
        )
        engine = session_factory.kw["bind"]
        try:
            self.assertIsInstance(engine.pool, QueuePool)
            self.assertEqual(engine.pool.size(), 2)
            self.assertEqual(engine.pool._max_overflow, 0)
            self.assertEqual(engine.pool._timeout, 3)
            self.assertEqual(engine.url.port, 6543)
        finally:
            engine.dispose()

    def test_supabase_pooler_url_without_explicit_port_is_normalized_to_transaction_mode(self) -> None:
        session_factory = create_database(
            "postgresql://user:password@aws-1-ap-southeast-2.pooler.supabase.com/postgres"
        )
        engine = session_factory.kw["bind"]
        try:
            self.assertIsInstance(engine.pool, QueuePool)
            self.assertEqual(engine.pool.size(), 2)
            self.assertEqual(engine.pool._max_overflow, 0)
            self.assertEqual(engine.pool._timeout, 3)
            self.assertEqual(engine.url.port, 6543)
        finally:
            engine.dispose()

    def test_normalize_database_url_moves_supabase_pooler_to_transaction_port(self) -> None:
        normalized = normalize_database_url(
            "postgresql://user:password@aws-1-ap-southeast-2.pooler.supabase.com:5432/postgres"
        )
        self.assertTrue(normalized.startswith("postgresql+psycopg://"))
        self.assertIn(":6543/", normalized)

    def test_normalize_database_url_moves_supabase_pooler_without_port_to_transaction_port(self) -> None:
        normalized = normalize_database_url(
            "postgresql://user:password@aws-1-ap-southeast-2.pooler.supabase.com/postgres"
        )
        self.assertTrue(normalized.startswith("postgresql+psycopg://"))
        self.assertIn(":6543/", normalized)

    def test_normalize_database_url_can_keep_session_pooler_when_override_disabled(self) -> None:
        os.environ["SUPABASE_FORCE_TRANSACTION_POOLER"] = "false"
        normalized = normalize_database_url(
            "postgresql://user:password@aws-1-ap-southeast-2.pooler.supabase.com:5432/postgres"
        )
        self.assertTrue(normalized.startswith("postgresql+psycopg://"))
        self.assertIn(":5432/", normalized)

    def test_supabase_session_pooler_uses_small_queue_pool_when_transaction_override_disabled(self) -> None:
        os.environ["SUPABASE_FORCE_TRANSACTION_POOLER"] = "false"
        session_factory = create_database(
            "postgresql://user:password@aws-1-ap-southeast-2.pooler.supabase.com:5432/postgres"
        )
        engine = session_factory.kw["bind"]
        try:
            self.assertIsInstance(engine.pool, QueuePool)
            self.assertEqual(engine.pool.size(), 1)
            self.assertEqual(engine.pool._max_overflow, 0)
        finally:
            engine.dispose()

    def test_pool_class_can_force_null_pool(self) -> None:
        os.environ["DATABASE_POOL_CLASS"] = "null"
        session_factory = create_database("postgresql://user:password@example.com:5432/postgres")
        engine = session_factory.kw["bind"]
        try:
            self.assertIsInstance(engine.pool, NullPool)
        finally:
            engine.dispose()

    def test_supabase_pooler_connections_disable_prepared_statements(self) -> None:
        with patch("supabase_store.create_engine") as mocked_create_engine:
            mocked_create_engine.return_value = object()
            create_database("postgresql://user:password@aws-1-ap-southeast-2.pooler.supabase.com:6543/postgres")
            connect_args = mocked_create_engine.call_args.kwargs["connect_args"]
            self.assertIn("prepare_threshold", connect_args)
            self.assertIsNone(connect_args["prepare_threshold"])
            self.assertEqual(connect_args["connect_timeout"], 3)
            self.assertEqual(connect_args["application_name"], "tulip_mobile_backend")

    def test_database_connect_timeout_can_be_overridden_by_environment(self) -> None:
        os.environ["DATABASE_CONNECT_TIMEOUT_SECONDS"] = "7"
        os.environ["DATABASE_APPLICATION_NAME"] = "tulip_test_app"

        with patch("supabase_store.create_engine") as mocked_create_engine:
            mocked_create_engine.return_value = object()
            create_database("postgresql://user:password@example.com:5432/postgres")
            connect_args = mocked_create_engine.call_args.kwargs["connect_args"]
            self.assertEqual(connect_args["connect_timeout"], 7)
            self.assertEqual(connect_args["application_name"], "tulip_test_app")

    def test_postgres_pool_limits_can_be_overridden_by_environment(self) -> None:
        os.environ["DATABASE_POOL_SIZE"] = "2"
        os.environ["DATABASE_MAX_OVERFLOW"] = "1"
        os.environ["DATABASE_POOL_TIMEOUT_SECONDS"] = "5"
        os.environ["DATABASE_POOL_RECYCLE_SECONDS"] = "60"

        session_factory = create_database("postgresql://user:password@example.com:5432/postgres")
        engine = session_factory.kw["bind"]
        try:
            self.assertIsInstance(engine.pool, QueuePool)
            self.assertEqual(engine.pool.size(), 2)
            self.assertEqual(engine.pool._max_overflow, 1)
            self.assertEqual(engine.pool._timeout, 5)
            self.assertEqual(engine.pool._recycle, 60)
            self.assertTrue(engine.pool._pre_ping)
        finally:
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
