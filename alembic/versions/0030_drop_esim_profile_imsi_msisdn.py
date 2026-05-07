"""drop unused esim_profiles.imsi and msisdn columns

Revision ID: 0030_drop_esim_profile_imsi_msisdn
Revises: 0029_drop_provider_snapshot_tables
Create Date: 2026-05-05 17:45:00
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0030_drop_esim_profile_imsi_msisdn"
down_revision = "0029_drop_provider_snapshot_tables"
branch_labels = None
depends_on = None


def _column_names(table_name: str) -> set[str]:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return {column["name"] for column in inspector.get_columns(table_name)}


def upgrade() -> None:
    existing = _column_names("esim_profiles")
    if "msisdn" in existing:
        op.drop_column("esim_profiles", "msisdn")
    if "imsi" in existing:
        op.drop_column("esim_profiles", "imsi")


def downgrade() -> None:
    existing = _column_names("esim_profiles")
    if "imsi" not in existing:
        op.add_column("esim_profiles", sa.Column("imsi", sa.String(length=120), nullable=True))
    if "msisdn" not in existing:
        op.add_column("esim_profiles", sa.Column("msisdn", sa.String(length=120), nullable=True))
