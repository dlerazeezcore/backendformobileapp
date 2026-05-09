"""drop telegram_support_messages table — feature removed

Revision ID: 0033_drop_telegram_support_messages
Revises: 0032_telegram_support_indexes_and_trigger
Create Date: 2026-05-09 01:00:00

The Telegram-bridged support chat is being removed from the product. Drops the
table, its indexes, the updated_at trigger, and the dedicated S3-uploads code
paths follow in the same release. Downgrade restores the table shape from
migration 0020 (without data) so a previous release can roll back cleanly.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0033_drop_telegram_support_messages"
down_revision = "0032_telegram_support_indexes_and_trigger"
branch_labels = None
depends_on = None


def _table_names() -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return set(inspector.get_table_names())


def _index_names(table_name: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {index["name"] for index in inspector.get_indexes(table_name)}


def _drop_index_if_exists(table_name: str, index_name: str) -> None:
    if table_name not in _table_names():
        return
    if index_name not in _index_names(table_name):
        return
    op.drop_index(index_name, table_name=table_name)


def upgrade() -> None:
    bind = op.get_bind()
    dialect_name = bind.dialect.name
    tables = _table_names()

    if "telegram_support_messages" not in tables:
        return

    if dialect_name == "postgresql":
        op.execute(
            "drop trigger if exists trg_telegram_support_messages_updated_at on public.telegram_support_messages;"
        )

    # Drop indexes explicitly so the migration is portable across dialects that
    # don't auto-cascade them with the table.
    for index_name in (
        "ix_telegram_support_messages_user_id",
        "ix_telegram_support_messages_admin_user_id",
        "ix_telegram_support_messages_user_created",
        "ix_telegram_support_messages_direction_created",
        "ix_telegram_support_messages_status_created",
        "ix_telegram_support_messages_chat_created",
        "ix_telegram_support_messages_admin_self_created",
        # Defensive: legacy single-column indexes from migration 0020 that
        # 0032 dropped — re-list here in case 0032 didn't run on this DB.
        "ix_telegram_support_messages_direction",
        "ix_telegram_support_messages_status",
    ):
        _drop_index_if_exists("telegram_support_messages", index_name)

    op.drop_table("telegram_support_messages")


def downgrade() -> None:
    tables = _table_names()
    if "telegram_support_messages" in tables:
        return

    op.create_table(
        "telegram_support_messages",
        sa.Column("id", sa.Uuid(as_uuid=False), primary_key=True, nullable=False),
        sa.Column("user_id", sa.Uuid(as_uuid=False), nullable=True),
        sa.Column("admin_user_id", sa.Uuid(as_uuid=False), nullable=True),
        sa.Column("direction", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("message_text", sa.Text(), nullable=False),
        sa.Column("telegram_chat_id", sa.BigInteger(), nullable=True),
        sa.Column("telegram_message_id", sa.BigInteger(), nullable=True, unique=True),
        sa.Column("push_delivery_status", sa.String(length=32), nullable=True),
        sa.Column("provider_payload", sa.JSON(), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["user_id"], ["app_users.id"], name="fk_telegram_support_messages_user_id", ondelete="set null"),
        sa.ForeignKeyConstraint(["admin_user_id"], ["admin_users.id"], name="fk_telegram_support_messages_admin_user_id", ondelete="set null"),
    )
    op.create_index("ix_telegram_support_messages_user_id", "telegram_support_messages", ["user_id"], unique=False)
    op.create_index("ix_telegram_support_messages_admin_user_id", "telegram_support_messages", ["admin_user_id"], unique=False)
    op.create_index("ix_telegram_support_messages_user_created", "telegram_support_messages", ["user_id", "created_at"], unique=False)
    op.create_index("ix_telegram_support_messages_chat_created", "telegram_support_messages", ["telegram_chat_id", "created_at"], unique=False)
