"""add password_hash to app_users and admin_users

Revision ID: 0010_add_password_hash_columns
Revises: 0009_schema_consistency_indexes
Create Date: 2026-04-06 17:05:00
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0010_add_password_hash_columns"
down_revision = "0009_schema_consistency_indexes"
branch_labels = None
depends_on = None


def _column_names(table_name: str) -> set[str]:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return {column["name"] for column in inspector.get_columns(table_name)}


def upgrade() -> None:
    if "password_hash" not in _column_names("app_users"):
        op.add_column("app_users", sa.Column("password_hash", sa.String(length=255), nullable=True))
    if "password_hash" not in _column_names("admin_users"):
        op.add_column("admin_users", sa.Column("password_hash", sa.String(length=255), nullable=True))


def downgrade() -> None:
    if "password_hash" in _column_names("admin_users"):
        op.drop_column("admin_users", "password_hash")
    if "password_hash" in _column_names("app_users"):
        op.drop_column("app_users", "password_hash")
