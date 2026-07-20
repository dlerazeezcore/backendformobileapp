"""Report FIB payments that were confirmed paid but never delivered.

Provisioning is triggered by the app calling /orders/managed (or
/topups/managed). If the app died in between, the customer was charged and got
nothing — and we do not refund. The cron finalizer
(/api/v1/internal/cron/finalize-unprovisioned-orders) now recovers these
automatically, but ONLY when the payment carries the orderIntent/topupIntent the
app persists at checkout. Payments taken by app builds from BEFORE that change
have no intent, so the finalizer deliberately skips them and they need a human.

This script finds every paid-but-undelivered FIB payment and splits them into:

  * RECOVERABLE — has an intent; the cron will (or already did) handle it.
    Nothing to do unless it keeps reappearing, which means the sweep is failing.
  * MANUAL      — no intent (legacy build). Fulfil by hand. The report prints
    the fulfilment hints the old builds did send (packageCode / place / iccid),
    which is normally enough to identify what the customer paid for.

READ-ONLY. This script performs no writes and never spends provider credit —
provisioning a legacy payment is a judgement call (the package may no longer
exist at that price), so it is deliberately left to an operator.

Usage:

    DATABASE_URL=postgresql+psycopg://... python scripts/report_undelivered_fib_payments.py
    # or pass the DSN inline:
    python scripts/report_undelivered_fib_payments.py "postgresql+psycopg://..."

    # Only payments older than N hours (default 1 — skips live checkouts):
    DATABASE_URL=... python scripts/report_undelivered_fib_payments.py --hours 24

    # Machine-readable output for piping into a ticket/spreadsheet:
    DATABASE_URL=... python scripts/report_undelivered_fib_payments.py --json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import timedelta

# Allow running as `python scripts/report_undelivered_fib_payments.py` from the
# Backend/ root (the sibling modules live one directory up).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from supabase_store import (  # noqa: E402
    APP_TIMEZONE,
    AppUser,
    PaymentAttempt,
    normalize_database_url,
    utcnow,
)


def _resolve_dsn(argv_dsn: str | None) -> str:
    dsn = argv_dsn or os.environ.get("DATABASE_URL") or ""
    if not dsn.strip():
        print(
            "ERROR: no database URL. Pass it inline or set DATABASE_URL.\n"
            "  DATABASE_URL=postgresql+psycopg://... python scripts/report_undelivered_fib_payments.py",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return normalize_database_url(dsn.strip())


def _classify(meta: dict) -> tuple[str, str]:
    """(bucket, why) for one payment's metadata."""
    if isinstance(meta.get("orderIntent"), dict):
        return "RECOVERABLE", "orderIntent present - cron finalizer handles it"
    if isinstance(meta.get("topupIntent"), dict):
        return "RECOVERABLE", "topupIntent present - cron finalizer handles it"
    if meta.get("kind") == "topup" or meta.get("iccid"):
        return "MANUAL", "legacy top-up (no intent)"
    return "MANUAL", "legacy order (no intent)"


def _hints(meta: dict) -> str:
    """Fulfilment hints the pre-intent builds did send."""
    parts = []
    for key in ("packageCode", "place", "iccid", "kind"):
        value = meta.get(key)
        if isinstance(value, (str, int)) and str(value).strip():
            parts.append(f"{key}={value}")
    return ", ".join(parts) or "(no hints in metadata)"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dsn", nargs="?", default=None)
    parser.add_argument(
        "--hours",
        type=float,
        default=1.0,
        help="Only payments paid more than N hours ago (default 1, skips live checkouts).",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of a table.")
    args = parser.parse_args()

    engine = create_engine(_resolve_dsn(args.dsn), future=True)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

    cutoff = utcnow() - timedelta(hours=args.hours)
    rows: list[dict] = []
    with session_factory() as session:
        # Undelivered == paid, but never bound to an order. Top-ups never bind,
        # so a topupClaim is what marks those as already handled.
        attempts = session.scalars(
            select(PaymentAttempt)
            .where(
                PaymentAttempt.payment_method == "fib",
                PaymentAttempt.status == "paid",
                PaymentAttempt.customer_order_id.is_(None),
                PaymentAttempt.order_item_id.is_(None),
            )
            .order_by(PaymentAttempt.paid_at.asc())
        ).all()
        for attempt in attempts:
            meta = attempt.metadata_payload or {}
            if meta.get("topupClaim") or meta.get("topupApplied"):
                continue  # a top-up that was applied
            paid_at = attempt.paid_at
            if paid_at is not None:
                # Aware in Postgres (timestamptz); naive only on SQLite, where the
                # written value was APP_TIMEZONE — utcnow() is not actually UTC.
                reference = paid_at if paid_at.tzinfo is not None else paid_at.replace(tzinfo=APP_TIMEZONE)
                if reference > cutoff:
                    continue  # still inside the live-checkout window
            phone = None
            if attempt.user_id:
                user = session.scalar(select(AppUser).where(AppUser.id == attempt.user_id))
                phone = getattr(user, "phone", None)
            bucket, why = _classify(meta)
            rows.append(
                {
                    "bucket": bucket,
                    "why": why,
                    "paymentAttemptId": attempt.id,
                    "providerPaymentId": attempt.provider_payment_id,
                    "userId": attempt.user_id,
                    "phone": phone,
                    "amountMinor": attempt.amount_minor,
                    "currency": attempt.currency_code,
                    "paidAt": paid_at.isoformat() if paid_at else None,
                    "hints": _hints(meta),
                }
            )

    if args.json:
        print(json.dumps(rows, indent=2))
        return

    manual = [r for r in rows if r["bucket"] == "MANUAL"]
    recoverable = [r for r in rows if r["bucket"] == "RECOVERABLE"]

    print(f"Paid-but-undelivered FIB payments older than {args.hours}h: {len(rows)}")
    print(f"  MANUAL (legacy, needs a human): {len(manual)}")
    print(f"  RECOVERABLE (cron handles it):  {len(recoverable)}")
    if recoverable:
        print(
            "\nNOTE: RECOVERABLE rows should disappear within ~15 minutes. If they\n"
            "persist, the sweep is failing - check the 'Finalize undelivered\n"
            "purchases (cron)' workflow run log for the 'failed' array."
        )
    for title, group in (("MANUAL", manual), ("RECOVERABLE", recoverable)):
        if not group:
            continue
        print(f"\n=== {title} ===")
        for r in group:
            amount = f"{r['amountMinor']} {r['currency']}"
            print(
                f"  {r['paidAt']}  {amount:>16}  user={r['phone'] or r['userId']}\n"
                f"      attempt={r['paymentAttemptId']}  fibPayment={r['providerPaymentId']}\n"
                f"      {r['why']} | {r['hints']}"
            )
    if not rows:
        # Plain ASCII: ops shells on Windows default to cp1252 and a stray emoji
        # crashes the whole report with UnicodeEncodeError.
        print("\nNothing undelivered.")


if __name__ == "__main__":
    main()
