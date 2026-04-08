"""add push notification device and delivery tables

Revision ID: 0017_push_notifications
Revises: 0016_payment_policy_indexes
Create Date: 2026-04-08 15:20:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0017_push_notifications"
down_revision = "0016_payment_policy_indexes"
branch_labels = None
depends_on = None


def _table_names() -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return set(inspector.get_table_names())


def _index_names(table_name: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {index["name"] for index in inspector.get_indexes(table_name)}


def upgrade() -> None:
    tables = _table_names()
    if "push_devices" not in tables:
        op.create_table(
            "push_devices",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Uuid(as_uuid=False), nullable=False),
            sa.Column("token", sa.String(length=512), nullable=False),
            sa.Column("platform", sa.String(length=32), nullable=False),
            sa.Column("device_id", sa.String(length=255), nullable=True),
            sa.Column("app_version", sa.String(length=64), nullable=True),
            sa.Column("locale", sa.String(length=32), nullable=True),
            sa.Column("timezone_name", sa.String(length=64), nullable=True),
            sa.Column("active", sa.Boolean(), nullable=False),
            sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("custom_fields", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["user_id"], ["app_users.id"], ondelete="CASCADE"),
            sa.UniqueConstraint("token", name="uq_push_devices_token"),
        )
    if "push_notifications" not in tables:
        op.create_table(
            "push_notifications",
            sa.Column("id", sa.Uuid(as_uuid=False), primary_key=True),
            sa.Column("recipient_scope", sa.String(length=32), nullable=False),
            sa.Column("title", sa.String(length=255), nullable=False),
            sa.Column("body", sa.Text(), nullable=False),
            sa.Column("data_payload", sa.JSON(), nullable=False),
            sa.Column("target_user_ids", sa.JSON(), nullable=False),
            sa.Column("channel_id", sa.String(length=64), nullable=False),
            sa.Column("image_url", sa.Text(), nullable=True),
            sa.Column("provider", sa.String(length=64), nullable=False),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("success_count", sa.Integer(), nullable=False),
            sa.Column("failure_count", sa.Integer(), nullable=False),
            sa.Column("invalid_token_count", sa.Integer(), nullable=False),
            sa.Column("invalid_tokens", sa.JSON(), nullable=False),
            sa.Column("provider_response", sa.JSON(), nullable=False),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("sent_by_admin_id", sa.Uuid(as_uuid=False), nullable=True),
            sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["sent_by_admin_id"], ["admin_users.id"], ondelete="SET NULL"),
        )

    tables = _table_names()
    if "push_devices" in tables:
        index_names = _index_names("push_devices")
        if "ix_push_devices_user_id" not in index_names:
            op.create_index("ix_push_devices_user_id", "push_devices", ["user_id"], unique=False)
        if "ix_push_devices_platform" not in index_names:
            op.create_index("ix_push_devices_platform", "push_devices", ["platform"], unique=False)
        if "ix_push_devices_device_id" not in index_names:
            op.create_index("ix_push_devices_device_id", "push_devices", ["device_id"], unique=False)
        if "ix_push_devices_active" not in index_names:
            op.create_index("ix_push_devices_active", "push_devices", ["active"], unique=False)
        if "ix_push_devices_user_active" not in index_names:
            op.create_index("ix_push_devices_user_active", "push_devices", ["user_id", "active"], unique=False)
        if "ix_push_devices_last_seen" not in index_names:
            op.create_index("ix_push_devices_last_seen", "push_devices", ["last_seen_at"], unique=False)

    if "push_notifications" in tables:
        index_names = _index_names("push_notifications")
        if "ix_push_notifications_recipient_scope" not in index_names:
            op.create_index("ix_push_notifications_recipient_scope", "push_notifications", ["recipient_scope"], unique=False)
        if "ix_push_notifications_status" not in index_names:
            op.create_index("ix_push_notifications_status", "push_notifications", ["status"], unique=False)
        if "ix_push_notifications_sent_by_admin_id" not in index_names:
            op.create_index("ix_push_notifications_sent_by_admin_id", "push_notifications", ["sent_by_admin_id"], unique=False)
        if "ix_push_notifications_status_created" not in index_names:
            op.create_index("ix_push_notifications_status_created", "push_notifications", ["status", "created_at"], unique=False)
        if "ix_push_notifications_sender_created" not in index_names:
            op.create_index("ix_push_notifications_sender_created", "push_notifications", ["sent_by_admin_id", "created_at"], unique=False)


def downgrade() -> None:
    tables = _table_names()
    if "push_notifications" in tables:
        index_names = _index_names("push_notifications")
        for index_name in (
            "ix_push_notifications_sender_created",
            "ix_push_notifications_status_created",
            "ix_push_notifications_sent_by_admin_id",
            "ix_push_notifications_status",
            "ix_push_notifications_recipient_scope",
        ):
            if index_name in index_names:
                op.drop_index(index_name, table_name="push_notifications")
        op.drop_table("push_notifications")

    if "push_devices" in tables:
        index_names = _index_names("push_devices")
        for index_name in (
            "ix_push_devices_last_seen",
            "ix_push_devices_user_active",
            "ix_push_devices_active",
            "ix_push_devices_device_id",
            "ix_push_devices_platform",
            "ix_push_devices_user_id",
        ):
            if index_name in index_names:
                op.drop_index(index_name, table_name="push_devices")
        op.drop_table("push_devices")
