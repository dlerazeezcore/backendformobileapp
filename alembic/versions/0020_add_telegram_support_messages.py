"""add telegram support messages table

Revision ID: 0020_tg_support_msgs
Revises: 0019_push_allow_anonymous
Create Date: 2026-04-09 00:00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0020_tg_support_msgs"
down_revision = "0019_push_allow_anonymous"
branch_labels = None
depends_on = None


def _table_names() -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return set(inspector.get_table_names())


def upgrade() -> None:
    if "telegram_support_messages" not in _table_names():
        op.create_table(
            "telegram_support_messages",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("user_id", sa.Uuid(), nullable=True),
            sa.Column("admin_user_id", sa.Uuid(), nullable=True),
            sa.Column("direction", sa.String(length=32), nullable=False),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("message_text", sa.Text(), nullable=False),
            sa.Column("telegram_chat_id", sa.BigInteger(), nullable=True),
            sa.Column("telegram_message_id", sa.BigInteger(), nullable=True),
            sa.Column("push_delivery_status", sa.String(length=32), nullable=True),
            sa.Column("provider_payload", sa.JSON(), nullable=False),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["admin_user_id"], ["admin_users.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["user_id"], ["app_users.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("telegram_message_id"),
        )

    op.create_index(
        "ix_telegram_support_messages_user_created",
        "telegram_support_messages",
        ["user_id", "created_at"],
        unique=False,
        if_not_exists=True,
    )
    op.create_index(
        "ix_telegram_support_messages_direction_created",
        "telegram_support_messages",
        ["direction", "created_at"],
        unique=False,
        if_not_exists=True,
    )
    op.create_index(
        "ix_telegram_support_messages_status_created",
        "telegram_support_messages",
        ["status", "created_at"],
        unique=False,
        if_not_exists=True,
    )
    op.create_index("ix_telegram_support_messages_user_id", "telegram_support_messages", ["user_id"], unique=False, if_not_exists=True)
    op.create_index("ix_telegram_support_messages_admin_user_id", "telegram_support_messages", ["admin_user_id"], unique=False, if_not_exists=True)
    op.create_index("ix_telegram_support_messages_direction", "telegram_support_messages", ["direction"], unique=False, if_not_exists=True)
    op.create_index("ix_telegram_support_messages_status", "telegram_support_messages", ["status"], unique=False, if_not_exists=True)


def downgrade() -> None:
    if "telegram_support_messages" in _table_names():
        op.drop_index("ix_telegram_support_messages_status", table_name="telegram_support_messages", if_exists=True)
        op.drop_index("ix_telegram_support_messages_direction", table_name="telegram_support_messages", if_exists=True)
        op.drop_index("ix_telegram_support_messages_admin_user_id", table_name="telegram_support_messages", if_exists=True)
        op.drop_index("ix_telegram_support_messages_user_id", table_name="telegram_support_messages", if_exists=True)
        op.drop_index("ix_telegram_support_messages_status_created", table_name="telegram_support_messages", if_exists=True)
        op.drop_index("ix_telegram_support_messages_direction_created", table_name="telegram_support_messages", if_exists=True)
        op.drop_index("ix_telegram_support_messages_user_created", table_name="telegram_support_messages", if_exists=True)
        op.drop_table("telegram_support_messages")
