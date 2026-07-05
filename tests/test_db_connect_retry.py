from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock

os.environ.setdefault("AUTH_SECRET_KEY", "test-auth-secret")

from sqlalchemy.exc import OperationalError as SQLAlchemyOperationalError

import dependencies


def _connect_timeout_error() -> SQLAlchemyOperationalError:
    # Mirrors the shape SQLAlchemy raises when psycopg's connect times out
    # against the Supabase pooler ("connection timeout expired").
    return SQLAlchemyOperationalError("SELECT 1", {}, Exception("connection timeout expired"))


class GetDbConnectRetryTest(unittest.TestCase):
    def setUp(self) -> None:
        # Keep the retry loop instant in tests.
        os.environ["DATABASE_CONNECT_RETRY_BACKOFF_SECONDS"] = "0"

    def tearDown(self) -> None:
        for name in (
            "DATABASE_CONNECT_RETRY_BACKOFF_SECONDS",
            "DATABASE_CONNECT_RETRY_ATTEMPTS",
        ):
            os.environ.pop(name, None)

    @staticmethod
    def _request_with_factory(factory: MagicMock) -> MagicMock:
        request = MagicMock()
        request.app.state.db_session_factory = factory
        return request

    def test_transient_connect_failure_is_retried_then_yields_healthy_session(self) -> None:
        bad = MagicMock(name="bad_session")
        bad.connection.side_effect = _connect_timeout_error()
        good = MagicMock(name="good_session")
        factory = MagicMock(side_effect=[bad, good])

        gen = dependencies.get_db(self._request_with_factory(factory))
        yielded = next(gen)

        # The blip on the first session is retried and the healthy one is served.
        self.assertIs(yielded, good)
        self.assertEqual(factory.call_count, 2)
        bad.close.assert_called()  # the failed pre-connect session is not leaked
        good.connection.assert_called_once()

        gen.close()  # runs the finally: closes the yielded session
        good.close.assert_called()

    def test_connect_failure_raises_after_retries_exhausted(self) -> None:
        bad1 = MagicMock(name="bad1")
        bad1.connection.side_effect = _connect_timeout_error()
        bad2 = MagicMock(name="bad2")
        bad2.connection.side_effect = _connect_timeout_error()
        factory = MagicMock(side_effect=[bad1, bad2])

        gen = dependencies.get_db(self._request_with_factory(factory))
        with self.assertRaises(SQLAlchemyOperationalError):
            next(gen)

        # Default is one retry -> two attempts, both sessions closed, none leaked.
        self.assertEqual(factory.call_count, 2)
        bad1.close.assert_called()
        bad2.close.assert_called()

    def test_retries_are_configurable_via_environment(self) -> None:
        os.environ["DATABASE_CONNECT_RETRY_ATTEMPTS"] = "3"
        sessions = []
        for i in range(3):
            s = MagicMock(name=f"bad{i}")
            s.connection.side_effect = _connect_timeout_error()
            sessions.append(s)
        good = MagicMock(name="good")
        sessions.append(good)
        factory = MagicMock(side_effect=sessions)

        gen = dependencies.get_db(self._request_with_factory(factory))
        yielded = next(gen)

        self.assertIs(yielded, good)
        self.assertEqual(factory.call_count, 4)  # 3 retries + final success
        gen.close()

    def test_healthy_session_connects_without_retry(self) -> None:
        good = MagicMock(name="good_session")
        factory = MagicMock(side_effect=[good])

        gen = dependencies.get_db(self._request_with_factory(factory))
        yielded = next(gen)

        self.assertIs(yielded, good)
        self.assertEqual(factory.call_count, 1)
        good.connection.assert_called_once()
        gen.close()


if __name__ == "__main__":
    unittest.main()
