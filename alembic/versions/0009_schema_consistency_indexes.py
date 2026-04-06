"""align indexes with model uniqueness and add package_slug index

Revision ID: 0009_schema_consistency_indexes
Revises: 0008_admin_users
Create Date: 2026-04-06 16:20:00
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0009_schema_consistency_indexes"
down_revision = "0008_admin_users"
branch_labels = None
depends_on = None


def _index_names(table_name: str) -> set[str]:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return {index["name"] for index in inspector.get_indexes(table_name)}


def _drop_index_if_exists(table_name: str, index_name: str) -> None:
    if index_name in _index_names(table_name):
        op.drop_index(index_name, table_name=table_name)


def _create_index_if_missing(table_name: str, index_name: str, columns: list[str], *, unique: bool = False) -> None:
    if index_name not in _index_names(table_name):
        op.create_index(index_name, table_name, columns, unique=unique)


def upgrade() -> None:
    # Remove duplicate non-unique indexes where a UNIQUE constraint already provides indexing.
    _drop_index_if_exists("app_users", "ix_app_users_phone")
    _drop_index_if_exists("customer_orders", "ix_customer_orders_order_number")
    _drop_index_if_exists("order_items", "ix_order_items_provider_order_no")
    _drop_index_if_exists("order_items", "ix_order_items_provider_transaction_id")
    _drop_index_if_exists("esim_profiles", "ix_esim_profiles_esim_tran_no")
    _drop_index_if_exists("esim_profiles", "ix_esim_profiles_iccid")
    _drop_index_if_exists("admin_users", "ix_admin_users_phone")

    # Keep model and schema aligned: package_slug is indexed in ORM and should be in DB.
    _create_index_if_missing("order_items", "ix_order_items_package_slug", ["package_slug"], unique=False)


def downgrade() -> None:
    _drop_index_if_exists("order_items", "ix_order_items_package_slug")

    _create_index_if_missing("admin_users", "ix_admin_users_phone", ["phone"], unique=False)
    _create_index_if_missing("app_users", "ix_app_users_phone", ["phone"], unique=False)
    _create_index_if_missing("customer_orders", "ix_customer_orders_order_number", ["order_number"], unique=False)
    _create_index_if_missing("order_items", "ix_order_items_provider_order_no", ["provider_order_no"], unique=False)
    _create_index_if_missing("order_items", "ix_order_items_provider_transaction_id", ["provider_transaction_id"], unique=False)
    _create_index_if_missing("esim_profiles", "ix_esim_profiles_esim_tran_no", ["esim_tran_no"], unique=False)
    _create_index_if_missing("esim_profiles", "ix_esim_profiles_iccid", ["iccid"], unique=False)
