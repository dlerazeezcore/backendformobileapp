"""add payment_attempts table for generic payment lifecycle

Revision ID: 0012_add_payment_attempts
Revises: 0011_drop_order_item_channels
Create Date: 2026-04-07 23:35:00
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0012_add_payment_attempts"
down_revision = "0011_drop_order_item_channels"
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


def _unique_names(table_name: str) -> set[str]:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return {constraint["name"] for constraint in inspector.get_unique_constraints(table_name) if constraint.get("name")}


def upgrade() -> None:
    bind = op.get_bind()
    is_sqlite = bind.dialect.name == "sqlite"
    if "payment_attempts" not in _table_names():
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
            sa.Column("metadata", sa.JSON(), nullable=False),
            sa.Column("provider_request", sa.JSON(), nullable=False),
            sa.Column("provider_response", sa.JSON(), nullable=False),
            sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["order_item_id"], ["order_items.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["user_id"], ["app_users.id"], ondelete="SET NULL"),
            sa.UniqueConstraint("transaction_id", name="uq_payment_attempts_transaction_id"),
            sa.UniqueConstraint("provider", "provider_payment_id", name="uq_payment_attempts_provider_payment_id"),
            sa.PrimaryKeyConstraint("id"),
        )

    unique_names = _unique_names("payment_attempts")
    if not is_sqlite and "uq_payment_attempts_transaction_id" not in unique_names:
        op.create_unique_constraint(
            "uq_payment_attempts_transaction_id",
            "payment_attempts",
            ["transaction_id"],
        )
    if not is_sqlite and "uq_payment_attempts_provider_payment_id" not in unique_names:
        op.create_unique_constraint(
            "uq_payment_attempts_provider_payment_id",
            "payment_attempts",
            ["provider", "provider_payment_id"],
        )

    index_names = _index_names("payment_attempts")
    index_specs = [
        ("ix_payment_attempts_payment_method", ["payment_method"]),
        ("ix_payment_attempts_provider", ["provider"]),
        ("ix_payment_attempts_status", ["status"]),
        ("ix_payment_attempts_user_id", ["user_id"]),
        ("ix_payment_attempts_service_type", ["service_type"]),
        ("ix_payment_attempts_order_item_id", ["order_item_id"]),
        ("ix_payment_attempts_user_created", ["user_id", "created_at"]),
        ("ix_payment_attempts_status_created", ["status", "created_at"]),
    ]
    for name, columns in index_specs:
        if name not in index_names:
            op.create_index(name, "payment_attempts", columns, unique=False)


def downgrade() -> None:
    if "payment_attempts" not in _table_names():
        return
    index_names = _index_names("payment_attempts")
    for index_name in (
        "ix_payment_attempts_status_created",
        "ix_payment_attempts_user_created",
        "ix_payment_attempts_order_item_id",
        "ix_payment_attempts_service_type",
        "ix_payment_attempts_user_id",
        "ix_payment_attempts_status",
        "ix_payment_attempts_provider",
        "ix_payment_attempts_payment_method",
    ):
        if index_name in index_names:
            op.drop_index(index_name, table_name="payment_attempts")
    unique_names = _unique_names("payment_attempts")
    if "uq_payment_attempts_provider_payment_id" in unique_names:
        op.drop_constraint("uq_payment_attempts_provider_payment_id", "payment_attempts", type_="unique")
    if "uq_payment_attempts_transaction_id" in unique_names:
        op.drop_constraint("uq_payment_attempts_transaction_id", "payment_attempts", type_="unique")
    op.drop_table("payment_attempts")
