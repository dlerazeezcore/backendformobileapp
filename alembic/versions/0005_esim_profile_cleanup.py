"""clean up duplicate esim profile fields

Revision ID: 0005_esim_profile_cleanup
Revises: 0004_profile_tracking
Create Date: 2026-04-05 02:25:00
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0005_esim_profile_cleanup"
down_revision = "0004_profile_tracking"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("ix_esim_profiles_package_code", table_name="esim_profiles")
    op.drop_index("ix_esim_profiles_country_code", table_name="esim_profiles")

    op.drop_column("esim_profiles", "booked_at")
    op.drop_column("esim_profiles", "booked_via_platform")
    op.drop_column("esim_profiles", "canceled_via_platform")
    op.drop_column("esim_profiles", "refunded_via_platform")
    op.drop_column("esim_profiles", "revoked_via_platform")
    op.drop_column("esim_profiles", "suspended_via_platform")
    op.drop_column("esim_profiles", "unsuspended_via_platform")
    op.drop_column("esim_profiles", "country_code")
    op.drop_column("esim_profiles", "country_name")
    op.drop_column("esim_profiles", "package_code")
    op.drop_column("esim_profiles", "package_name")


def downgrade() -> None:
    op.add_column("esim_profiles", sa.Column("package_name", sa.String(length=255), nullable=True))
    op.add_column("esim_profiles", sa.Column("package_code", sa.String(length=120), nullable=True))
    op.add_column("esim_profiles", sa.Column("country_name", sa.String(length=255), nullable=True))
    op.add_column("esim_profiles", sa.Column("country_code", sa.String(length=8), nullable=True))
    op.add_column("esim_profiles", sa.Column("unsuspended_via_platform", sa.String(length=80), nullable=True))
    op.add_column("esim_profiles", sa.Column("suspended_via_platform", sa.String(length=80), nullable=True))
    op.add_column("esim_profiles", sa.Column("revoked_via_platform", sa.String(length=80), nullable=True))
    op.add_column("esim_profiles", sa.Column("refunded_via_platform", sa.String(length=80), nullable=True))
    op.add_column("esim_profiles", sa.Column("canceled_via_platform", sa.String(length=80), nullable=True))
    op.add_column("esim_profiles", sa.Column("booked_via_platform", sa.String(length=80), nullable=True))
    op.add_column("esim_profiles", sa.Column("booked_at", sa.DateTime(timezone=True), nullable=True))

    op.create_index("ix_esim_profiles_country_code", "esim_profiles", ["country_code"], unique=False)
    op.create_index("ix_esim_profiles_package_code", "esim_profiles", ["package_code"], unique=False)
