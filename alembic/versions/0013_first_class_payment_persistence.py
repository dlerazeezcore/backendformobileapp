"""first-class payment persistence with provider event ledger

Revision ID: 0013_payment_persistence_v2
Revises: 0012_add_payment_attempts
Create Date: 2026-04-08 00:45:00
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

from alembic import op
import sqlalchemy as sa


revision = "0013_payment_persistence_v2"
down_revision = "0012_add_payment_attempts"
branch_labels = None
depends_on = None


def _table_names() -> set[str]:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return set(inspector.get_table_names())


def _index_names(table_name: str) -> set[str]:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return {index["name"] for index in inspector.get_indexes(table_name)}


def _safe_json(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            return {}
    return {}


def _normalize_status(value: Any) -> str:
    status = str(value or "pending").strip().lower()
    if status == "cancelled":
        return "canceled"
    return status


def _json_type(bind: Any) -> Any:
    if bind.dialect.name == "postgresql":
        return sa.dialects.postgresql.JSONB(astext_type=sa.Text())
    return sa.JSON()


def _create_payment_attempts_table() -> None:
    bind = op.get_bind()
    json_type = _json_type(bind)
    op.create_table(
        "payment_attempts",
        sa.Column("id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("customer_order_id", sa.Integer(), nullable=True),
        sa.Column("order_item_id", sa.Integer(), nullable=True),
        sa.Column("user_id", sa.Uuid(as_uuid=False), nullable=True),
        sa.Column("service_type", sa.String(length=32), nullable=False, server_default="esim"),
        sa.Column("payment_method", sa.String(length=32), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency_code", sa.String(length=8), nullable=False),
        sa.Column("provider_payment_id", sa.String(length=255), nullable=True),
        sa.Column("provider_reference", sa.String(length=255), nullable=True),
        sa.Column("transaction_id", sa.String(length=255), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("metadata", json_type, nullable=False, server_default=sa.text("'{}'")),
        sa.Column("provider_request", json_type, nullable=False, server_default=sa.text("'{}'")),
        sa.Column("provider_response", json_type, nullable=False, server_default=sa.text("'{}'")),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("canceled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["customer_order_id"], ["customer_orders.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["order_item_id"], ["order_items.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["app_users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("transaction_id", name="uq_payment_attempts_transaction_id"),
        sa.UniqueConstraint("provider", "provider_payment_id", name="uq_payment_attempts_provider_payment_id"),
    )
    op.create_index("ix_payment_attempts_customer_order_id", "payment_attempts", ["customer_order_id"], unique=False)
    op.create_index("ix_payment_attempts_order_item_id", "payment_attempts", ["order_item_id"], unique=False)
    op.create_index("ix_payment_attempts_user_created", "payment_attempts", ["user_id", "created_at"], unique=False)
    op.create_index("ix_payment_attempts_status_created", "payment_attempts", ["status", "created_at"], unique=False)
    op.create_index("ix_payment_attempts_method_created", "payment_attempts", ["payment_method", "created_at"], unique=False)


def _create_payment_provider_events_table() -> None:
    bind = op.get_bind()
    json_type = _json_type(bind)
    op.create_table(
        "payment_provider_events",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            primary_key=True,
            autoincrement=True,
            nullable=False,
        ),
        sa.Column("payment_attempt_id", sa.Uuid(as_uuid=False), nullable=True),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("event_type", sa.String(length=128), nullable=False),
        sa.Column("provider_event_id", sa.String(length=255), nullable=True),
        sa.Column("signature_valid", sa.Boolean(), nullable=True),
        sa.Column("raw_payload", json_type, nullable=False, server_default=sa.text("'{}'")),
        sa.Column("processed", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("processing_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["payment_attempt_id"], ["payment_attempts.id"], ondelete="SET NULL"),
    )
    op.create_index(
        "ix_payment_provider_events_provider_event_id",
        "payment_provider_events",
        ["provider", "provider_event_id"],
        unique=False,
    )
    op.create_index("ix_payment_provider_events_attempt_id", "payment_provider_events", ["payment_attempt_id"], unique=False)
    op.create_index(
        "ix_payment_provider_events_processed_created",
        "payment_provider_events",
        ["processed", "created_at"],
        unique=False,
    )


def _copy_old_payment_attempts(legacy_rows: list[dict[str, Any]]) -> None:
    bind = op.get_bind()
    if not legacy_rows:
        return
    payment_attempts = sa.Table("payment_attempts", sa.MetaData(), autoload_with=bind)

    order_map = {
        row.id: (row.customer_order_id, row.service_type)
        for row in bind.execute(sa.text("SELECT id, customer_order_id, service_type FROM order_items")).mappings()
    }
    for row in legacy_rows:
        old_id = row.get("id")
        if old_id is None:
            new_id = str(uuid.uuid4())
        else:
            try:
                new_id = str(uuid.UUID(str(old_id)))
            except Exception:
                new_id = str(uuid.uuid4())
        order_item_id = row.get("order_item_id")
        derived_order = order_map.get(order_item_id) if order_item_id is not None else None
        customer_order_id = row.get("customer_order_id")
        if customer_order_id is None and derived_order is not None:
            customer_order_id = derived_order[0]
        service_type = row.get("service_type")
        if not isinstance(service_type, str) or not service_type.strip():
            service_type = (derived_order[1] if derived_order and derived_order[1] else "esim")
        provider = row.get("provider")
        provider = provider.strip().lower() if isinstance(provider, str) and provider.strip() else None
        payment_method = row.get("payment_method")
        if not isinstance(payment_method, str) or not payment_method.strip():
            payment_method = "fib"
        payload = {
            "id": new_id,
            "customer_order_id": customer_order_id,
            "order_item_id": order_item_id,
            "user_id": row.get("user_id"),
            "service_type": service_type,
            "payment_method": payment_method.strip().lower(),
            "provider": provider,
            "status": _normalize_status(row.get("status")),
            "amount_minor": int(row.get("amount_minor") or 0),
            "currency_code": str(row.get("currency_code") or "IQD").upper(),
            "provider_payment_id": row.get("provider_payment_id"),
            "provider_reference": row.get("provider_reference"),
            "transaction_id": str(row.get("transaction_id") or f"legacy-{new_id}"),
            "idempotency_key": row.get("idempotency_key"),
            "failure_reason": row.get("failure_reason"),
            "metadata": _safe_json(row.get("metadata")),
            "provider_request": _safe_json(row.get("provider_request")),
            "provider_response": _safe_json(row.get("provider_response")),
            "paid_at": row.get("paid_at"),
            "failed_at": row.get("failed_at"),
            "canceled_at": row.get("canceled_at"),
            "created_at": row.get("created_at") or datetime.utcnow(),
            "updated_at": row.get("updated_at") or datetime.utcnow(),
        }
        bind.execute(sa.insert(payment_attempts).values(**payload))


def _backfill_from_order_items() -> None:
    bind = op.get_bind()
    payment_attempts = sa.Table("payment_attempts", sa.MetaData(), autoload_with=bind)
    rows = bind.execute(
        sa.text(
            """
            SELECT
                oi.id AS order_item_id,
                oi.customer_order_id,
                oi.service_type,
                oi.sale_price_minor,
                oi.custom_fields,
                co.user_id,
                co.currency_code
            FROM order_items oi
            LEFT JOIN customer_orders co ON co.id = oi.customer_order_id
            """
        )
    ).mappings()
    for row in rows:
        custom_fields = _safe_json(row.get("custom_fields"))
        method_raw = custom_fields.get("paymentMethod") or custom_fields.get("payment_method")
        if not isinstance(method_raw, str) or not method_raw.strip():
            continue
        payment_method = method_raw.strip().lower()
        provider = "fib" if payment_method == "fib" else "internal_loyalty" if payment_method == "loyalty" else payment_method
        transaction_id = f"backfill-order-item-{row['order_item_id']}"
        exists = bind.execute(
            sa.select(payment_attempts.c.id).where(payment_attempts.c.transaction_id == transaction_id)
        ).first()
        if exists is not None:
            continue
        amount_minor = int(row.get("sale_price_minor") or 0)
        currency_code = str(row.get("currency_code") or "IQD").upper()
        metadata = {
            "source": "backfill_order_items_custom_fields",
            "legacyPaymentMethod": method_raw,
            "orderItemId": row["order_item_id"],
        }
        bind.execute(
            sa.insert(payment_attempts).values(
                id=str(uuid.uuid4()),
                customer_order_id=row.get("customer_order_id"),
                order_item_id=row.get("order_item_id"),
                user_id=row.get("user_id"),
                service_type=(row.get("service_type") or "esim"),
                payment_method=payment_method,
                provider=provider,
                status="paid",
                amount_minor=amount_minor,
                currency_code=currency_code,
                transaction_id=transaction_id,
                metadata=metadata,
                provider_request={},
                provider_response={},
                paid_at=datetime.utcnow(),
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            )
        )


def upgrade() -> None:
    tables = _table_names()
    bind = op.get_bind()
    legacy_rows: list[dict[str, Any]] = []
    if "payment_attempts" in tables:
        legacy_rows = [dict(row) for row in bind.execute(sa.text("SELECT * FROM payment_attempts")).mappings().all()]
        op.drop_table("payment_attempts")
    _create_payment_attempts_table()
    _copy_old_payment_attempts(legacy_rows)
    _backfill_from_order_items()

    if "payment_provider_events" not in _table_names():
        _create_payment_provider_events_table()


def downgrade() -> None:
    if "payment_provider_events" in _table_names():
        index_names = _index_names("payment_provider_events")
        for index_name in (
            "ix_payment_provider_events_processed_created",
            "ix_payment_provider_events_attempt_id",
            "ix_payment_provider_events_provider_event_id",
        ):
            if index_name in index_names:
                op.drop_index(index_name, table_name="payment_provider_events")
        op.drop_table("payment_provider_events")

    if "payment_attempts" in _table_names():
        index_names = _index_names("payment_attempts")
        for index_name in (
            "ix_payment_attempts_method_created",
            "ix_payment_attempts_status_created",
            "ix_payment_attempts_user_created",
            "ix_payment_attempts_order_item_id",
            "ix_payment_attempts_customer_order_id",
        ):
            if index_name in index_names:
                op.drop_index(index_name, table_name="payment_attempts")
        op.drop_table("payment_attempts")

    # Restore pre-0013 layout by rerunning 0012 upgrade semantics.
    bind = op.get_bind()
    json_type = _json_type(bind)
    op.create_table(
        "payment_attempts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("transaction_id", sa.String(length=255), nullable=False),
        sa.Column("payment_method", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("provider_payment_id", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("amount_minor", sa.Integer(), nullable=False),
        sa.Column("currency_code", sa.String(length=8), nullable=False),
        sa.Column("user_id", sa.Uuid(as_uuid=False), nullable=True),
        sa.Column("service_type", sa.String(length=64), nullable=True),
        sa.Column("order_item_id", sa.Integer(), nullable=True),
        sa.Column("metadata", json_type, nullable=False),
        sa.Column("provider_request", json_type, nullable=False),
        sa.Column("provider_response", json_type, nullable=False),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["order_item_id"], ["order_items.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["app_users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("transaction_id", name="uq_payment_attempts_transaction_id"),
        sa.UniqueConstraint("provider", "provider_payment_id", name="uq_payment_attempts_provider_payment_id"),
    )
    op.create_index("ix_payment_attempts_payment_method", "payment_attempts", ["payment_method"], unique=False)
    op.create_index("ix_payment_attempts_provider", "payment_attempts", ["provider"], unique=False)
    op.create_index("ix_payment_attempts_status", "payment_attempts", ["status"], unique=False)
    op.create_index("ix_payment_attempts_user_id", "payment_attempts", ["user_id"], unique=False)
    op.create_index("ix_payment_attempts_service_type", "payment_attempts", ["service_type"], unique=False)
    op.create_index("ix_payment_attempts_order_item_id", "payment_attempts", ["order_item_id"], unique=False)
    op.create_index("ix_payment_attempts_user_created", "payment_attempts", ["user_id", "created_at"], unique=False)
    op.create_index("ix_payment_attempts_status_created", "payment_attempts", ["status", "created_at"], unique=False)
