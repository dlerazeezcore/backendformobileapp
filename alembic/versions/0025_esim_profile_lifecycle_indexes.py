"""add lifecycle query indexes for user profile inventory

Revision ID: 0025_esim_profile_lifecycle_indexes
Revises: 0024_norm_profile_usage_mb
Create Date: 2026-04-25 19:10:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0025_esim_profile_lifecycle_indexes"
down_revision = "0024_norm_profile_usage_mb"
branch_labels = None
depends_on = None


def _table_names() -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return set(inspector.get_table_names())


def _index_names(table_name: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {index["name"] for index in inspector.get_indexes(table_name)}


def _create_index_if_missing(table_name: str, index_name: str, columns: list[str]) -> None:
    if table_name not in _table_names():
        return
    if index_name in _index_names(table_name):
        return
    op.create_index(index_name, table_name, columns, unique=False)


def _drop_index_if_exists(table_name: str, index_name: str) -> None:
    if table_name not in _table_names():
        return
    if index_name not in _index_names(table_name):
        return
    op.drop_index(index_name, table_name=table_name)


def upgrade() -> None:
    _create_index_if_missing(
        "customer_orders",
        "ix_customer_orders_user_booked_created",
        ["user_id", "booked_at", "created_at"],
    )
    _create_index_if_missing(
        "order_items",
        "ix_order_items_customer_order_service_booked_created",
        ["customer_order_id", "service_type", "booked_at", "created_at"],
    )
    _create_index_if_missing(
        "esim_profiles",
        "ix_esim_profiles_user_updated_created",
        ["user_id", "updated_at", "created_at"],
    )


def downgrade() -> None:
    _drop_index_if_exists("esim_profiles", "ix_esim_profiles_user_updated_created")
    _drop_index_if_exists("order_items", "ix_order_items_customer_order_service_booked_created")
    _drop_index_if_exists("customer_orders", "ix_customer_orders_user_booked_created")
