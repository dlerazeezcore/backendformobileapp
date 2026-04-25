"""expand alembic version column length

Revision ID: 0021_alembic_version_len
Revises: 0020_tg_support_msgs
Create Date: 2026-04-10 16:58:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0021_alembic_version_len"
down_revision = "0020_tg_support_msgs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        return
    op.alter_column(
        "alembic_version",
        "version_num",
        existing_type=sa.String(length=32),
        type_=sa.String(length=64),
        existing_nullable=False,
    )


def downgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        return
    op.alter_column(
        "alembic_version",
        "version_num",
        existing_type=sa.String(length=64),
        type_=sa.String(length=32),
        existing_nullable=False,
    )

