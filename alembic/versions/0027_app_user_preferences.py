"""add user preference columns to app_users

Revision ID: 0027_app_user_preferences
Revises: 0026_catalog_cache_lookup_indexes
Create Date: 2026-05-05 17:30:00
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0027_app_user_preferences"
down_revision = "0026_catalog_cache_lookup_indexes"
branch_labels = None
depends_on = None


def _column_names(table_name: str) -> set[str]:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return {column["name"] for column in inspector.get_columns(table_name)}


def upgrade() -> None:
    existing = _column_names("app_users")
    if "preferred_language" not in existing:
        op.add_column(
            "app_users",
            sa.Column("preferred_language", sa.String(length=8), nullable=True),
        )
    if "preferred_currency" not in existing:
        op.add_column(
            "app_users",
            sa.Column("preferred_currency", sa.String(length=8), nullable=True),
        )
    if "notifications_enabled" not in existing:
        op.add_column(
            "app_users",
            sa.Column(
                "notifications_enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            ),
        )
        # server_default has done its job for the backfill; drop it so application-level
        # default (True) is the single source of truth going forward.
        op.alter_column("app_users", "notifications_enabled", server_default=None)


def downgrade() -> None:
    existing = _column_names("app_users")
    if "notifications_enabled" in existing:
        op.drop_column("app_users", "notifications_enabled")
    if "preferred_currency" in existing:
        op.drop_column("app_users", "preferred_currency")
    if "preferred_language" in existing:
        op.drop_column("app_users", "preferred_language")
