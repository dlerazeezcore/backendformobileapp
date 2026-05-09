"""
Stream every public table from the Sydney Supabase project to Frankfurt
using server-side COPY. Idempotent re-runs are not safe -- run once.

Usage:
    SYDNEY_URL=postgres://...syd python copy_sydney_to_frankfurt.py
    FRANKFURT_URL=postgres://...frk python copy_sydney_to_frankfurt.py

Strategy:
1. Connect to both projects with a SINGLE direct connection each
   (pooler is fine; we just stream COPY through it).
2. For each table in FK dependency order:
   a. COPY ... TO STDOUT BINARY from Sydney
   b. COPY ... FROM STDIN BINARY into Frankfurt
3. After all tables, reset auto-increment sequences to MAX(id) so
   future inserts pick up where production left off.
4. Print row counts before / after for verification.

Tables to skip:
  alembic_version (Frankfurt already has its own row at the right head)
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg

SYDNEY_URL = os.environ.get("SYDNEY_URL")
FRANKFURT_URL = os.environ.get("FRANKFURT_URL")
if not SYDNEY_URL or not FRANKFURT_URL:
    print("ERROR: set SYDNEY_URL and FRANKFURT_URL env vars", file=sys.stderr)
    sys.exit(2)

# FK dependency order: parents first, children last. Within each tier, order
# is alphabetical for determinism.
COPY_ORDER = [
    # tier 0: no FK deps on app data
    "admin_users",
    "app_users",
    "discount_rules",
    "exchange_rates",
    "featured_locations",
    "pricing_rules",
    # tier 1: depend on users only
    "customer_orders",      # -> app_users
    # tier 2
    "order_items",          # -> customer_orders
    # tier 3
    "esim_profiles",        # -> order_items, app_users
    "payment_attempts",     # -> customer_orders, order_items, app_users, admin_users
    "push_devices",         # -> app_users, admin_users
    "push_notifications",   # -> admin_users
    # tier 4
    "esim_lifecycle_events",   # -> customer_orders, order_items, esim_profiles
    "payment_provider_events", # -> payment_attempts
]

# Tables with serial/identity primary keys whose sequence must be reset after
# bulk insert with explicit IDs. UUID PK tables are excluded (no sequence).
TABLES_WITH_SERIAL_PK = [
    ("customer_orders", "id"),
    ("discount_rules", "id"),
    ("esim_lifecycle_events", "id"),
    ("esim_profiles", "id"),
    ("exchange_rates", "id"),
    ("featured_locations", "id"),
    ("order_items", "id"),
    ("payment_provider_events", "id"),
    ("pricing_rules", "id"),
    ("push_devices", "id"),
]


def short_url(url: str) -> str:
    # Mask the password for log lines.
    import re
    return re.sub(r"://([^:]+):[^@]+@", r"://\1:***@", url)


def count_rows(conn: psycopg.Connection, table: str) -> int:
    with conn.cursor() as c:
        c.execute(f"SELECT count(*) FROM {table}")
        row = c.fetchone()
        return int(row[0]) if row else 0


def copy_table(src: psycopg.Connection, dst: psycopg.Connection, table: str) -> int:
    """Stream one table src -> dst using BINARY COPY. Returns rows copied."""
    src_rows = count_rows(src, table)
    if src_rows == 0:
        print(f"  [skip] {table}: empty in source")
        return 0
    dst_rows_before = count_rows(dst, table)
    if dst_rows_before > 0:
        # We don't auto-truncate -- safer to fail fast and let the operator decide.
        raise RuntimeError(
            f"{table} already has {dst_rows_before} rows in Frankfurt; refusing to merge."
        )

    with src.cursor() as src_cur, dst.cursor() as dst_cur:
        with src_cur.copy(f"COPY {table} TO STDOUT (FORMAT BINARY)") as src_copy:
            with dst_cur.copy(f"COPY {table} FROM STDIN (FORMAT BINARY)") as dst_copy:
                while True:
                    chunk = src_copy.read()
                    if not chunk:
                        break
                    dst_copy.write(chunk)

    dst_rows_after = count_rows(dst, table)
    if dst_rows_after != src_rows:
        raise RuntimeError(
            f"row count mismatch for {table}: src={src_rows} dst={dst_rows_after}"
        )
    print(f"  [ok]   {table}: {src_rows} rows")
    return src_rows


def reset_sequences(conn: psycopg.Connection) -> None:
    print("Resetting sequences...")
    with conn.cursor() as c:
        for table, pk in TABLES_WITH_SERIAL_PK:
            # pg_get_serial_sequence returns the actual sequence name; setval to MAX(id)
            c.execute(
                f"""
                SELECT setval(
                    pg_get_serial_sequence('{table}', '{pk}'),
                    COALESCE((SELECT MAX({pk}) FROM {table}), 1),
                    (SELECT MAX({pk}) IS NOT NULL FROM {table})
                )
                """
            )
            row = c.fetchone()
            print(f"  [seq]  {table}.{pk} -> {row[0] if row else '?'}")


def main() -> int:
    print(f"src: {short_url(SYDNEY_URL)}")
    print(f"dst: {short_url(FRANKFURT_URL)}")
    print()

    with psycopg.connect(SYDNEY_URL, autocommit=True) as src:
        with psycopg.connect(FRANKFURT_URL, autocommit=False) as dst:
            print("Pre-flight row counts (src / dst):")
            for table in COPY_ORDER:
                src_n = count_rows(src, table)
                dst_n = count_rows(dst, table)
                marker = "" if dst_n == 0 else "  <-- destination NOT empty"
                print(f"  {table:30s} {src_n:>8d} / {dst_n:>8d}{marker}")
            print()

            print("Copying...")
            total = 0
            for table in COPY_ORDER:
                total += copy_table(src, dst, table)
            dst.commit()
            print(f"\nCopied {total} rows total across {len(COPY_ORDER)} tables.\n")

            reset_sequences(dst)
            dst.commit()

            print("\nFinal row counts (frankfurt):")
            for table in COPY_ORDER:
                n = count_rows(dst, table)
                print(f"  {table:30s} {n:>8d}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
