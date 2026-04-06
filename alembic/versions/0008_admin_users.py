"""create separate admin user accounts

Revision ID: 0008_admin_users
Revises: 0007_order_pricing_snapshots
Create Date: 2026-04-06 14:05:00
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0008_admin_users"
down_revision = "0007_order_pricing_snapshots"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "admin_users",
        sa.Column("id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("phone", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("role", sa.String(length=64), nullable=False),
        sa.Column("can_manage_users", sa.Boolean(), nullable=False),
        sa.Column("can_manage_orders", sa.Boolean(), nullable=False),
        sa.Column("can_manage_pricing", sa.Boolean(), nullable=False),
        sa.Column("can_manage_content", sa.Boolean(), nullable=False),
        sa.Column("can_send_push", sa.Boolean(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("blocked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("custom_fields", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("phone"),
    )
    op.create_index(op.f("ix_admin_users_phone"), "admin_users", ["phone"], unique=False)
    op.create_index(op.f("ix_admin_users_role"), "admin_users", ["role"], unique=False)
    op.create_index(op.f("ix_admin_users_status"), "admin_users", ["status"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_admin_users_status"), table_name="admin_users")
    op.drop_index(op.f("ix_admin_users_role"), table_name="admin_users")
    op.drop_index(op.f("ix_admin_users_phone"), table_name="admin_users")
    op.drop_table("admin_users")
