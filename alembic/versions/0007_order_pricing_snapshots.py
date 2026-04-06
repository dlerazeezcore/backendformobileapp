"""add immutable pricing snapshot fields

Revision ID: 0007_order_pricing_snapshots
Revises: 0006_admin_rules
Create Date: 2026-04-05 03:30:00
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0007_order_pricing_snapshots"
down_revision = "0006_admin_rules"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("customer_orders", sa.Column("discount_minor", sa.Integer(), nullable=True, server_default="0"))

    op.add_column("order_items", sa.Column("markup_minor", sa.Integer(), nullable=True))
    op.add_column("order_items", sa.Column("discount_minor", sa.Integer(), nullable=True))
    op.add_column("order_items", sa.Column("applied_pricing_rule_id", sa.Integer(), nullable=True))
    op.add_column("order_items", sa.Column("applied_discount_rule_id", sa.Integer(), nullable=True))
    op.add_column("order_items", sa.Column("applied_pricing_rule_type", sa.String(length=16), nullable=True))
    op.add_column("order_items", sa.Column("applied_pricing_rule_value", sa.Float(), nullable=True))
    op.add_column("order_items", sa.Column("applied_pricing_rule_basis", sa.String(length=32), nullable=True))
    op.add_column("order_items", sa.Column("applied_discount_rule_type", sa.String(length=16), nullable=True))
    op.add_column("order_items", sa.Column("applied_discount_rule_value", sa.Float(), nullable=True))
    op.add_column("order_items", sa.Column("applied_discount_rule_basis", sa.String(length=32), nullable=True))


def downgrade() -> None:
    op.drop_column("order_items", "applied_discount_rule_basis")
    op.drop_column("order_items", "applied_discount_rule_value")
    op.drop_column("order_items", "applied_discount_rule_type")
    op.drop_column("order_items", "applied_pricing_rule_basis")
    op.drop_column("order_items", "applied_pricing_rule_value")
    op.drop_column("order_items", "applied_pricing_rule_type")
    op.drop_column("order_items", "applied_discount_rule_id")
    op.drop_column("order_items", "applied_pricing_rule_id")
    op.drop_column("order_items", "discount_minor")
    op.drop_column("order_items", "markup_minor")

    op.drop_column("customer_orders", "discount_minor")
