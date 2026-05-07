"""create app_settings KV table for global admin toggles

Revision ID: 0031_create_app_settings
Revises: 0030_drop_esim_profile_imsi_msisdn
Create Date: 2026-05-07 14:30:00
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0031_create_app_settings"
down_revision = "0030_drop_esim_profile_imsi_msisdn"
branch_labels = None
depends_on = None


def _table_names() -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return set(inspector.get_table_names())


def upgrade() -> None:
    bind = op.get_bind()
    dialect_name = bind.dialect.name
    if "app_settings" in _table_names():
        return
    op.create_table(
        "app_settings",
        sa.Column("key", sa.String(length=64), primary_key=True, nullable=False),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    if dialect_name == "postgresql":
        op.execute(
            """
            create trigger trg_app_settings_updated_at
            before update on public.app_settings
            for each row execute function set_updated_at();
            """
        )


def downgrade() -> None:
    bind = op.get_bind()
    dialect_name = bind.dialect.name
    if "app_settings" not in _table_names():
        return
    if dialect_name == "postgresql":
        op.execute("drop trigger if exists trg_app_settings_updated_at on public.app_settings;")
    op.drop_table("app_settings")
