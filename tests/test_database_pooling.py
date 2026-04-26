from __future__ import annotations

import os
import unittest

from sqlalchemy.pool import QueuePool

from supabase_store import create_database


class DatabasePoolingTest(unittest.TestCase):
    def tearDown(self) -> None:
        for name in (
            "DATABASE_POOL_SIZE",
            "DATABASE_MAX_OVERFLOW",
            "DATABASE_POOL_TIMEOUT_SECONDS",
            "DATABASE_POOL_RECYCLE_SECONDS",
        ):
            os.environ.pop(name, None)

    def test_postgres_pool_defaults_are_conservative_for_supabase_pooler(self) -> None:
        session_factory = create_database("postgresql://user:password@example.com:5432/postgres")
        engine = session_factory.kw["bind"]
        try:
            self.assertIsInstance(engine.pool, QueuePool)
            self.assertEqual(engine.pool.size(), 1)
            self.assertEqual(engine.pool._max_overflow, 0)
            self.assertEqual(engine.pool._timeout, 30)
            self.assertEqual(engine.pool._recycle, 300)
            self.assertTrue(engine.pool._pre_ping)
        finally:
            engine.dispose()

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
