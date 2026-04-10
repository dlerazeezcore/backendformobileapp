"""backfill successful payment attempts from order payment fields

Revision ID: 0023_backfill_order_payments
Revises: 0022_order_payment_fields
Create Date: 2026-04-10 19:05:00
"""

from __future__ import annotations

import uuid

from alembic import op
import sqlalchemy as sa


revision = "0023_backfill_order_payments"
down_revision = "0022_order_payment_fields"
branch_labels = None
depends_on = None


def _table_names() -> set[str]:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return set(inspector.get_table_names())


def _normalize_provider(method: str | None, provider: str | None) -> str | None:
    normalized_method = str(method or "").strip().lower() or None
    normalized_provider = str(provider or "").strip().lower() or None
    if normalized_method and normalized_provider is None:
        return "internal_loyalty" if normalized_method == "loyalty" else normalized_method
    return normalized_provider


def upgrade() -> None:
    if not {"payment_attempts", "order_items", "customer_orders"}.issubset(_table_names()):
        return

    bind = op.get_bind()
    payment_attempts = sa.Table("payment_attempts", sa.MetaData(), autoload_with=bind)
    rows = bind.execute(
        sa.text(
            """
            SELECT
                oi.id AS order_item_id,
                oi.customer_order_id,
                oi.payment_method,
                oi.payment_provider,
                oi.provider_transaction_id,
                oi.provider_order_no,
                oi.sale_price_minor,
                oi.service_type,
                oi.booked_at AS item_booked_at,
                co.user_id,
                co.currency_code,
                co.total_minor,
                co.booked_at AS order_booked_at
            FROM order_items oi
            JOIN customer_orders co ON co.id = oi.customer_order_id
            WHERE oi.payment_method IS NOT NULL
            ORDER BY oi.id ASC
            """
        )
    ).mappings()

    for row in rows:
        order_item_id = row.get("order_item_id")
        customer_order_id = row.get("customer_order_id")
        method = str(row.get("payment_method") or "").strip().lower()
        if not method:
            continue
        existing = bind.execute(
            sa.text("SELECT id FROM payment_attempts WHERE order_item_id = :order_item_id LIMIT 1"),
            {"order_item_id": order_item_id},
        ).fetchone()
        if existing is not None:
            continue

        user_id = row.get("user_id")
        if user_id is None:
            continue

        provider = _normalize_provider(method, row.get("payment_provider"))
        transaction_id = f"backfill-{method}-order-item-{order_item_id}"
        already_by_tx = bind.execute(
            sa.text("SELECT id FROM payment_attempts WHERE transaction_id = :transaction_id LIMIT 1"),
            {"transaction_id": transaction_id},
        ).fetchone()
        if already_by_tx is not None:
            continue

        amount_minor = row.get("sale_price_minor")
        if amount_minor is None:
            amount_minor = row.get("total_minor") or 0

        paid_at = row.get("item_booked_at") or row.get("order_booked_at")

        bind.execute(
            sa.insert(payment_attempts).values(
                id=str(uuid.uuid4()),
                customer_order_id=customer_order_id,
                order_item_id=order_item_id,
                user_id=str(user_id),
                service_type=str(row.get("service_type") or "esim"),
                payment_method=method,
                provider=provider,
                status="paid",
                amount_minor=int(amount_minor),
                currency_code=str(row.get("currency_code") or "IQD").upper(),
                provider_payment_id=row.get("provider_transaction_id"),
                provider_reference=row.get("provider_order_no"),
                transaction_id=transaction_id,
                metadata={"source": "backfill_order_payment_fields"},
                provider_request={},
                provider_response={},
                paid_at=paid_at,
            )
        )


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text(
            """
            DELETE FROM payment_attempts
            WHERE transaction_id LIKE 'backfill-%-order-item-%'
            """
        )
    )
