"""app_users.app_version — last app build each user was seen on

Revision ID: 0047_app_user_app_version
Revises: 0046_backfill_admin_permission_flags
Create Date: 2026-07-02 00:00:00

The mobile app sends an ``X-App-Version`` header; ``GET /auth/me`` (called on
every launch / session restore) stamps it here. The admin users list compares
it against ``app_release_info.latest_version`` to show whether each user is on
the latest published version — which matters now that updates are mandatory
(older builds are blocked until they update).

Additive and nullable — existing rows read as "version unknown" until the
user's next app launch on a build that sends the header.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0047_app_user_app_version"
down_revision = "0046_backfill_admin_permission_flags"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("app_users", sa.Column("app_version", sa.String(32), nullable=True))
    op.add_column(
        "app_users",
        sa.Column("app_version_updated_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("app_users", "app_version_updated_at")
    op.drop_column("app_users", "app_version")
