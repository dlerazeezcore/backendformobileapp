"""create app_release_info (singleton row for mobile version metadata)

Revision ID: 0040_app_release_info
Revises: 0039_create_app_user_travelers
Create Date: 2026-05-30 12:00:00
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0040_app_release_info"
down_revision = "0039_create_app_user_travelers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "app_release_info" in set(inspector.get_table_names()):
        return
    is_postgres = bind.dialect.name == "postgresql"
    now_default = sa.text("now()") if is_postgres else sa.text("CURRENT_TIMESTAMP")
    op.create_table(
        "app_release_info",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column("latest_version", sa.String(length=32), nullable=False, server_default="1.0.0"),
        sa.Column("min_supported_version", sa.String(length=32), nullable=False, server_default="1.0.0"),
        sa.Column("app_store_url", sa.String(length=512), nullable=False, server_default=""),
        sa.Column("play_store_url", sa.String(length=512), nullable=False, server_default=""),
        sa.Column("release_notes_en", sa.Text(), nullable=True),
        sa.Column("release_notes_ar", sa.Text(), nullable=True),
        sa.Column("release_notes_ku", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=now_default),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=now_default),
    )
    # Seed the singleton row. The mobile app expects this endpoint to always succeed,
    # so we make sure row id=1 exists from migration time. Defaults on created_at /
    # updated_at fill the timestamps on the row.
    op.execute(
        sa.text(
            """
            INSERT INTO app_release_info
                (id, latest_version, min_supported_version, app_store_url, play_store_url,
                 release_notes_en, release_notes_ar, release_notes_ku)
            VALUES
                (1, '1.0.0', '1.0.0', '', '', NULL, NULL, NULL)
            """
        )
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "app_release_info" not in set(inspector.get_table_names()):
        return
    op.drop_table("app_release_info")
