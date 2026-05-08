"""telegram_support_messages: add chat composite index, admin self-scope partial index, drop redundant single-column indexes, attach updated_at trigger

Revision ID: 0032_telegram_support_indexes_and_trigger
Revises: 0031_create_app_settings
Create Date: 2026-05-08 14:00:00
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0032_telegram_support_indexes_and_trigger"
down_revision = "0031_create_app_settings"
branch_labels = None
depends_on = None


def _index_names(table_name: str) -> set[str]:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return {ix["name"] for ix in inspector.get_indexes(table_name)}


def upgrade() -> None:
    bind = op.get_bind()
    dialect_name = bind.dialect.name
    existing = _index_names("telegram_support_messages")

    # 1. Composite index for the webhook recent-thread fallback query:
    #    WHERE telegram_chat_id = ? AND direction IN (...) ORDER BY created_at DESC LIMIT 1
    if "ix_telegram_support_messages_chat_created" not in existing:
        op.create_index(
            "ix_telegram_support_messages_chat_created",
            "telegram_support_messages",
            ["telegram_chat_id", "created_at"],
            unique=False,
        )

    # 2. Partial composite for the admin default-scope query:
    #    WHERE admin_user_id = ? AND user_id IS NULL ORDER BY created_at DESC
    # Use raw SQL to express the WHERE clause and DESC order; op.create_index doesn't support partial in all dialects.
    if "ix_telegram_support_messages_admin_self_created" not in existing:
        if dialect_name == "postgresql":
            op.execute(
                """
                CREATE INDEX ix_telegram_support_messages_admin_self_created
                ON telegram_support_messages (admin_user_id, created_at DESC)
                WHERE user_id IS NULL
                """
            )
        else:
            # SQLite (used in tests) -- no partial index support; create a plain composite that still helps.
            op.create_index(
                "ix_telegram_support_messages_admin_self_created",
                "telegram_support_messages",
                ["admin_user_id", "created_at"],
                unique=False,
            )

    # 3. Drop redundant single-column indexes that are never used standalone.
    #    Keep ix_telegram_support_messages_user_id and ix_telegram_support_messages_admin_user_id
    #    because the FK ON DELETE SET NULL paths benefit from them.
    if "ix_telegram_support_messages_direction" in existing:
        op.drop_index(
            "ix_telegram_support_messages_direction",
            table_name="telegram_support_messages",
        )
    if "ix_telegram_support_messages_status" in existing:
        op.drop_index(
            "ix_telegram_support_messages_status",
            table_name="telegram_support_messages",
        )

    # 4. Attach the standard updated_at trigger so the column auto-advances even when raw SQL is used.
    if dialect_name == "postgresql":
        op.execute(
            "DROP TRIGGER IF EXISTS trg_telegram_support_messages_updated_at ON public.telegram_support_messages"
        )
        op.execute(
            """
            CREATE TRIGGER trg_telegram_support_messages_updated_at
            BEFORE UPDATE ON public.telegram_support_messages
            FOR EACH ROW EXECUTE FUNCTION public.set_updated_at()
            """
        )


def downgrade() -> None:
    bind = op.get_bind()
    dialect_name = bind.dialect.name
    existing = _index_names("telegram_support_messages")

    if dialect_name == "postgresql":
        op.execute(
            "DROP TRIGGER IF EXISTS trg_telegram_support_messages_updated_at ON public.telegram_support_messages"
        )

    if "ix_telegram_support_messages_admin_self_created" in existing:
        op.drop_index(
            "ix_telegram_support_messages_admin_self_created",
            table_name="telegram_support_messages",
        )
    if "ix_telegram_support_messages_chat_created" in existing:
        op.drop_index(
            "ix_telegram_support_messages_chat_created",
            table_name="telegram_support_messages",
        )

    # Recreate the dropped single-column indexes for symmetry with prior state.
    if "ix_telegram_support_messages_status" not in existing:
        op.create_index(
            "ix_telegram_support_messages_status",
            "telegram_support_messages",
            ["status"],
            unique=False,
        )
    if "ix_telegram_support_messages_direction" not in existing:
        op.create_index(
            "ix_telegram_support_messages_direction",
            "telegram_support_messages",
            ["direction"],
            unique=False,
        )
