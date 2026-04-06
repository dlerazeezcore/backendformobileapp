"""baseline app_users

Revision ID: 0001_baseline_app_users
Revises: None
Create Date: 2026-04-05 00:00:00
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0001_baseline_app_users"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    dialect_name = bind.dialect.name

    if dialect_name == "postgresql":
        op.execute('create extension if not exists "pgcrypto";')
        op.execute(
            """
            create or replace function public.set_updated_at()
            returns trigger
            language plpgsql
            as $$
            begin
              new.updated_at = now();
              return new;
            end;
            $$;
            """
        )

    op.create_table(
        "app_users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("phone", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="active"),
        sa.Column("is_loyalty", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("blocked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status in ('active', 'blocked', 'deleted')", name="ck_app_users_status"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("phone", name="uq_app_users_phone"),
    )
    op.create_index("ix_app_users_phone", "app_users", ["phone"], unique=False)
    op.create_index("ix_app_users_status", "app_users", ["status"], unique=False)

    if dialect_name == "postgresql":
        op.execute("drop trigger if exists trg_app_users_updated_at on public.app_users;")
        op.execute(
            """
            create trigger trg_app_users_updated_at
            before update on public.app_users
            for each row
            execute function public.set_updated_at();
            """
        )


def downgrade() -> None:
    bind = op.get_bind()
    dialect_name = bind.dialect.name

    if dialect_name == "postgresql":
        op.execute("drop trigger if exists trg_app_users_updated_at on public.app_users;")

    op.drop_index("ix_app_users_status", table_name="app_users")
    op.drop_index("ix_app_users_phone", table_name="app_users")
    op.drop_table("app_users")

    if dialect_name == "postgresql":
        op.execute("drop function if exists public.set_updated_at();")
