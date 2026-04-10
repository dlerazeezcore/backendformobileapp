"""add payment method/provider columns to orders

Revision ID: 0022_order_payment_fields
Revises: 0021_alembic_version_len
Create Date: 2026-04-10 18:45:00
"""

from __future__ import annotations

import json

from alembic import op
import sqlalchemy as sa


revision = "0022_order_payment_fields"
down_revision = "0021_alembic_version_len"
branch_labels = None
depends_on = None


def _column_names(table_name: str) -> set[str]:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return {column["name"] for column in inspector.get_columns(table_name)}


def _index_names(table_name: str) -> set[str]:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return {index["name"] for index in inspector.get_indexes(table_name)}


def _normalize_json(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _normalize_method_provider(method: object, provider: object) -> tuple[str | None, str | None]:
    normalized_method = str(method or "").strip().lower() or None
    normalized_provider = str(provider or "").strip().lower() or None
    if normalized_method and normalized_provider is None:
        normalized_provider = "internal_loyalty" if normalized_method == "loyalty" else normalized_method
    return normalized_method, normalized_provider


def upgrade() -> None:
    order_columns = _column_names("order_items")
    if "payment_method" not in order_columns:
        op.add_column("order_items", sa.Column("payment_method", sa.String(length=32), nullable=True))
    if "payment_provider" not in order_columns:
        op.add_column("order_items", sa.Column("payment_provider", sa.String(length=64), nullable=True))

    customer_columns = _column_names("customer_orders")
    if "payment_method" not in customer_columns:
        op.add_column("customer_orders", sa.Column("payment_method", sa.String(length=32), nullable=True))
    if "payment_provider" not in customer_columns:
        op.add_column("customer_orders", sa.Column("payment_provider", sa.String(length=64), nullable=True))

    order_indexes = _index_names("order_items")
    if "ix_order_items_payment_method" not in order_indexes:
        op.create_index("ix_order_items_payment_method", "order_items", ["payment_method"], unique=False)
    if "ix_order_items_payment_provider" not in order_indexes:
        op.create_index("ix_order_items_payment_provider", "order_items", ["payment_provider"], unique=False)

    customer_indexes = _index_names("customer_orders")
    if "ix_customer_orders_payment_method" not in customer_indexes:
        op.create_index("ix_customer_orders_payment_method", "customer_orders", ["payment_method"], unique=False)
    if "ix_customer_orders_payment_provider" not in customer_indexes:
        op.create_index("ix_customer_orders_payment_provider", "customer_orders", ["payment_provider"], unique=False)

    bind = op.get_bind()

    order_payment_map: dict[int, tuple[str | None, str | None]] = {}
    payment_rows = bind.execute(
        sa.text(
            """
            SELECT order_item_id, payment_method, provider
            FROM payment_attempts
            WHERE order_item_id IS NOT NULL
            ORDER BY created_at DESC
            """
        )
    ).mappings()
    for row in payment_rows:
        order_item_id = row.get("order_item_id")
        if order_item_id is None or order_item_id in order_payment_map:
            continue
        order_payment_map[int(order_item_id)] = _normalize_method_provider(row.get("payment_method"), row.get("provider"))

    order_rows = bind.execute(
        sa.text("SELECT id, custom_fields, payment_method, payment_provider, customer_order_id FROM order_items")
    ).mappings()
    for row in order_rows:
        order_item_id = int(row["id"])
        method, provider = order_payment_map.get(order_item_id, (None, None))
        if method is None:
            custom_fields = _normalize_json(row.get("custom_fields"))
            method, provider = _normalize_method_provider(
                custom_fields.get("paymentMethod") or custom_fields.get("payment_method"),
                custom_fields.get("paymentProvider") or custom_fields.get("payment_provider"),
            )
        current_method = str(row.get("payment_method") or "").strip().lower() or None
        current_provider = str(row.get("payment_provider") or "").strip().lower() or None
        if (method and method != current_method) or (provider and provider != current_provider):
            bind.execute(
                sa.text(
                    """
                    UPDATE order_items
                    SET payment_method = COALESCE(:payment_method, payment_method),
                        payment_provider = COALESCE(:payment_provider, payment_provider)
                    WHERE id = :id
                    """
                ),
                {
                    "id": order_item_id,
                    "payment_method": method,
                    "payment_provider": provider,
                },
            )

    customer_payment_map: dict[int, tuple[str | None, str | None]] = {}
    customer_rows_from_attempt = bind.execute(
        sa.text(
            """
            SELECT customer_order_id, payment_method, provider
            FROM payment_attempts
            WHERE customer_order_id IS NOT NULL
            ORDER BY created_at DESC
            """
        )
    ).mappings()
    for row in customer_rows_from_attempt:
        order_id = row.get("customer_order_id")
        if order_id is None or order_id in customer_payment_map:
            continue
        customer_payment_map[int(order_id)] = _normalize_method_provider(row.get("payment_method"), row.get("provider"))

    item_rows = bind.execute(
        sa.text(
            """
            SELECT customer_order_id, payment_method, payment_provider
            FROM order_items
            WHERE customer_order_id IS NOT NULL
            ORDER BY created_at DESC
            """
        )
    ).mappings()
    for row in item_rows:
        order_id = row.get("customer_order_id")
        if order_id is None or order_id in customer_payment_map:
            continue
        method, provider = _normalize_method_provider(row.get("payment_method"), row.get("payment_provider"))
        if method:
            customer_payment_map[int(order_id)] = (method, provider)

    customer_rows = bind.execute(
        sa.text("SELECT id, payment_method, payment_provider FROM customer_orders")
    ).mappings()
    for row in customer_rows:
        order_id = int(row["id"])
        method, provider = customer_payment_map.get(order_id, (None, None))
        current_method = str(row.get("payment_method") or "").strip().lower() or None
        current_provider = str(row.get("payment_provider") or "").strip().lower() or None
        if (method and method != current_method) or (provider and provider != current_provider):
            bind.execute(
                sa.text(
                    """
                    UPDATE customer_orders
                    SET payment_method = COALESCE(:payment_method, payment_method),
                        payment_provider = COALESCE(:payment_provider, payment_provider)
                    WHERE id = :id
                    """
                ),
                {
                    "id": order_id,
                    "payment_method": method,
                    "payment_provider": provider,
                },
            )


def downgrade() -> None:
    customer_columns = _column_names("customer_orders")
    customer_indexes = _index_names("customer_orders")
    if "ix_customer_orders_payment_provider" in customer_indexes:
        op.drop_index("ix_customer_orders_payment_provider", table_name="customer_orders")
    if "ix_customer_orders_payment_method" in customer_indexes:
        op.drop_index("ix_customer_orders_payment_method", table_name="customer_orders")
    if "payment_provider" in customer_columns:
        op.drop_column("customer_orders", "payment_provider")
    if "payment_method" in customer_columns:
        op.drop_column("customer_orders", "payment_method")

    order_columns = _column_names("order_items")
    order_indexes = _index_names("order_items")
    if "ix_order_items_payment_provider" in order_indexes:
        op.drop_index("ix_order_items_payment_provider", table_name="order_items")
    if "ix_order_items_payment_method" in order_indexes:
        op.drop_index("ix_order_items_payment_method", table_name="order_items")
    if "payment_provider" in order_columns:
        op.drop_column("order_items", "payment_provider")
    if "payment_method" in order_columns:
        op.drop_column("order_items", "payment_method")
