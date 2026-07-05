"""Housekeeping: prune stale ANONYMOUS push devices (no user, no admin owner).

Anonymous device rows accumulate forever — every fresh install / simulator /
bot that hits POST /push-notifications/devices/register before signing in
creates one, and nothing ever removes it. This script hard-deletes anonymous
rows whose ``last_seen_at`` is older than the retention window. Owned devices
(a real user or admin account) are NEVER touched.

There is no scheduler in this backend, so run this from cron / a manual ops
session, the same way as the other scripts in this folder.

Usage:

    # DRY RUN (default): report how many rows WOULD be deleted, change nothing.
    DATABASE_URL=postgresql+psycopg://... python scripts/cleanup_anonymous_push_devices.py
    # or pass the DSN inline:
    python scripts/cleanup_anonymous_push_devices.py "postgresql+psycopg://..."

    # COMMIT the delete:
    DATABASE_URL=... python scripts/cleanup_anonymous_push_devices.py --commit

    # Override the retention window (default: PUSH_ANONYMOUS_DEVICE_RETENTION_DAYS or 90):
    DATABASE_URL=... python scripts/cleanup_anonymous_push_devices.py --days 120 --commit

Safe to run repeatedly. Dry run performs no writes.
"""
from __future__ import annotations

import os
import sys

# Allow running as `python scripts/cleanup_anonymous_push_devices.py` from the
# Backend/ root (the sibling modules live one directory up).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from supabase_store import SupabaseStore  # noqa: E402


def _resolve_dsn(argv: list[str]) -> str:
    for arg in argv[1:]:
        if arg and not arg.startswith("-"):
            return arg.strip()
    dsn = os.environ.get("DATABASE_URL", "").strip()
    if not dsn:
        print("ERROR: set DATABASE_URL or pass the DSN as the first argument.", file=sys.stderr)
        sys.exit(2)
    return dsn


def _resolve_days(argv: list[str]) -> int:
    if "--days" in argv:
        try:
            return int(argv[argv.index("--days") + 1])
        except (IndexError, ValueError):
            print("ERROR: --days requires an integer value.", file=sys.stderr)
            sys.exit(2)
    return int(os.environ.get("PUSH_ANONYMOUS_DEVICE_RETENTION_DAYS", "90") or "90")


def main(argv: list[str]) -> int:
    commit = "--commit" in argv
    days = _resolve_days(argv)
    if days <= 0:
        print(f"Retention window is {days} days (<= 0) — nothing to do.")
        return 0

    engine = create_engine(_resolve_dsn(argv), future=True)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

    with session_factory() as session:
        store = SupabaseStore(session)
        stale = store.count_stale_anonymous_push_devices(older_than_days=days)
        print(f"Stale anonymous push devices older than {days} days: {stale}")
        if not commit:
            print("DRY RUN — no rows deleted. Re-run with --commit to delete them.")
            return 0
        deleted = store.delete_stale_anonymous_push_devices(older_than_days=days)
        session.commit()
        print(f"Deleted {deleted} stale anonymous push device(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
