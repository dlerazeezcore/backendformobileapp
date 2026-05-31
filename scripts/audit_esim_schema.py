"""Read-only audit of the live Supabase eSIM tables.

Usage:

    DATABASE_URL=postgresql+psycopg://... python scripts/audit_esim_schema.py
    # or pass it inline:
    python scripts/audit_esim_schema.py "postgresql+psycopg://..."

Reports:
  * alembic_version (so we know if production is behind the repo).
  * Column-level shape of the six eSIM tables vs. the SQLAlchemy models.
  * Profile rows whose provider_status is ONBOARDING / IN_USE but our local
    app_status disagrees (the bug fixed by migration 0041 + ONBOARDING alias).
  * Orphan order_items (paid eSIM with no profile placeholder).
  * Lifecycle bookkeeping: rows where installed=true but activated_at IS NULL.
  * Top-up capability distribution.

NO writes. Safe to run against production.
"""
from __future__ import annotations

import os
import sys
from typing import Iterable

from sqlalchemy import create_engine, text


ESIM_TABLES = (
    "customer_orders",
    "order_items",
    "esim_profiles",
    "esim_lifecycle_events",
    "payment_attempts",
)


def _resolve_dsn(argv: list[str]) -> str:
    if len(argv) > 1 and argv[1].strip():
        return argv[1].strip()
    dsn = (os.environ.get("DATABASE_URL") or "").strip()
    if not dsn:
        print("ERROR: pass a DSN as argv[1] or set DATABASE_URL", file=sys.stderr)
        sys.exit(2)
    return dsn


def _print_header(title: str) -> None:
    print()
    print(f"=== {title} ===")


def _print_rows(rows: Iterable[dict]) -> None:
    rows = list(rows)
    if not rows:
        print("  (none)")
        return
    for r in rows:
        print("  " + ", ".join(f"{k}={v!r}" for k, v in r.items()))


def main(argv: list[str]) -> int:
    dsn = _resolve_dsn(argv)
    # SQLAlchemy accepts both postgres:// and postgresql+psycopg:// — normalize
    # the older form so SQLAlchemy can pick the driver without a warning.
    if dsn.startswith("postgres://"):
        dsn = "postgresql+psycopg://" + dsn[len("postgres://") :]
    elif dsn.startswith("postgresql://") and "+" not in dsn.split("://", 1)[0]:
        dsn = "postgresql+psycopg://" + dsn[len("postgresql://") :]

    print(f"Connecting (read-only): {dsn.split('@')[-1]}")
    engine = create_engine(dsn, pool_pre_ping=True)

    with engine.connect() as conn:
        _print_header("Alembic revision")
        row = conn.execute(text("SELECT version_num FROM alembic_version")).first()
        print(f"  version_num = {row[0] if row else '(none)'}")

        _print_header("eSIM table column shape")
        cols = conn.execute(
            text(
                """
                SELECT table_name, column_name, data_type, is_nullable
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = ANY(:tables)
                ORDER BY table_name, ordinal_position
                """
            ),
            {"tables": list(ESIM_TABLES)},
        ).mappings().all()
        by_table: dict[str, list[dict]] = {}
        for c in cols:
            by_table.setdefault(c["table_name"], []).append(dict(c))
        for t in ESIM_TABLES:
            print(f"\n  -- {t} ({len(by_table.get(t, []))} columns)")
            for c in by_table.get(t, []):
                print(f"    {c['column_name']:<30} {c['data_type']:<28} nullable={c['is_nullable']}")
            if t not in by_table:
                print("    !!! MISSING TABLE !!!")

        _print_header("Profiles with provider ONBOARDING/IN_USE but app_status != ACTIVE")
        rows = conn.execute(
            text(
                """
                SELECT id, iccid, esim_tran_no, app_status, provider_status,
                       installed, activated_at, validity_days, expires_at
                FROM esim_profiles
                WHERE upper(coalesce(provider_status, '')) IN ('ONBOARDING', 'IN_USE')
                  AND upper(coalesce(app_status, '')) <> 'ACTIVE'
                ORDER BY id DESC
                LIMIT 50
                """
            )
        ).mappings().all()
        _print_rows(rows)
        if rows:
            print(f"  !!! {len(rows)} drift rows — migration 0041 should clear these.")

        _print_header("Profiles where installed=true but activated_at IS NULL")
        rows = conn.execute(
            text(
                """
                SELECT id, iccid, esim_tran_no, app_status, provider_status,
                       installed, installed_at, activated_at
                FROM esim_profiles
                WHERE installed = true AND activated_at IS NULL
                ORDER BY id DESC
                LIMIT 25
                """
            )
        ).mappings().all()
        _print_rows(rows)

        _print_header("Paid eSIM order_items with no profile placeholder")
        rows = conn.execute(
            text(
                """
                SELECT co.id AS customer_order_id, co.user_id, co.order_status,
                       oi.id AS order_item_id, oi.provider_order_no, oi.item_status,
                       oi.booked_at
                FROM customer_orders co
                JOIN order_items oi ON oi.customer_order_id = co.id
                LEFT JOIN esim_profiles ep ON ep.order_item_id = oi.id
                WHERE oi.service_type = 'esim'
                  AND ep.id IS NULL
                ORDER BY oi.booked_at DESC NULLS LAST
                LIMIT 25
                """
            )
        ).mappings().all()
        _print_rows(rows)

        _print_header("Profile counts by app_status")
        rows = conn.execute(
            text(
                """
                SELECT upper(coalesce(app_status, 'NULL')) AS status, count(*) AS n
                FROM esim_profiles
                GROUP BY 1
                ORDER BY n DESC
                """
            )
        ).mappings().all()
        _print_rows(rows)

        _print_header("Profile counts by provider_status")
        rows = conn.execute(
            text(
                """
                SELECT upper(coalesce(provider_status, 'NULL')) AS provider_status, count(*) AS n
                FROM esim_profiles
                GROUP BY 1
                ORDER BY n DESC
                """
            )
        ).mappings().all()
        _print_rows(rows)

        _print_header("Top-up support distribution")
        rows = conn.execute(
            text(
                """
                SELECT COALESCE(custom_fields ->> 'supportTopUpType', '(missing)') AS support_top_up_type,
                       count(*) AS n
                FROM esim_profiles
                GROUP BY 1
                ORDER BY n DESC
                """
            )
        ).mappings().all()
        _print_rows(rows)

    print()
    print("Audit done.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
