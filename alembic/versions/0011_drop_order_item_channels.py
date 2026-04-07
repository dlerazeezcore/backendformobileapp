"""drop unused order_items channel/platform columns

Revision ID: 0011_drop_order_item_channels
Revises: 0010_add_password_hash_columns
Create Date: 2026-04-07 01:25:00
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0011_drop_order_item_channels"
down_revision = "0010_add_password_hash_columns"
branch_labels = None
depends_on = None


def _column_names(table_name: str) -> set[str]:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return {column["name"] for column in inspector.get_columns(table_name)}


def upgrade() -> None:
    columns = _column_names("order_items")
    if "revoked_via_platform" in columns:
        op.drop_column("order_items", "revoked_via_platform")
    if "refunded_via_platform" in columns:
        op.drop_column("order_items", "refunded_via_platform")
    if "canceled_via_platform" in columns:
        op.drop_column("order_items", "canceled_via_platform")
    if "booked_via_platform" in columns:
        op.drop_column("order_items", "booked_via_platform")
    if "purchase_channel" in columns:
        op.drop_column("order_items", "purchase_channel")


def downgrade() -> None:
    columns = _column_names("order_items")
    if "purchase_channel" not in columns:
        op.add_column("order_items", sa.Column("purchase_channel", sa.String(length=80), nullable=True))
    if "booked_via_platform" not in columns:
        op.add_column("order_items", sa.Column("booked_via_platform", sa.String(length=80), nullable=True))
    if "canceled_via_platform" not in columns:
        op.add_column("order_items", sa.Column("canceled_via_platform", sa.String(length=80), nullable=True))
    if "refunded_via_platform" not in columns:
        op.add_column("order_items", sa.Column("refunded_via_platform", sa.String(length=80), nullable=True))
    if "revoked_via_platform" not in columns:
        op.add_column("order_items", sa.Column("revoked_via_platform", sa.String(length=80), nullable=True))
