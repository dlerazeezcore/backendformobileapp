"""create app_user_travelers (saved travelers per user)

Revision ID: 0039_create_app_user_travelers
Revises: 0038_clean_featured_locations
Create Date: 2026-05-26 14:00:00
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0039_create_app_user_travelers"
down_revision = "0038_clean_featured_locations"
branch_labels = None
depends_on = None


def _json_type(bind) -> sa.types.TypeEngine:
    if bind.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import JSONB

        return JSONB()
    return sa.JSON()


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "app_user_travelers" in set(inspector.get_table_names()):
        return
    op.create_table(
        "app_user_travelers",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Uuid(as_uuid=False), sa.ForeignKey("app_users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("relation", sa.String(length=64), nullable=True),
        sa.Column("dob", sa.String(length=32), nullable=True),
        sa.Column("custom_fields", _json_type(bind), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_app_user_travelers_user_id", "app_user_travelers", ["user_id"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "app_user_travelers" not in set(inspector.get_table_names()):
        return
    op.drop_index("ix_app_user_travelers_user_id", table_name="app_user_travelers")
    op.drop_table("app_user_travelers")
