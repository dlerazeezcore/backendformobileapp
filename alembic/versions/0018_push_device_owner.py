"""allow push devices to be owned by admin users

Revision ID: 0018_push_device_owner
Revises: 0017_push_notifications
Create Date: 2026-04-08 16:20:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0018_push_device_owner"
down_revision = "0017_push_notifications"
branch_labels = None
depends_on = None


def _table_names() -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return set(inspector.get_table_names())


def _index_names(table_name: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {index["name"] for index in inspector.get_indexes(table_name)}


def upgrade() -> None:
    if "push_devices" not in _table_names():
        return

    with op.batch_alter_table("push_devices") as batch_op:
        batch_op.add_column(sa.Column("admin_user_id", sa.Uuid(as_uuid=False), nullable=True))
        batch_op.create_foreign_key(
            "fk_push_devices_admin_user_id_admin_users",
            "admin_users",
            ["admin_user_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch_op.alter_column("user_id", existing_type=sa.Uuid(as_uuid=False), nullable=True)
        batch_op.create_check_constraint(
            "ck_push_devices_has_owner",
            "(user_id IS NOT NULL AND admin_user_id IS NULL) OR (user_id IS NULL AND admin_user_id IS NOT NULL)",
        )

    index_names = _index_names("push_devices")
    if "ix_push_devices_admin_user_id" not in index_names:
        op.create_index("ix_push_devices_admin_user_id", "push_devices", ["admin_user_id"], unique=False)
    if "ix_push_devices_admin_active" not in index_names:
        op.create_index("ix_push_devices_admin_active", "push_devices", ["admin_user_id", "active"], unique=False)

def downgrade() -> None:
    if "push_devices" not in _table_names():
        return

    index_names = _index_names("push_devices")
    if "ix_push_devices_admin_active" in index_names:
        op.drop_index("ix_push_devices_admin_active", table_name="push_devices")
    if "ix_push_devices_admin_user_id" in index_names:
        op.drop_index("ix_push_devices_admin_user_id", table_name="push_devices")

    with op.batch_alter_table("push_devices") as batch_op:
        batch_op.drop_constraint("ck_push_devices_has_owner", type_="check")
        batch_op.drop_constraint("fk_push_devices_admin_user_id_admin_users", type_="foreignkey")
        batch_op.drop_column("admin_user_id")
        batch_op.alter_column("user_id", existing_type=sa.Uuid(as_uuid=False), nullable=False)
