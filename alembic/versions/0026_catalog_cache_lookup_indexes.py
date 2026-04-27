"""add catalog lookup indexes

Revision ID: 0026_catalog_cache_lookup_indexes
Revises: 0025_esim_profile_lifecycle_indexes
Create Date: 2026-04-27 16:30:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0026_catalog_cache_lookup_indexes"
down_revision = "0025_esim_profile_lifecycle_indexes"
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
        "featured_locations",
        "ix_featured_locations_public_lookup",
        ["service_type", "enabled", "is_popular", "sort_order", "updated_at"],
    )


def downgrade() -> None:
    _drop_index_if_exists("featured_locations", "ix_featured_locations_public_lookup")
