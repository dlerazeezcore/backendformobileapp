"""case-insensitive unique email index for app_users and admin_users

Revision ID: 0035_user_email_unique_ci
Revises: 0034_drop_app_settings
Create Date: 2026-05-26 09:00:00

Email becomes an alternative login identifier, so it must be unique. A partial,
functional unique index on lower(email) WHERE email IS NOT NULL enforces
case-insensitive uniqueness while allowing many NULL-email rows. Identical SQL
works on Postgres and SQLite (both support expression + partial indexes).
"""
from __future__ import annotations

from alembic import op


revision = "0035_user_email_unique_ci"
down_revision = "0034_drop_app_settings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_app_users_email_ci "
        "ON app_users (lower(email)) WHERE email IS NOT NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_admin_users_email_ci "
        "ON admin_users (lower(email)) WHERE email IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_admin_users_email_ci")
    op.execute("DROP INDEX IF EXISTS uq_app_users_email_ci")
