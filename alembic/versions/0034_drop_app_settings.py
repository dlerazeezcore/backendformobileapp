"""drop app_settings KV table — admin whitelist UI removed

Revision ID: 0034_drop_app_settings
Revises: 0033_drop_telegram_support_messages
Create Date: 2026-05-09 02:00:00

The only writer of this KV table was the admin "whitelist-settings" UI, which
is being removed alongside the home tutorial admin panel. The stored
`country_whitelist` value was never read by any non-admin code path, so
dropping the table has no user-visible effect. Downgrade recreates the empty
table shape from migration 0031.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0034_drop_app_settings"
down_revision = "0033_drop_telegram_support_messages"
branch_labels = None
depends_on = None


def _table_names() -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return set(inspector.get_table_names())


def upgrade() -> None:
    bind = op.get_bind()
    dialect_name = bind.dialect.name

    if "app_settings" not in _table_names():
        return

    if dialect_name == "postgresql":
        op.execute("drop trigger if exists trg_app_settings_updated_at on public.app_settings;")

    op.drop_table("app_settings")


def downgrade() -> None:
    if "app_settings" in _table_names():
        return

    op.create_table(
        "app_settings",
        sa.Column("key", sa.String(length=64), primary_key=True, nullable=False),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            """
            create trigger trg_app_settings_updated_at
            before update on public.app_settings
            for each row execute function public.set_updated_at();
            """
        )
