"""drop unused featured_locations.badge_text column

Revision ID: 0028_drop_featured_locations_badge_text
Revises: 0027_app_user_preferences
Create Date: 2026-05-05 17:35:00
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0028_drop_featured_locations_badge_text"
down_revision = "0027_app_user_preferences"
branch_labels = None
depends_on = None


def _column_names(table_name: str) -> set[str]:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return {column["name"] for column in inspector.get_columns(table_name)}


def upgrade() -> None:
    if "badge_text" in _column_names("featured_locations"):
        op.drop_column("featured_locations", "badge_text")


def downgrade() -> None:
    if "badge_text" not in _column_names("featured_locations"):
        op.add_column(
            "featured_locations",
            sa.Column("badge_text", sa.String(length=64), nullable=True),
        )
